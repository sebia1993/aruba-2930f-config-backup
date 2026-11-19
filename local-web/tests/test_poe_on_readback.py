"""Regression checks for a PoE ON write whose acknowledgement or observation is lost."""

import json
import tempfile
import unittest
from dataclasses import replace
from functools import partial
from itertools import chain, count, repeat
from pathlib import Path
from threading import Event
from unittest.mock import Mock, call, patch
from uuid import uuid4

from aruba2930f_backup.batch_port_control import (
    BatchPlan,
    BatchPortController,
    BatchStatus,
    BatchTarget,
)
from aruba2930f_backup.models import (
    CollectionFailure,
    CollectionOptions,
    Credentials,
    DeviceTarget,
    ErrorCode,
)
from aruba2930f_backup.port_control import (
    PortAction,
    PortControlService,
    PortSnapshot,
    PreMutationRejected,
)
from aruba2930f_backup.port_control_ssh import PortControlSSHSession


class PoEOnReadbackTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.directory = Path(tmp.name)
        self.factory = Mock()
        self.service = PortControlService(
            host_key_store=Mock(), session_factory=self.factory, audit_directory=self.directory
        )
        self.session = Mock()
        self.creds = Credentials("test", "test")
        self.cancel = Event()
        self.before = PortSnapshot(
            DeviceTarget("192.0.2.42"),
            "1",
            "edge",
            "Aruba 2930F JL255A",
            True,
            "Down",
            False,
            "Disabled",
            0.0,
            "SHA256:pinned",
        )

    def apply_once(self, action=PortAction.POE_ON, before=None):
        return self.service._apply_once(
            self.session, before or self.before, action, self.creds, self.cancel
        )

    def test_still_off_after_fresh_read_reports_power_enable_and_preserves_observation(self):
        with (
            patch.object(self.service, "_read_snapshot", return_value=self.before),
            patch.object(self.service, "_read_again", return_value=self.before) as reread,
        ):
            result = self.apply_once()
        self.session.change_port.assert_called_once_with("1", "poe_on")
        reread.assert_called_once_with(self.before, self.creds, self.cancel)
        self.assertEqual(result.status, "unverified")
        self.assertIs(result.after, self.before)
        self.assertRegex(result.message, r"Power Enable.*No.*Yes")
        records = [json.loads(line) for line in result.audit_path.read_text("utf-8").splitlines()]
        self.assertEqual([row["status"] for row in records], ["pending", "unverified"])
        self.assertEqual(records[-1]["message"], result.message)

    def test_mismatched_identity_or_admin_state_cannot_verify_even_with_power_enabled(self):
        for changes in ({"interface": "2"}, {"fingerprint": "other"}, {"enabled": False}):
            with self.subTest(changes=changes):
                self.session.reset_mock()
                after = replace(self.before, poe_enabled=True, **changes)
                with (
                    patch.object(self.service, "_read_snapshot", return_value=after),
                    patch.object(self.service, "_read_again") as reread,
                ):
                    result = self.apply_once()
                self.session.change_port.assert_called_once_with("1", "poe_on")
                reread.assert_not_called()
                self.assertEqual(result.status, "unverified")
                self.assertIsNone(result.after)

    def test_rejected_write_cannot_be_promoted_by_another_read(self):
        self.session.change_port.side_effect = CollectionFailure(
            ErrorCode.COMMAND_REJECTED, "명령이 거부되었습니다."
        )
        with (
            patch.object(self.service, "_read_snapshot") as read,
            patch.object(self.service, "_read_again") as reread,
        ):
            result = self.apply_once()
        self.session.change_port.assert_called_once_with("1", "poe_on")
        read.assert_not_called()
        reread.assert_not_called()
        self.assertEqual(result.status, "unverified")
        self.assertIn(ErrorCode.COMMAND_REJECTED.value, result.message)

    def test_denial_received_before_missing_prompt_stops_batch_even_if_power_later_turns_on(self):
        command = "interface 1 power-over-ethernet"
        batch = BatchPortController(
            controller_factory=lambda: self.service, journal_directory=self.directory
        )
        targets = tuple(
            BatchTarget(
                self.before.target, port, self.before.hostname, True, False, self.before.fingerprint
            )
            for port in ("1", "2")
        )
        for termination in ("timeout", "EOF"):
            with self.subTest(termination=termination):
                channel = Mock()
                channel.normalize_cmd.side_effect = lambda text: text + "\n"
                channel.read_channel.side_effect = chain(
                    (
                        "configure terminal\r\nedge(config)#",
                        "",
                        command + "\r\nPermis",
                        "sion denied\r\n",
                    ),
                    repeat("") if termination == "timeout" else (EOFError(),),
                )
                adapter = PortControlSSHSession(
                    self.before.target,
                    self.creds,
                    CollectionOptions(command_timeout_seconds=1),
                    self.service.host_key_store,
                    clock=count(step=0.05).__next__,
                    sleeper=lambda _: None,
                )
                adapter._connection = channel
                self.factory.side_effect = [Mock(), adapter]
                with (
                    patch.object(adapter, "_require_prepared", return_value="edge#"),
                    patch.object(self.service, "_prepare"),
                    patch.object(
                        self.service,
                        "_trusted_key",
                        return_value=Mock(fingerprint=self.before.fingerprint),
                    ),
                    patch.object(self.service, "_read_snapshot", return_value=self.before) as read,
                    patch.object(
                        self.service,
                        "_read_again",
                        return_value=replace(self.before, poe_enabled=True),
                    ) as reread,
                ):
                    plan = BatchPlan(str(uuid4()), "", PortAction.POE_ON, targets)
                    result = batch.execute(plan, self.creds)
                self.assertEqual(
                    [row.status for row in result.results],
                    [BatchStatus.UNVERIFIED, BatchStatus.PAUSED],
                )
                self.assertIn(ErrorCode.COMMAND_REJECTED.value, result.results[0].message)
                self.assertEqual(
                    channel.write_channel.call_args_list,
                    [call("configure terminal\n"), call(command + "\n")],
                )
                self.assertEqual(read.call_count, 2)  # Inspection and same-session preflight only.
                reread.assert_not_called()

    def test_cancellation_after_write_blocks_probe_and_readback_authentication(self):
        def lost_response(*args):
            self.cancel.set()
            raise TimeoutError

        self.session.change_port.side_effect = lost_response
        with patch.object(self.service, "_trusted_key") as lookup:
            result = self.apply_once()
        self.session.change_port.assert_called_once_with("1", "poe_on")
        lookup.assert_not_called()
        self.factory.assert_not_called()
        self.assertEqual(result.status, "unverified")
        self.assertIn(ErrorCode.CANCELLED.value, result.message)

    def test_newly_trusted_key_must_also_match_the_original_port_snapshot(self):
        self.session.change_port.side_effect = TimeoutError()
        with patch.object(
            self.service, "_trusted_key", return_value=Mock(fingerprint="new")
        ) as key:
            result = self.apply_once()
        self.session.change_port.assert_called_once_with("1", "poe_on")
        key.assert_called_once_with(self.before.target, None)
        self.factory.assert_not_called()
        self.assertEqual(result.status, "unverified")
        self.assertIn(ErrorCode.HOST_KEY_CHANGED.value, result.message)

    def test_poe_off_does_not_use_poe_on_recovery(self):
        before = replace(self.before, poe_enabled=True)
        self.session.change_port.side_effect = TimeoutError()
        with patch.object(self.service, "_read_again") as reread:
            result = self.apply_once(PortAction.POE_OFF, before)
        self.session.change_port.assert_called_once_with("1", "poe_off")
        reread.assert_not_called()
        self.assertEqual(result.status, "unverified")

    def test_unprivileged_prompt_or_denied_config_entry_never_sends_port_command(self):
        for prompt, expected_calls in (
            ("edge>", []),
            ("Aruba 2930F Edge#", [call("configure terminal", "Aruba 2930F Edge(config)#")]),
        ):
            with self.subTest(prompt=prompt):
                adapter = Mock(spec=PortControlSSHSession)
                adapter._require_prepared.return_value = prompt
                adapter._exchange.side_effect = CollectionFailure(
                    ErrorCode.COMMAND_REJECTED, "denied"
                )
                self.session.change_port.side_effect = partial(
                    PortControlSSHSession.change_port, adapter
                )
                with (
                    patch.object(self.service, "_read_again") as reread,
                    self.assertRaises(PreMutationRejected),
                ):
                    self.apply_once()
                self.assertEqual(adapter._exchange.call_args_list, expected_calls)
                reread.assert_not_called()
        paths = list(self.directory.glob("*.jsonl"))
        self.assertEqual(len(paths), 2)
        for path in paths:
            records = [json.loads(line) for line in path.read_text("utf-8").splitlines()]
            self.assertEqual([row["status"] for row in records], ["pending", "preflight_failed"])
