from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest
from tools.prepare_release import ReleasePreparationError, prepare_release

COMMIT = "a" * 40
TAG_OBJECT = "b" * 40
OTHER_COMMIT = "c" * 40
TAG = "v0.2.0"


class FakeAPI:
    def __init__(self) -> None:
        self.main = COMMIT
        self.tag_ref: dict[str, Any] | None = None
        self.tag_object: dict[str, Any] | None = None
        self.release: dict[str, Any] | None = None
        self.draft_releases: list[dict[str, Any]] = []
        self.move_main_after_tag = False
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        allow_missing: bool = False,
    ) -> dict[str, Any] | list[dict[str, Any]] | None:
        self.calls.append((method, path, deepcopy(payload)))
        if method == "GET" and path == "git/ref/heads/main":
            return {"object": {"type": "commit", "sha": self.main}}
        if method == "GET" and path == f"releases/tags/{TAG}":
            assert allow_missing
            return self.release
        if method == "GET" and path.startswith("releases?per_page=100&page="):
            page = int(path.rsplit("=", maxsplit=1)[1])
            entries = ([] if self.release is None else [self.release]) + self.draft_releases
            return deepcopy(entries[(page - 1) * 100 : page * 100])
        if method == "GET" and path == f"git/ref/tags/{TAG}":
            if self.tag_ref is None:
                assert allow_missing
            return deepcopy(self.tag_ref)
        if method == "POST" and path == "git/tags":
            assert payload is not None
            self.tag_object = {
                "sha": TAG_OBJECT,
                "tag": payload["tag"],
                "object": {"type": payload["type"], "sha": payload["object"]},
            }
            return deepcopy(self.tag_object)
        if method == "POST" and path == "git/refs":
            assert payload == {"ref": f"refs/tags/{TAG}", "sha": TAG_OBJECT}
            self.tag_ref = {"object": {"type": "tag", "sha": TAG_OBJECT}}
            if self.move_main_after_tag:
                self.main = OTHER_COMMIT
            return deepcopy(self.tag_ref)
        if method == "GET" and path == f"git/tags/{TAG_OBJECT}":
            return deepcopy(self.tag_object)
        if method == "POST" and path == "actions/workflows/release.yml/dispatches":
            assert payload == {"ref": TAG}
            return None
        raise AssertionError(f"Unexpected API operation: {method} {path}")


def _prepare(api: FakeAPI, **overrides: str) -> None:
    arguments = {
        "tag": TAG,
        "expected_sha": COMMIT,
        "event_sha": COMMIT,
        "checkout_sha": COMMIT,
        "event_name": "workflow_dispatch",
        "ref": "refs/heads/main",
    }
    arguments.update(overrides)
    prepare_release(api, **arguments)


def _writes(api: FakeAPI) -> list[tuple[str, str, dict[str, Any] | None]]:
    return [call for call in api.calls if call[0] != "GET"]


def test_prepare_creates_annotated_tag_and_dispatches_release_on_that_tag() -> None:
    api = FakeAPI()
    _prepare(api)

    assert [call[:2] for call in _writes(api)] == [
        ("POST", "git/tags"),
        ("POST", "git/refs"),
        ("POST", "actions/workflows/release.yml/dispatches"),
    ]
    assert api.tag_object is not None
    assert api.tag_object["object"] == {"type": "commit", "sha": COMMIT}


@pytest.mark.parametrize(
    "overrides",
    [
        {"tag": "main"},
        {"tag": "v0.2.0/../../main"},
        {"expected_sha": "a" * 7},
        {"event_sha": OTHER_COMMIT},
        {"checkout_sha": OTHER_COMMIT},
        {"ref": "refs/heads/feature"},
        {"event_name": "push"},
    ],
)
def test_prepare_rejects_unsafe_context_before_contacting_github(overrides: dict[str, str]) -> None:
    api = FakeAPI()
    with pytest.raises(ReleasePreparationError):
        _prepare(api, **overrides)
    assert api.calls == []


def test_prepare_rejects_stale_main_without_creating_a_tag() -> None:
    api = FakeAPI()
    api.main = OTHER_COMMIT
    with pytest.raises(ReleasePreparationError, match="main"):
        _prepare(api)
    assert _writes(api) == []


@pytest.mark.parametrize("tag_kind", ["lightweight", "different_commit", "nested_tag"])
def test_prepare_never_replaces_an_incompatible_existing_tag(tag_kind: str) -> None:
    api = FakeAPI()
    api.tag_ref = {"object": {"type": "tag", "sha": TAG_OBJECT}}
    api.tag_object = {"tag": TAG, "object": {"type": "commit", "sha": COMMIT}}
    if tag_kind == "lightweight":
        api.tag_ref = {"object": {"type": "commit", "sha": COMMIT}}
    elif tag_kind == "different_commit":
        api.tag_object["object"]["sha"] = OTHER_COMMIT
    else:
        api.tag_object["object"]["type"] = "tag"

    with pytest.raises(ReleasePreparationError):
        _prepare(api)
    assert _writes(api) == []


def test_prepare_can_retry_dispatch_with_an_identical_annotated_tag() -> None:
    api = FakeAPI()
    api.tag_ref = {"object": {"type": "tag", "sha": TAG_OBJECT}}
    api.tag_object = {"tag": TAG, "object": {"type": "commit", "sha": COMMIT}}
    _prepare(api)
    assert _writes(api) == [("POST", "actions/workflows/release.yml/dispatches", {"ref": TAG})]


def test_prepare_does_not_dispatch_if_main_moves_after_tag_creation() -> None:
    api = FakeAPI()
    api.move_main_after_tag = True
    with pytest.raises(ReleasePreparationError, match="main"):
        _prepare(api)
    assert all("dispatches" not in path for _, path, _ in api.calls)
    assert api.tag_ref is not None


def test_prepare_rejects_a_release_that_has_already_been_created() -> None:
    api = FakeAPI()
    api.release = {"tag_name": TAG}
    with pytest.raises(ReleasePreparationError, match="already exists"):
        _prepare(api)
    assert _writes(api) == []


@pytest.mark.parametrize("preceding_releases", [0, 100])
def test_prepare_rejects_existing_draft_releases_on_every_page(preceding_releases: int) -> None:
    api = FakeAPI()
    api.draft_releases = [
        {"tag_name": f"v0.0.{index}", "draft": True} for index in range(preceding_releases)
    ] + [{"tag_name": TAG, "draft": True}]
    with pytest.raises(ReleasePreparationError, match="already exists"):
        _prepare(api)
    assert _writes(api) == []
