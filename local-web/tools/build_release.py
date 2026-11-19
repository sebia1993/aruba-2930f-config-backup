"""Build the self-contained CPython/Windows release on a Windows x64 build host."""

import argparse
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
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from urllib.request import urlopen
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]
VERSION = "1.3.1"
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
APP_TYPES = {"core": {".py"}, "localweb": {".py"}, "static": {".html", ".css", ".js"}}
APP_FILES = (
    "server.py",
    "Start-Local-Web.cmd",
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


def runtime_content(path, rel):
    """Return replacement bytes for vendor documentation that contains synthetic observations."""
    if rel.parent.name.startswith("netmiko-") and rel.name == "METADATA":
        original = path.read_bytes()
        start = original.index(b"### Getting Started:")
        end = original.index(b"## API-Documentation", start)
        return original[:start] + original[end:]
    if rel.as_posix() == "rich/highlighter.py":
        original = path.read_bytes()
        # The pinned Rich module ends with a standalone address/JSON demonstration.
        start = original.index(b'\nif __name__ == "__main__":')
        return original[:start].rstrip() + b"\n"
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
            if dev_payload(package_rel):
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
                name == "runtime/packages/rich/highlighter.py"
                and b'if __name__ == "__main__"' in data
            ):
                raise ValueError(f"Address demonstration in {name}")
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
