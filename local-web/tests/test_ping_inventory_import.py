"""Exercise clipboard-only device inventory previews."""

import unittest

from localweb.ping_inventory_import import InventoryInputError, preview_inventory


class InventoryImportTests(unittest.TestCase):
    def test_reordered_clipboard_with_user_headers_and_blank_destinations(self):
        payload = {
            "text": "\ufeffBB\t유지보수 담당\tNMS server\t장비hostanme\tSW IP\tGW IP\r\n"
            "198.51.100.7\t무시\t\t지사-1\t192.0.2.1\t198.51.100.7\r\n"
            "\r\n"
            "\t\t198.51.100.8\t\t192.0.2.2\t\r\n"
        }
        result = preview_inventory(payload)
        self.assertEqual(result["errors"], [])
        self.assertEqual(
            result["mapping"], {"hostname": 3, "swIp": 4, "gwIp": 5, "bbIp": 0, "nmsIp": 2}
        )
        self.assertEqual(
            result["rows"],
            [
                dict(
                    rowNumber=2,
                    hostname="지사-1",
                    swIp="192.0.2.1",
                    gwIp="198.51.100.7",
                    bbIp="198.51.100.7",
                    nmsIp="",
                    errors=[],
                ),
                dict(
                    rowNumber=4,
                    hostname="",
                    swIp="192.0.2.2",
                    gwIp="",
                    bbIp="",
                    nmsIp="198.51.100.8",
                    errors=[],
                ),
            ],
        )

    def test_manual_mapping_recovers_unrecognized_or_ambiguous_headings(self):
        payload = {"text": "관리주소\t대상 A\tGW IP\tGW_IP\n192.0.2.1\t198.51.100.10\t\t"}
        initial = preview_inventory(payload)
        self.assertIsNone(initial["mapping"]["swIp"])
        self.assertIsNone(initial["mapping"]["gwIp"])
        self.assertTrue(initial["errors"])
        corrected = preview_inventory({**payload, "mapping": {"swIp": 0, "gwIp": 1}})
        self.assertFalse(corrected["errors"])
        self.assertEqual(corrected["rows"][0]["gwIp"], "198.51.100.10")
        self.assertFalse(corrected["rows"][0]["errors"])
        self.assertTrue(
            preview_inventory({**payload, "mapping": {"swIp": 0, "gwIp": None}})["errors"]
        )
        self.assertTrue(preview_inventory({**payload, "mapping": {"swIp": 0, "gwIp": 0}})["errors"])
        for bad in (True, "0", -1, 4):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                preview_inventory({**payload, "mapping": {"swIp": bad}})

    def test_source_row_and_column_errors_and_unmeasured_rows(self):
        result = preview_inventory(
            {
                "text": "SW IP\tGW IP\tBB\tHostname\n"
                "192.0.2.5\t198.51.100.7\tbad-ip\t가\n"
                "192.0.2.5\t\t\t나\n"
                "\t127.0.0.1\t\t다\n"
                "192.0.2.6\t\t\t라"
            }
        )
        rows = result["rows"]
        self.assertFalse(result["errors"])
        self.assertTrue(any("2행" in e and "BB" in e for e in rows[0]["errors"]))
        for row in rows[:2]:
            self.assertTrue(any("SW IP" in e and "중복" in e for e in row["errors"]))
        self.assertTrue(any("SW IP" in e for e in rows[2]["errors"]))
        self.assertTrue(any("GW IP" in e for e in rows[2]["errors"]))
        self.assertEqual(rows[3]["errors"], [])

    def test_file_and_mixed_requests_are_rejected(self):
        table = "SW IP\tGW IP\n192.0.2.1\t198.51.100.1"
        upload = {"filename": "private-site.xlsx", "content": "private-content"}
        for payload in (
            upload,
            {**upload, "text": table},
            {"text": table, "sheet": "private-site"},
            {"text": table, "filename": "private-site.xlsx"},
        ):
            with self.subTest(keys=list(payload)):
                with self.assertRaisesRegex(InventoryInputError, "붙여넣") as error:
                    preview_inventory(payload)
                self.assertNotIn("private", str(error.exception))
        self.assertEqual(
            set(preview_inventory({"text": table})), {"headers", "rows", "errors", "mapping"}
        )

    def test_clipboard_formulas_flag_their_rows_and_columns(self):
        preview = preview_inventory({"text": "장비 Hostname\tSW IP\tBB\n=1+1\t192.0.2.1\t=1+2"})
        errors = preview["rows"][0]["errors"]
        self.assertEqual(len(errors), 2)
        self.assertTrue(any("2행 장비 Hostname" in error for error in errors))
        self.assertTrue(any("2행 BB" in error for error in errors))
        self.assertNotIn("=1+1", repr(preview))

    def test_clipboard_input_bounds(self):
        table = "SW IP\tBB\n192.0.2.1\t198.51.100.1"
        for payload in (
            {},
            {"text": {"raw": table}},
            {"text": "a\t" * 81 + "b\n1"},
            {"text": "a\n" * 5002},
            {"text": "한" * (1024**2 + 1)},
            {"text": '\t"bad quote'},
            {"text": table, "mapping": {"extra": 0}},
        ):
            with self.subTest(keys=list(payload)), self.assertRaises(InventoryInputError):
                preview_inventory(payload)
