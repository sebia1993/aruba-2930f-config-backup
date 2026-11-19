"""Build the self-contained CPython/Windows release on a Windows x64 build host."""

import argparse
import ast
import base64
import csv
import hashlib
import io
import os
import re
import shutil
import struct
import subprocess
import sys
import tokenize
from itertools import accumulate
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from urllib.request import urlopen
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]
VERSION = "1.8.1"
PYTHON = "3.14.3"
EMBED_SHA256 = "ad4961a479dedbeb7c7d113253f8db1b1935586b73c27488712beec4f2c894e6"
CACHE = Path(os.environ["LOCALAPPDATA"]) / "Aruba2930FBuild" / PYTHON
ARCHIVE = CACHE / "python-embed.zip"
RUNTIME = CACHE / "runtime"
REQUIREMENTS = ROOT / "requirements-runtime.txt"
ISOLATED_PATH = "python314.zip\n.\npackages\n..\n../core\n"
DEV_NAMES = {
    "test",
    "tests",
    "testdata",
    "fixture",
    "fixtures",
    "sample",
    "samples",
    "example",
    "examples",
    "demo",
    "demos",
    "mock",
    "mocks",
    "synthetic",
    "benchmarks",
}
COMMENT_BLOCK = re.compile(rb"(?m)^(?:[ \t]*#[^\n]*(?:\n|$))+")
ATTRIBUTION = re.compile(
    rb"copyright|SPDX-|licensed under|[Aa]uthors?\s*:|[Ll]icense\s*:|\xc2\xa9", re.I
)
SAMPLE_COMMENT = re.compile(
    rb"(?im)^[ \t]*#[^\n]*(?:\b(?:example|sample)\s*:|\b(?:\d{1,3}\.){3}\d{1,3}\b)"
)
DEVICE_EXAMPLE = re.compile(
    r"(?im)(?<![\w/<])(?:SSH@)?[a-z][\w.@:/-]{0,70}(?:\([\w./-]+\))?[>#]"
    r"(?:[ \t]*$|[ \t]*(?:show|display|login|enable|conf(?:ig)?|terminal|copy|write)\b)"
    # ADVA FSP drivers document configuration sessions with arrow prompts, command
    # blocks and sample network element paths instead of a shell '#' or '>'.
    r"|^[ \t]*(?:[\w.:-]+-->[ \t]*\w|#CLI:PORT\b|configure (?:snmp|system|communication)[ \t]*$)"
    r"|^[ \t]*>>>|\b(?:\d{1,3}\.){3}\d{1,3}\b|make_test_card"
)
# These pinned Rich modules have optional standalone demonstrations with generated
# values. File-oriented formatters (JSON, Markdown, syntax, ANSI) stay executable.
RICH_DEMO_MODULES = frozenset(
    [
        "__init__",
        "_log_render",
        "_ratio",
        "_win32_console",
        "_wrap",
        "abc",
        "align",
        "console",
        "highlighter",
        "layout",
        "live",
        "logging",
        "markup",
        "padding",
        "pager",
        "panel",
        "pretty",
        "progress",
        "progress_bar",
        "prompt",
        "repr",
        "scope",
        "segment",
        "spinner",
        "status",
        "styled",
        "table",
        "text",
        "traceback",
        "tree",
    ]
)
VENDOR_FIXTURES = {"rich/__main__.py", "ntc_templates/templates/bogus_for_testing.textfsm"}
APP_TYPES = {"core": {".py"}, "localweb": {".py"}, "static": {".html", ".css", ".js"}}
APP_FILES = (
    "server.py",
    "Start-Local-Web.cmd",
    "Enable-LAN-Firewall.ps1",
    "README.md",
    "FEATURES_KO.md",
    "LICENSE",
    "requirements-runtime.txt",
)
# Build-time signatures from the retired port matrix and log viewer.
DEMO_MARKERS = tuple(
    marker.encode()
    for marker in (
        "서울대연구소",
        "서울R&D",
        "SW-SNU-",
        "SW-RND-",
        "SW-TP-",
        "교육동 2930F",
        "getExampleSnapshot",
        "demoSource",
        "resetDemo",
        "fixture-ap",
    )
)


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def prepare():
    if sys.version_info[:2] != (3, 14) or struct.calcsize("P") != 8:
        raise RuntimeError("Use Windows x64 Python 3.14 to build.")
    CACHE.mkdir(parents=True, exist_ok=True)
    marker = CACHE / "ready"
    if (
        marker.exists()
        and marker.read_text() == digest(REQUIREMENTS)
        and (RUNTIME / "python.exe").is_file()
    ):
        (RUNTIME / "python314._pth").write_text(ISOLATED_PATH, encoding="ascii")
        return
    if not ARCHIVE.exists():
        url = f"https://www.python.org/ftp/python/{PYTHON}/python-{PYTHON}-embed-amd64.zip"
        part = ARCHIVE.with_suffix(".part")
        with urlopen(url, timeout=90) as source, part.open("wb") as out:
            shutil.copyfileobj(source, out)
        part.replace(ARCHIVE)
    if digest(ARCHIVE) != EMBED_SHA256:
        raise RuntimeError("Unexpected digest for the CPython redistributable.")
    with TemporaryDirectory(dir=CACHE) as tmp:
        dest = Path(tmp) / "runtime"
        with ZipFile(ARCHIVE) as original:
            original.extractall(dest)
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--index-url",
                "https://pypi.org/simple",
                "--disable-pip-version-check",
                "--only-binary=:all:",
                "--no-compile",
                "--target",
                str(dest / "packages"),
                "-r",
                str(REQUIREMENTS),
            ],
            check=True,
        )
        (dest / "python314._pth").write_text(ISOLATED_PATH, encoding="ascii")
        subprocess.run(
            [dest / "python.exe", "-B", "-c", "import netmiko,paramiko,openpyxl,sqlite3"],
            check=True,
        )
        if RUNTIME.exists():
            if RUNTIME.resolve() != CACHE.resolve() / "runtime":
                raise RuntimeError("Unexpected build cache")
            shutil.rmtree(RUNTIME)
        dest.replace(RUNTIME)
    marker.write_text(digest(REQUIREMENTS))


