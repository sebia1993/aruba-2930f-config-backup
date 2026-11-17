"""Fail-closed checks for release requests submitted through Chat."""

from __future__ import annotations

from copy import deepcopy

import pytest
from tools.prepare_release import ReleasePreparationError
from tools.prepare_release_request import (
    BOOTSTRAP_ADDITIONS,
    REPO,
    validate_release_request,
)

PARENT = "a" * 40
HEAD = "b" * 40
TAG = "v0.4.0"
PATH = ".github/release-requests/v0.4.0.json"
REQUEST = {"schema": 1, "tag": TAG, "expected_source_main_sha": PARENT}


def check(**changes: object) -> str:
    values = {
        "request": deepcopy(REQUEST),
        "changes": {PATH: "A"},
        "parent_sha": PARENT,
        "event_sha": HEAD,
        "checkout_sha": HEAD,
        "event_name": "push",
        "ref": "refs/heads/main",
        "repository": REPO,
        "bootstrap": False,
    }
    values.update(changes)
    return validate_release_request(**values)  # type: ignore[arg-type]


def test_simple_request_releases_a_previously_reviewed_main() -> None:
    assert check() == TAG


def test_bootstrap_requires_only_workflow_helper_tests_and_request() -> None:
    files = {PATH} | set(BOOTSTRAP_ADDITIONS)
    assert check(bootstrap=True, changes={path: "A" for path in files}) == TAG
    with pytest.raises(ReleasePreparationError):
        check(bootstrap=True)


@pytest.mark.parametrize(
    "files",
    [
        {PATH: "M"},
        {PATH: "A", "src/aruba2930f_backup/ssh.py": "M"},
        {PATH: "A", ".github/release-requests/v0.4.1.json": "A"},
    ],
)
def test_unexpected_changes_fail_closed(files: dict[str, str]) -> None:
    with pytest.raises(ReleasePreparationError, match="unexpected"):
        check(changes=files)


@pytest.mark.parametrize(
    "changes",
    [
        {"event_name": "workflow_dispatch"},
        {"ref": "refs/heads/feature"},
        {"repository": "somebody/fork"},
        {"event_sha": PARENT},
        {"checkout_sha": PARENT},
        {"parent_sha": HEAD},
    ],
)
def test_untrusted_event_commit_or_repository_rejected(changes: dict[str, str]) -> None:
    with pytest.raises(ReleasePreparationError):
        check(**changes)


@pytest.mark.parametrize(
    "request",
    [
        {"schema": 1, "tag": TAG, "expected_source_main_sha": HEAD},
        {"schema": True, "tag": TAG, "expected_source_main_sha": PARENT},
        {"schema": 1, "tag": "broken", "expected_source_main_sha": PARENT},
        {"schema": 1, "tag": TAG, "expected_source_main_sha": PARENT, "command": "run"},
        None,
        [],
    ],
)
def test_invalid_request_is_rejected(request: object) -> None:
    with pytest.raises(ReleasePreparationError):
        check(request=request)
