"""Parse physical link from the AOS-S status column, including fixed-width port names."""

import unittest

from verify_http_ssh import interface_status_table

from aruba2930f_backup.port_control import parse_interface_links


class InterfaceLinksTests(unittest.TestCase):
    def test_korean_name_padding_never_changes_which_token_supplies_link(self):
        for terminal_width in (True, False):
            with self.subTest(terminal_width=terminal_width):
                lines = interface_status_table(
                    [("1", "연구실 Down", "Up"), ("2/1", "연구실 Up", "Down")],
                    terminal_width=terminal_width,
                )
                self.assertEqual(
                    parse_interface_links("\n".join(lines)), {"1": "Up", "2/1": "Down"}
                )

    def test_named_blank_and_wrapped_port_names_do_not_supply_the_status(self):
        lines = interface_status_table(
            [
                ("1", "Down Up", "Up"),
                ("10", "Up", "Down"),
                ("2/1", "", "Up"),
            ]
        )
        # Continuation of a wrapped, printable name, aligned only in the Name column.
        lines.insert(3, "          Down")
        self.assertEqual(
            parse_interface_links("\r\n".join(lines)),
            {
                "1": "Up",
                "10": "Down",
                "2/1": "Up",
            },
        )

    def test_missing_duplicate_or_non_status_values_never_produce_a_link(self):
        lines = interface_status_table(
            [
                ("1", "Down", ""),
                ("2", "Up", "Unknown"),
                ("3", "", "Up"),
                ("3", "", "Down"),
                ("4", "", "Down"),
                ("11", "", "Down"),
            ]
        )
        self.assertEqual(parse_interface_links("\n".join(lines)), {"4": "Down", "11": "Down"})

    def test_wrong_table_or_wrong_status_header_never_supplies_a_link(self):
        lines = interface_status_table([("1", "", "Up")])
        lines[0] = lines[0].replace("Status", "Duplex")
        for output in (
            "\n".join(lines),
            "Port Type | Alert Enabled Status Mode\n1 100/1000T | No Yes Down Auto",
        ):
            with self.subTest(output=output):
                self.assertEqual(parse_interface_links(output), {})
