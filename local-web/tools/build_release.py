"""Build the self-contained CPython/Windows release on a Windows x64 build host."""

import argparse
import hashlib
import os
import shutil
import struct
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.request import urlopen
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]
VERSION = "1.3.0"
PYTHON = "3.14.3"
EMBED_SHA256 = "ad4961a479dedbeb7c7d113253f8db1b1935586b73c27488712beec4f2c894e6"
CACHE = Path(os.environ["LOCALAPPDATA"]) / "Aruba2930FBuild" / PYTHON
ARCHIVE = CACHE / "python-embed.zip"
RUNTIME = CACHE / "runtime"
REQUIREMENTS = ROOT / "requirements-runtime.txt"
ISOLATED_PATH = "python314.zip\n.\npackages\n..\n../core\n"


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


def package(output):
    output.mkdir(parents=True, exist_ok=True)
    name = f"Aruba2930F_Local_Web_v{VERSION}_Windows_x64"
    final = output / f"{name}.zip"
    temp = final.with_suffix(".partial")
    with ZipFile(temp, "w", ZIP_DEFLATED, compresslevel=6) as archive:
        for source, base in [
            (ROOT / "core", "core"),
            (ROOT / "localweb", "localweb"),
            (ROOT / "static", "static"),
            (RUNTIME, "runtime"),
        ]:
            for file in sorted(source.rglob("*")):
                if file.is_file() and "__pycache__" not in file.parts and file.suffix != ".pyc":
                    archive.write(file, f"{base}/{file.relative_to(source).as_posix()}")
        for filename in (
            "server.py",
            "Start-Local-Web.cmd",
            "README.md",
            "FEATURES_KO.md",
            "LICENSE",
            "requirements-runtime.txt",
        ):
            archive.write(ROOT / filename, filename)
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
