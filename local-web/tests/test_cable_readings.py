"""Port-scoped 2930F cable readings and non-measurement rows."""

import unittest

from localweb.cable import parse_result


class CableReadingTests(unittest.TestCase):
    results = "1 1-2 OK 2m\n 3-6 OK 3m\n 4-5 Inter-short 1m\n 7-8 Open 0m"

    def test_module_port_notice_is_scoped_to_its_full_port_name(self):
        notice = "The transceiver on port 1/A1 does not support cable diagnostics."
        for text in [notice + "\n" + self.results, self.results + "\n" + notice]:
            with self.subTest(text=text):
                self.assertEqual(parse_result(text, "1")["status"], "complete")
                self.assertEqual(parse_result(text, "1/A1")["status"], "unsupported")

    def test_global_warning_after_foreign_table_block_prevents_false_completion(self):
        text = self.results + "\n2 1-2 OK 73m\n 3-6 OK 73m\n"
        self.assertEqual(parse_result(text + "Warning: test incomplete", "1")["status"], "pending")
        self.assertEqual(
            parse_result(text + "Warning: Test incomplete on port 2", "1")["status"], "complete"
        )
        self.assertEqual(parse_result(text + "Warning: unexpected status", "1")["status"], "failed")

    def test_selected_port_prefixed_failure_overrides_a_complete_looking_table(self):
        for notice in ("Error: test failed", "Warning: invalid reading"):
            with self.subTest(notice=notice):
                self.assertEqual(
                    parse_result(self.results + "\nPort 1: " + notice, "1")["status"], "failed"
                )
                self.assertEqual(
                    parse_result(self.results + "\nPort 2: " + notice, "1")["status"], "complete"
                )

    def test_pending_and_unavailable_pair_rows_are_never_measurements(self):
        for status in (
            "Pending --",
            "Not tested N/A",
            "Unknown --",
            "In progress -",
            "Test failed --",
            "Untested N/A",
        ):
            with self.subTest(status=status):
                text = f"1 1-2 OK 0m\n 3-6 {status}\n 4-5 Short 1m\n 7-8 Open 0m"
                result = parse_result(text, "1")
                self.assertEqual(result["status"], "partial")
                self.assertEqual([p["pair"] for p in result["pairs"]], ["1-2", "4-5", "7-8"])
                self.assertEqual(result["unresolved"], ["3-6"])
                self.assertEqual(result["pairs"][0]["distance"], "0m")

    def test_no_result_notice_withdraws_measurements_for_this_port_only(self):
        msg = "No cable diagnostic results available for port 1"
        self.assertIs(parse_result(msg, "1").get("invalidatesPairs"), True)
        self.assertNotIn(
            "invalidatesPairs",
            parse_result(self.results + "\n" + msg.replace("port 1", "port 2"), "1"),
        )
