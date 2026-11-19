"""IPv4 interfaces and ephemeral HTTPS identity for the Windows LAN listener."""

from __future__ import annotations

import ctypes
import os
import secrets
import ssl
import struct
from datetime import UTC, datetime, timedelta
from ipaddress import IPv4Address, IPv4Network
from pathlib import Path
from tempfile import TemporaryDirectory

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


def _read_table(function, row_format):
    """Read one variable-sized Win32 IP Helper table, retrying if adapters change."""
    function.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32), ctypes.c_int]
    function.restype = ctypes.c_uint32
    size = ctypes.c_uint32()
    buffer = None
    for _ in range(4):
        status = function(buffer, ctypes.byref(size), False)
        if status == 0 and buffer is not None:
            count = struct.unpack_from("=I", buffer)[0]
            return list(row_format.iter_unpack(buffer.raw[4 : 4 + count * row_format.size]))
        if status != 122:  # ERROR_INSUFFICIENT_BUFFER
            raise OSError(status, "Windows IP Helper table unavailable")
        buffer = ctypes.create_string_buffer(size.value)
    raise OSError("Windows network interface table changed repeatedly")


def _network_options(address_rows, route_rows):
    default_metrics = {}
    for destination, mask, _, _, index, route_type, _, _, _, metric, *_ in route_rows:
        if destination == mask == 0 and route_type in (3, 4):
            default_metrics[index] = min(metric, default_metrics.get(index, metric))
    ranked = []
    for address, index, mask, _, _, _, flags in address_rows:
        if flags & (0x0008 | 0x0040 | 0x0080):  # Disconnected, deleted or transient
            continue
        ip = IPv4Address(struct.pack("=I", address))
        if (
            ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.packed[0] == 0
        ):
            continue
        mask_bytes = struct.pack("=I", mask)
        prefix = int.from_bytes(mask_bytes, "big").bit_count()
        network = IPv4Network((ip, prefix), strict=False)
        if network.netmask.packed != mask_bytes:
            continue
        if network.prefixlen < 31 and ip in (network.network_address, network.broadcast_address):
            continue
        metric = default_metrics.get(index, float("inf"))
        ranked.append((metric == float("inf"), not ip.is_private, metric, ip, -network.prefixlen))
    # An address can be assigned on multiple adapters. Prefer its longest on-link prefix.
    prefixes = {}
    for _, _, _, ip, negative_prefix in sorted(ranked):
        prefixes[str(ip)] = max(prefixes.get(str(ip), 0), -negative_prefix)
    return [{"address": address, "prefix": prefix} for address, prefix in prefixes.items()]


def discover_interfaces() -> list[dict]:
    if os.name != "nt":
        return []
    library = ctypes.WinDLL("iphlpapi.dll", winmode=0x0800)  # LOAD_LIBRARY_SEARCH_SYSTEM32
    try:
        # MIB_IPADDRROW_XP has five DWORD and two WORD fields.
        addresses = _read_table(library.GetIpAddrTable, struct.Struct("=5I2H"))
    except OSError:
        return []
    try:
        # MIB_IPFORWARDROW has fourteen DWORD fields.
        routes = _read_table(library.GetIpForwardTable, struct.Struct("=14I"))
    except OSError:
        routes = []
    return _network_options(addresses, routes)


def tls_context(directory: Path, address: str) -> tuple[ssl.SSLContext, str]:
    """Return a server context and its SHA-256 fingerprint for browser verification."""
    ip = IPv4Address(address)
    private_key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, str(ip))])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=90))
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ip)]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(private_key, hashes.SHA256())
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    password = secrets.token_urlsafe(32).encode("ascii")
    # Python's SSL loader requires a file. Restrict its directory to this user and
    # encrypt the one-use private key. Remove the file before exposing the listener.
    with TemporaryDirectory(dir=directory) as private:
        pem = Path(private, "identity.pem")
        pem.write_bytes(
            certificate.public_bytes(serialization.Encoding.PEM)
            + private_key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.BestAvailableEncryption(password),
            )
        )
        context.load_cert_chain(pem, password=password)
    fingerprint = certificate.fingerprint(hashes.SHA256()).hex(":").upper()
    return context, fingerprint
