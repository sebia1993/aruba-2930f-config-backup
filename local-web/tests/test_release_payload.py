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
            ("static/csv.html", b"<script>" + DEMO_MARKERS[0] + b"</script>"),
            ("static/logs.html", b"const " + DEMO_MARKERS[-2] + b" = 0;"),
            ("static/demo-data.js", b"const rows = [];"),
            ("core/mock_devices.py", b"rows = []"),
            ("localweb/inventory.csv", b""),
            ("runtime/packages/testdata/default_index", b""),
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
        record = "textfsm-2.1.0.dist-info/RECORD"
        bare_files = {
            template: license_header + b"\n# Example: output\n" + rules,
            metadata: metadata_header + cli_sample + api_docs,
            "rich/highlighter.py": module + b'\nif __name__ == "__main__":\n    print(1)\n',
            "testdata/default_index": b"",
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
            self.assertEqual(z.read(prefix + "LICENSE"), b"license content")
            rows = {
                name: (h, n)
                for name, h, n in csv.reader(io.StringIO(z.read(prefix + record).decode()))
            }
            self.assertNotIn("testdata/default_index", rows)
            for name in (template, metadata, "rich/highlighter.py"):
                data = z.read(prefix + name)
                h = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip("=")
                self.assertEqual(rows[name], ("sha256=" + h, str(len(data))))