def dev_payload(path):
    return bool(DEV_NAMES.intersection(part.lower() for part in path.parts[:-1]))


def template_rules(data):
    # Some upstream comments include full sample CLI transcripts. TextFSM ignores
    # whole-line comments; preserve intact any comment block with an attribution.
    return COMMENT_BLOCK.sub(lambda match: match[0] if ATTRIBUTION.search(match[0]) else b"", data)


def driver_source(data):
    """Remove sample transcript paragraphs and comments without editing executable expressions."""
    lines = data.splitlines(keepends=True)
    offsets = list(accumulate(map(len, lines), initial=0))
    edits = []
    for expr in ast.walk(ast.parse(data)):
        if not (
            isinstance(expr, ast.Expr)
            and isinstance(expr.value, ast.Constant)
            and isinstance(expr.value.value, str)
        ):
            continue
        doc = expr.value.value
        paragraphs = re.split(r"\n[ \t]*\n", doc)
        kept = [
            part
            for part in paragraphs
            if not DEVICE_EXAMPLE.search(part) or ATTRIBUTION.search(part.encode())
        ]
        if len(kept) != len(paragraphs):
            start = offsets[expr.lineno - 1] + expr.col_offset
            end = offsets[expr.end_lineno - 1] + expr.end_col_offset
            edits.append((start, end, repr("\n\n".join(kept)).encode()))
    for token in tokenize.tokenize(io.BytesIO(data).readline):
        if token.type != tokenize.COMMENT or not DEVICE_EXAMPLE.search(token.string):
            continue
        encoded = token.string.encode()
        if not ATTRIBUTION.search(encoded):
            line, col = token.start
            start = offsets[line - 1] + len(lines[line - 1].decode()[:col].encode())
            edits.append((start, start + len(encoded), b"#"))
    for start, end, replacement in sorted(edits, reverse=True):
        data = data[:start] + replacement + data[end:]
    return data


def rich_source(data, module):
    if module in RICH_DEMO_MODULES:
        # A pinned package may change over time: remove only the AST span of its
        # standalone entry point, never functions or statements that follow it.
        lines = data.splitlines(keepends=True)
        for node in reversed(ast.parse(data).body):
            if isinstance(node, ast.If) and ast.unparse(node.test) == "__name__ == '__main__'":
                del lines[node.lineno - 1 : node.end_lineno]
        data = b"".join(lines).rstrip() + b"\n"
    if module == "console":
        data = driver_source(data)  # Also remove the documentation for the omitted test card.
    return data


def runtime_content(path, rel):
    """Return replacement bytes for vendor documentation that contains synthetic observations."""
    if rel.parent.name.startswith("netmiko-") and rel.name == "METADATA":
        original = path.read_bytes()
        start = original.index(b"### Getting Started:")
        end = original.index(b"## API-Documentation", start)
        return original[:start] + original[end:]
    if rel.suffix == ".py" and rel.parts[0] in {"netmiko", "rich"}:
        original = path.read_bytes()
        filtered = (
            driver_source(original)
            if rel.parts[0] == "netmiko"
            else rich_source(original, rel.stem)
        )
        if filtered != original:
            return filtered
    if rel.suffix == ".textfsm" and rel.parts[:2] == ("ntc_templates", "templates"):
        original = path.read_bytes()
        rules = template_rules(original)
        if rules != original:
            return rules
    return None


