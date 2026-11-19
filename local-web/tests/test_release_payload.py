import base64
import csv
import hashlib
import io
import tempfile
import unittest
from pathlib import Path
from zipfile import ZipFile

from tools.build_release import DEMO_MARKERS, check_payload, write_runtime


class ReleasePayloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.zip = self.root / "release.zip"

    def test_finished_zip_rejects_retired_code_and_unexpected_datasets(self):
        for name, content in (
            ("static/matrix.html", b"<script>" + DEMO_MARKERS[0] + b"</script>"),
            ("static/csv.html", b""),
            ("core/aruba2930f_backup/poe_query_report.py", b""),
            ("static/logs.html", b""),
            ("static/demo-data.js", b"const rows = [];"),
            ("core/mock_devices.py", b"rows = []"),
            ("localweb/inventory.csv", b""),
            ("runtime/packages/testdata/default_index", b""),
            ("runtime/packages/ntc_templates/templates/bogus_for_testing.textfsm", b""),
            ("runtime/packages/rich/__main__.py", b"def make_test_card(): pass"),
            ("runtime/packages/rich/live.py", b'if __name__ == "__main__":\n    print(1)\n'),
            (
                "runtime/packages/netmiko/vendor/driver.py",
                b'def port():\n    """Lab-switch#show vlan"""\n    return 1\n',
            ),
            (
                "runtime/packages/netmiko/adva/driver.py",
                b'"""Lab-router:--> home"""\nPROMPT_PATTERN = r"-->$"\n',
            ),
            ("tools/build_release.py", b""),
        ):
            with self.subTest(name=name):
                with ZipFile(self.zip, "w") as z:
                    z.writestr(name, content)
                with self.assertRaisesRegex(ValueError, name):
                    check_payload(self.zip)

    def test_vendor_test_data_removed_licenses_rules_and_wheel_hashes_preserved(self):
        runtime = self.root / "runtime"
        packages = runtime / "packages"
        template = "ntc_templates/templates/cable_test.textfsm"
        metadata = "netmiko-4.6.0.dist-info/METADATA"
        license_header = b"# SPDX-License-Identifier: MIT\n# Author: upstream\n"
        rules = b"Value Port (\\S+)\n\nStart\n  ^${Port} -> Record\n"
        metadata_header = b"Name: netmiko\nVersion: 4.6.0\n\n"
        cli_sample = b"### Getting Started:\ncisco_881 = {}\n"
        api_docs = b"## API-Documentation\n"
        module = b"class ReprHighlighter: pass\n"
        file_cli = b'if __name__ == "__main__":\n    import sys\n    print(sys.stdin.read())\n'
        driver = (
            b'PROMPT = "switch#"\nPROMPT_PATTERN = r"-->$"\n\ndef port():\n'
            b'    """Read the port.\n\n    SSH@Lab-switch#show vlan\n\n'
            b"    home\n    configure snmp\n    add v3user unit_user\n\n"
            b'    Lab-router:--> home\n    """\n    return PROMPT\n'
        )
        excluded = (
            "testdata/default_index",
            "rich/__main__.py",
            "ntc_templates/templates/bogus_for_testing.textfsm",
        )
        record = "textfsm-2.1.0.dist-info/RECORD"
        bare_files = {
            template: license_header + b"\n# Example: output\n" + rules,
            metadata: metadata_header + cli_sample + api_docs,
            "rich/highlighter.py": module + b'\nif __name__ == "__main__":\n    print(1)\n',
            "rich/json.py": file_cli,
            "netmiko/vendor/driver.py": license_header + driver,
            **dict.fromkeys(excluded, b""),
            "LICENSE": b"license content",
        }
        files = {
            **bare_files,
            record: "".join(f"{name},,\n" for name in [*bare_files, record]).encode(),
        }
        for name, data in files.items():
            path = packages / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        with ZipFile(self.zip, "w") as z:
            write_runtime(z, runtime)
        check_payload(self.zip)
        prefix = "runtime/packages/"
        with ZipFile(self.zip) as z:
            self.assertNotIn(prefix + "testdata/default_index", z.namelist())
            self.assertEqual(z.read(prefix + template), license_header + b"\n" + rules)
            self.assertEqual(z.read(prefix + metadata), metadata_header + api_docs)
            self.assertEqual(z.read(prefix + "rich/highlighter.py"), module)
            self.assertEqual(z.read(prefix + "rich/json.py"), file_cli)
            result = z.read(prefix + "netmiko/vendor/driver.py")
            self.assertNotIn(b"Lab-switch", result)
            self.assertNotIn(b"Lab-router", result)
            self.assertNotIn(b"unit_user", result)
            self.assertIn(b"Read the port.", result)
            self.assertTrue(result.startswith(license_header))
            self.assertIn(b'PROMPT = "switch#"', result)
            self.assertIn(b'PROMPT_PATTERN = r"-->$"', result)
            self.assertIn(b"return PROMPT", result)
            compile(result, "driver.py", "exec")
            self.assertEqual(z.read(prefix + "LICENSE"), b"license content")
            rows = {
                name: (h, n)
                for name, h, n in csv.reader(io.StringIO(z.read(prefix + record).decode()))
            }
            for name in excluded:
                self.assertNotIn(name, rows)
                self.assertNotIn(prefix + name, z.namelist())
            for name in (template, metadata, "rich/highlighter.py", "netmiko/vendor/driver.py"):
                data = z.read(prefix + name)
                h = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip("=")
                self.assertEqual(rows[name], ("sha256=" + h, str(len(data))))
