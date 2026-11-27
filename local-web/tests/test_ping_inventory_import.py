"""Offline device inventory paste and workbook previews."""

import base64
import io
import unittest
from unittest.mock import patch
from zipfile import ZIP_DEFLATED, ZipFile

from localweb.ping_inventory_import import InventoryInputError, preview_inventory
from openpyxl import Workbook


def workbook_payload(data, second=None):
    book = Workbook()
    for row in data:
        book.active.append(row)
    if second:
        sheet = book.create_sheet("점검")
        for row in second:
            sheet.append(row)
    buffer = io.BytesIO()
    book.save(buffer)
    book.close()
    return {"filename": "장비표.xlsx", "content": base64.b64encode(buffer.getvalue()).decode()}


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

    def test_workbook_can_choose_sheet_and_rejects_formula_without_evaluation(self):
        payload = workbook_payload(
            [["SW IP", "GW IP"], ["192.0.2.1", "198.51.100.1"]],
            [["장비 hostname", "SW IP", "BB"], ["=1+1", "192.0.2.2", "=1+2"]],
        )
        first = preview_inventory(payload)
        self.assertEqual(first["sheets"], ["Sheet", "점검"])
        self.assertEqual(first["rows"][0]["errors"], [])
        second = preview_inventory({**payload, "sheet": "점검"})
        self.assertEqual(second["sheet"], "점검")
        self.assertEqual(second["rows"][0]["swIp"], "192.0.2.2")
        self.assertEqual(len(second["rows"][0]["errors"]), 2)
        self.assertNotIn("=1+1", repr(second))
        with self.assertRaises(ValueError):
            preview_inventory({**payload, "sheet": "없는 시트"})

    def test_library_errors_never_echo_imported_values(self):
        payload = workbook_payload([["SW IP", "BB"], ["192.0.2.2", "198.51.100.2"]])
        with (
            patch(
                "localweb.ping_analysis_nms.load_workbook",
                side_effect=ValueError("private cell content"),
            ),
            self.assertRaises(InventoryInputError) as caught,
        ):
            preview_inventory(payload)
        self.assertEqual(str(caught.exception), "Excel 파일을 읽지 못했습니다.")

    def test_input_bounds_and_unsafe_workbooks(self):
        # Some workbooks open on a cover sheet with no inventory data.
        cover = workbook_payload([], [["SW IP", "BB"], ["192.0.2.2", "198.51.100.2"]])
        preview = preview_inventory(cover)
        self.assertEqual(preview["sheets"], ["Sheet", "점검"])
        self.assertTrue(preview["errors"])
        self.assertFalse(preview_inventory({**cover, "sheet": "점검"})["errors"])
        table = "SW IP\tBB\n192.0.2.1\t198.51.100.1"
        for payload in (
            {},
            {"text": table, "content": "abc"},
            {"text": {"raw": table}},
            {"text": "a\t" * 81 + "b\n1"},
            {"text": "a\n" * 5002},
            {"text": '\t"bad quote'},
            {"text": table, "mapping": {"extra": 0}},
            {"filename": "장비.xlsm", "content": "aGVsbG8="},
            {"filename": "not-a-book.xlsx", "content": "not-base64!"},
        ):
            with self.subTest(keys=list(payload)), self.assertRaises(ValueError):
                preview_inventory(payload)
        for entry, data in [
            ("xl/vbaProject.bin", b"fake"),
            ("a.xml", b"<!DOCTYPE fake>"),
            ("a", b"x" * (20 * 1024**2 + 1)),
        ]:
            stream = io.BytesIO()
            with ZipFile(stream, "w", ZIP_DEFLATED) as archive:
                archive.writestr(entry, data)
            with self.subTest(entry=entry), self.assertRaises(ValueError):
                preview_inventory(
                    dict(
                        filename="unsafe.xlsx", content=base64.b64encode(stream.getvalue()).decode()
                    )
                )