def write_runtime(archive, root):
    records = []
    updates = {}
    for file in sorted(root.rglob("*")):
        if not file.is_file() or "__pycache__" in file.parts or file.suffix == ".pyc":
            continue
        rel = file.relative_to(root)
        member = "runtime/" + rel.as_posix()
        if rel.parts[0] == "packages":
            package_rel = PurePosixPath(*rel.parts[1:])
            if dev_payload(package_rel) or package_rel.as_posix() in VENDOR_FIXTURES:
                updates[package_rel.as_posix()] = None
                continue
            if file.name == "RECORD" and file.parent.name.endswith(".dist-info"):
                records.append((file, member))
                continue
            data = runtime_content(file, package_rel)
            if data is not None:
                archive.writestr(member, data)
                digest64 = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=")
                updates[package_rel.as_posix()] = ("sha256=" + digest64.decode(), str(len(data)))
                continue
        archive.write(file, member)
    for file, member in records:
        output = io.StringIO(newline="")
        writer = csv.writer(output, lineterminator="\n")
        with file.open(encoding="utf-8", newline="") as stream:
            for name, checksum, size in csv.reader(stream):
                row = updates.get(name, (checksum, size))
                if row is not None:
                    writer.writerow((name, *row))
        archive.writestr(member, output.getvalue())


def check_payload(path):
    """Inspect finished ZIP entries, including files not used by the initial screen."""
    with ZipFile(path) as archive:
        for name in archive.namelist():
            p = PurePosixPath(name)
            base = p.parts[0]
            if dev_payload(p) or (
                base != "runtime" and base not in APP_TYPES and name not in APP_FILES
            ):
                raise ValueError(f"Development or operational data in release: {name}")
            if name.removeprefix("runtime/packages/") in VENDOR_FIXTURES:
                raise ValueError(f"Vendor demonstration or test fixture in release: {name}")
            if name in {"static/csv.html", "core/aruba2930f_backup/poe_query_report.py"}:
                raise ValueError(f"Retired application asset: {name}")
            if base in APP_TYPES:
                tokens = re.split(r"[._-]", p.stem.lower())
                if p.suffix.lower() not in APP_TYPES[base] or DEV_NAMES.intersection(tokens):
                    raise ValueError(f"Unexpected application asset: {name}")
            if base == "runtime" and p.suffix not in {".py", ".textfsm"} and p.name != "METADATA":
                continue
            data = archive.read(name)
            if any(x in data for x in DEMO_MARKERS):
                raise ValueError(f"Retired synthetic data in release: {name}")
            if (
                p.name == "METADATA"
                and p.parent.name.startswith("netmiko-")
                and b"cisco_881 = {" in data
            ):
                raise ValueError(f"Sample device inventory in {name}")
            if (
                p.parts[:3] == ("runtime", "packages", "netmiko")
                and p.suffix == ".py"
                and driver_source(data) != data
            ):
                raise ValueError(f"Example device transcript in {name}")
            if (
                p.parts[:3] == ("runtime", "packages", "rich")
                and p.suffix == ".py"
                and rich_source(data, p.stem) != data
            ):
                raise ValueError(f"Unfiltered console demonstration in {name}")
            if p.suffix == ".textfsm" and SAMPLE_COMMENT.search(data):
                raise ValueError(f"Sample CLI output in parser comments: {name}")


def package(output):
    output.mkdir(parents=True, exist_ok=True)
    name = f"Aruba2930F_Local_Web_v{VERSION}_Windows_x64"
    final = output / f"{name}.zip"
    with TemporaryDirectory(dir=output) as work:
        temp = Path(work) / final.name
        with ZipFile(temp, "w", ZIP_DEFLATED, compresslevel=6) as archive:
            for base in APP_TYPES:
                source = ROOT / base
                for file in sorted(source.rglob("*")):
                    if file.is_file() and "__pycache__" not in file.parts and file.suffix != ".pyc":
                        archive.write(file, f"{base}/{file.relative_to(source).as_posix()}")
            write_runtime(archive, RUNTIME)
            for filename in APP_FILES:
                archive.write(ROOT / filename, filename)
        check_payload(temp)
        temp.replace(final)
    final.with_suffix(".sha256").write_text(f"{digest(final)}  {final.name}\n", encoding="ascii")
    print(final, flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--output", type=Path, default=ROOT.parent / "releases")
    args = parser.parse_args()
    prepare()
    if not args.prepare_only:
        package(args.output)
