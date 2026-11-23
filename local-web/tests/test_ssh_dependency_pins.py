"""Block accidental SSH dependency upgrades until compatibility is reviewed."""

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DESKTOP_LOCK = ROOT.parent / "requirements-lock.txt"
WEB_LOCK = ROOT / "requirements-runtime.txt"


def paramiko_pin(path: Path) -> str:
    entries = [
        line.strip().split("==", 1)[1]
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip().lower().startswith("paramiko==")
    ]
    if len(entries) != 1:
        raise AssertionError(f"Expected exactly one Paramiko pin in {path.name}")
    return entries[0]


class SSHDependencyPinTests(unittest.TestCase):
    def test_desktop_and_web_keep_the_reviewed_paramiko_version(self):
        # Paramiko 5 changes SHA-1-based KEX support. Upgrade only with a
        # targeted SSH negotiation and host-key regression review.
        self.assertEqual(paramiko_pin(DESKTOP_LOCK), "4.0.0")
        self.assertEqual(paramiko_pin(WEB_LOCK), paramiko_pin(DESKTOP_LOCK))


if __name__ == "__main__":
    unittest.main()
