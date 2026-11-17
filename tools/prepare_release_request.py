"""Dispatch the existing guarded Release workflow after a reviewed main push."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

from tools.prepare_release import (
    GitHubAPI,
    ReleasePreparationError,
    _require_main,
    _require_no_release,
    _require_tag,
)

ROOT = Path(__file__).resolve().parents[1]
REPO = "sebia1993/aruba-2930f-config-backup"
REQUEST_DIRECTORY = ".github/release-requests/"
BOOTSTRAP_ADDITIONS = frozenset(
    {
        ".github/workflows/chat-release-request.yml",
        "tools/prepare_release_request.py",
        "tests/test_prepare_release_request.py",
    }
)
SHA = re.compile(r"[0-9a-f]{40}")
TAG = re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+")


def _git(*arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments],
        cwd=ROOT,
        capture_output=True,
        check=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()


def validate_release_request(
    *,
    request: object,
    changes: dict[str, str],
    parent_sha: str,
    event_sha: str,
    checkout_sha: str,
    event_name: str,
    ref: str,
    repository: str,
    bootstrap: bool,
) -> str:
    """Verify one intentionally approved, code-free release-request commit."""
    if repository != REPO or event_name != "push" or ref != "refs/heads/main":
        raise ReleasePreparationError("Only a reviewed push to repository main is permitted")
    if any(SHA.fullmatch(value) is None for value in (parent_sha, event_sha, checkout_sha)):
        raise ReleasePreparationError("Malformed source or event SHA")
    if event_sha != checkout_sha or parent_sha == checkout_sha:
        raise ReleasePreparationError("Event and checkout main commits do not match")
    if not isinstance(request, dict) or set(request) != {
        "schema", "tag", "expected_source_main_sha"
    }:
        raise ReleasePreparationError("Invalid release request document")
    if type(request["schema"]) is not int or request["schema"] != 1:
        raise ReleasePreparationError("Unsupported release request schema")
    tag = request["tag"]
    if not isinstance(tag, str) or TAG.fullmatch(tag) is None:
        raise ReleasePreparationError("Invalid release tag")
    if request["expected_source_main_sha"] != parent_sha:
        raise ReleasePreparationError("Reviewed source main changed before merge")
    request_path = REQUEST_DIRECTORY + tag + ".json"
    files = {request_path} | (set(BOOTSTRAP_ADDITIONS) if bootstrap else set())
    if changes != {path: "A" for path in files}:
        raise ReleasePreparationError("Release request commit includes unexpected changes")
    return tag


def main() -> None:
    head = _git("rev-parse", "--verify", "HEAD^{commit}").lower()
    parent = _git("rev-parse", "--verify", "HEAD^1").lower()
    changes: dict[str, str] = {}
    diff = _git("diff", "--name-status", "--no-renames", parent, head)
    for line in diff.splitlines():
        status, sep, path = line.partition("\t")
        if not sep or path in changes:
            raise ReleasePreparationError("Ambiguous release-request file changes")
        changes[path] = status
    request_files = [
        path
        for path in changes
        if path.startswith(REQUEST_DIRECTORY) and path.endswith(".json")
    ]
    if len(request_files) != 1:
        raise ReleasePreparationError("Expected exactly one release request file")
    installed = subprocess.run(
        ["git", "cat-file", "-e", f"{parent}:.github/workflows/chat-release-request.yml"],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    ).returncode == 0
    request = json.loads((ROOT / request_files[0]).read_text(encoding="utf-8"))
    tag = validate_release_request(
        request=request,
        changes=changes,
        parent_sha=parent,
        event_sha=os.environ.get("GITHUB_SHA", "").lower(),
        checkout_sha=head,
        event_name=os.environ.get("GITHUB_EVENT_NAME", ""),
        ref=os.environ.get("GITHUB_REF", ""),
        repository=os.environ.get("GITHUB_REPOSITORY", ""),
        bootstrap=not installed,
    )
    subprocess.run(
        [sys.executable, str(ROOT / "tools/check_version.py"), "--tag", tag],
        cwd=ROOT,
        check=True,
    )
    api = GitHubAPI(REPO, os.environ.get("GH_TOKEN", ""))
    _require_main(api, head)
    report = api.request("GET", f"commits/{parent}/check-runs?per_page=100")
    runs = report.get("check_runs") if isinstance(report, dict) else None
    if not isinstance(runs, list) or not any(
        isinstance(run, dict)
        and run.get("name") == "Windows x64 validation and package"
        and run.get("status") == "completed"
        and run.get("conclusion") == "success"
        and isinstance(run.get("app"), dict)
        and run["app"].get("slug") == "github-actions"
        for run in runs
    ):
        raise ReleasePreparationError("Source main has no successful Windows packaging check")
    _require_no_release(api, tag)
    if api.request("GET", f"git/ref/tags/{tag}", allow_missing=True) is None:
        annotation = api.request(
            "POST",
            "git/tags",
            {"tag": tag, "message": f"Release {tag}", "object": head, "type": "commit"},
        )
        sha = annotation.get("sha") if isinstance(annotation, dict) else None
        if not isinstance(sha, str) or SHA.fullmatch(sha) is None:
            raise ReleasePreparationError("GitHub did not return an annotated tag SHA")
        api.request("POST", "git/refs", {"ref": f"refs/tags/{tag}", "sha": sha})
    _require_tag(api, tag, head)
    _require_main(api, head)
    api.request("POST", "actions/workflows/release.yml/dispatches", {"ref": tag})
    print(f"Validated {tag}; dispatched the existing verified Release workflow")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        print(f"chat-release-request: {exc}", file=sys.stderr)
        sys.exit(1)
