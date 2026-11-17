"""Ensure repository secret checks never silently omit large text files."""

from __future__ import annotations

from pathlib import Path

import pytest
from tools import check_no_secrets as secret_scan


def test_oversized_text_cannot_bypass_secret_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "oversized-source.md"
    # Keep this synthetic address split in source so the repository safety
    # scanner does not flag its own test fixture as a real internal IP.
    private_address = b"192" + b".168.50.42\n"
    filler = (b"harmless " + b"x" * 1016 + b"\n") * 2100
    path.write_bytes(filler + private_address)
    assert path.stat().st_size > secret_scan.MAX_TEXT_BYTES

    monkeypatch.setattr(secret_scan, "ROOT", tmp_path)
    monkeypatch.setattr(secret_scan, "_repository_files", lambda: [path])

    assert secret_scan.main() == 1
    assert "oversized-source.md" in capsys.readouterr().err


def test_oversized_binary_images_stay_exempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = tmp_path / "large.png"
    binary.write_bytes(b"\x89PNG" + b"\x00" * (secret_scan.MAX_TEXT_BYTES + 1))
    monkeypatch.setattr(secret_scan, "ROOT", tmp_path)
    monkeypatch.setattr(secret_scan, "_repository_files", lambda: [binary])
    assert secret_scan.main() == 0
