"""Check LAN adapter selection and the browser-visible sharing certificate."""

import hashlib
import socket
import ssl
import struct
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from ipaddress import IPv4Address
from pathlib import Path

from localweb.lan import _network_options, discover_interfaces, tls_context


def address_row(address, mask, index=1, flags=1):
    def native(value):
        return struct.unpack("=I", IPv4Address(value).packed)[0]

    return native(address), index, native(mask), 0, 0, 0, flags


def route_row(index, metric):
    return (0, 0, 0, 0, index, 4, 0, 0, 0, metric, 0, 0, 0, 0)


class NetworkOptionsTests(unittest.TestCase):
    def test_filter_masks_and_rank_active_lan_addresses(self):
        slash24 = "255.255.255.0"
        addresses = [
            address_row("198.51.100.2", slash24, 5),
            address_row("192.0.2.42", slash24, 2),
            address_row("192.0.2.42", "255.255.0.0", 3),
            address_row("192.0.2.222", "255.255.255.254", 4),
            address_row("127.0.0.1", slash24),
            address_row("169.254.62.9", slash24),
            address_row("224.0.0.5", slash24),
            address_row("0.0.0.3", slash24),
            address_row("203.0.113.1", "255.0.255.0"),
            address_row("203.0.113.1", "0.0.0.255"),
            address_row("203.0.113.1", "255.255.255.255"),
            address_row("198.51.100.3", "128.0.0.0"),
            address_row("192.0.2.0", slash24),
            address_row("192.0.2.255", slash24),
            *(address_row("198.51.100.5", slash24, flags=flag) for flag in (8, 64, 128)),
        ]
        self.assertEqual(
            _network_options(addresses, [route_row(5, 90), route_row(2, 5)]),
            [
                {"address": "192.0.2.42", "prefix": 24},
                {"address": "198.51.100.2", "prefix": 24},
                {"address": "192.0.2.222", "prefix": 31},
            ],
        )

    @unittest.skipUnless(sys.platform == "win32", "Windows IP Helper tables")
    def test_system_addresses_are_real_ipv4_interfaces(self):
        for interface in discover_interfaces():
            self.assertFalse(IPv4Address(interface["address"]).is_loopback)
            self.assertIn(interface["prefix"], range(8, 32))


class TLSIdentityTests(unittest.TestCase):
    def test_ip_san_pin_and_no_persisted_private_key(self):
        with tempfile.TemporaryDirectory() as folder:
            context, fingerprint = tls_context(Path(folder), "127.0.0.1")
            self.assertGreaterEqual(context.minimum_version, ssl.TLSVersion.TLSv1_2)
            self.assertEqual(list(Path(folder).iterdir()), [])

        def respond(listener):
            with (
                listener.accept()[0] as client,
                context.wrap_socket(client, server_side=True) as tls,
            ):
                tls.sendall(b"ready")

        with socket.socket() as listener, ThreadPoolExecutor(max_workers=1) as pool:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            listener.settimeout(5)
            client = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            client.minimum_version = ssl.TLSVersion.TLSv1_2
            client.check_hostname = False
            client.verify_mode = ssl.CERT_NONE
            for _ in range(2):
                reply = pool.submit(respond, listener)
                with (
                    socket.create_connection(listener.getsockname(), timeout=5) as sock,
                    client.wrap_socket(sock, server_hostname="127.0.0.1") as tls,
                ):
                    der = tls.getpeercert(binary_form=True)
                    self.assertEqual(hashlib.sha256(der).digest().hex(":").upper(), fingerprint)
                    self.assertEqual(tls.recv(5), b"ready")
                reply.result(timeout=5)
                # The second handshake verifies the pinned self-signed leaf and IP SAN.
                client = ssl.create_default_context(cadata=ssl.DER_cert_to_PEM_cert(der))
                client.minimum_version = ssl.TLSVersion.TLSv1_2
