"""Create an annotated tag on reviewed main, then dispatch the existing release gates."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Protocol

ROOT = Path(__file__).resolve().parents[1]
SHA_RE = re.compile(r"[0-9a-fA-F]{40}")


class ReleasePreparationError(ValueError):
    """The requested release cannot be prepared without changing its provenance."""


class ReleaseAPI(Protocol):
    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        allow_missing: bool = False,
    ) -> dict[str, Any] | list[Any] | None: ...


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        # A repository rename should fail rather than forward a token to another URL.
        return None


class GitHubAPI:
    def __init__(self, repository: str, token: str) -> None:
        if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) is None:
            raise ReleasePreparationError("Repository must use owner/name form")
        if not token:
            raise ReleasePreparationError("GH_TOKEN is required")
        self._base = f"https://api.github.com/repos/{repository}/"
        self._token = token
        self._opener = urllib.request.build_opener(_NoRedirects())

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        allow_missing: bool = False,
    ) -> dict[str, Any] | list[Any] | None:
        request = urllib.request.Request(
            self._base + path,
            data=None if payload is None else json.dumps(payload).encode("utf-8"),
            method=method,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "Aruba2930F-release-preparation",
            },
        )
        try:
            with self._opener.open(request, timeout=30) as response:
                body = response.read()
        except urllib.error.HTTPError as error:
            if allow_missing and method == "GET" and error.code == 404:
                return None
            # Do not include request headers, tokens, or raw remote response bodies in logs.
            raise ReleasePreparationError(
                f"GitHub {method} {path} failed with HTTP {error.code}"
            ) from None
        except urllib.error.URLError:
            raise ReleasePreparationError(f"GitHub {method} {path} could not connect") from None
        if not body:
            return None
        value = json.loads(body)
        if not isinstance(value, (dict, list)):
            raise ReleasePreparationError(f"GitHub {method} {path} returned an unexpected response")
        return value


def _object(value: object, kind: str, label: str) -> str:
    obj = value.get("object") if isinstance(value, dict) else None
    if not isinstance(obj, dict) or obj.get("type") != kind:
        raise ReleasePreparationError(f"{label} must point directly to a {kind}")
    sha = obj.get("sha")
    if not isinstance(sha, str) or SHA_RE.fullmatch(sha) is None:
        raise ReleasePreparationError(f"{label} has a malformed SHA")
    return sha.lower()


def _require_main(api: ReleaseAPI, expected_sha: str) -> None:
    if _object(api.request("GET", "git/ref/heads/main"), "commit", "main") != expected_sha:
        raise ReleasePreparationError("Current main differs from the approved commit")


def _require_tag(api: ReleaseAPI, tag: str, expected_sha: str) -> None:
    tag_sha = _object(api.request("GET", f"git/ref/tags/{tag}"), "tag", "Release tag")
    tag_object = api.request("GET", f"git/tags/{tag_sha}")
    if not isinstance(tag_object, dict) or tag_object.get("tag") != tag:
        raise ReleasePreparationError("Annotated tag name does not match the requested release")
    if _object(tag_object, "commit", "Annotated tag") != expected_sha:
        raise ReleasePreparationError("Existing annotated tag points to a different commit")


def _require_no_release(api: ReleaseAPI, tag: str) -> None:
    # The by-tag endpoint excludes drafts. The authenticated list includes them.
    page = 1
    while True:
        releases = api.request("GET", f"releases?per_page=100&page={page}")
        if not isinstance(releases, list) or any(not isinstance(item, dict) for item in releases):
            raise ReleasePreparationError("GitHub returned an unexpected release list")
        if any(item.get("tag_name") == tag for item in releases):
            raise ReleasePreparationError(
                "A published or draft release already exists for this tag"
            )
        if len(releases) < 100:
            return
        page += 1


def prepare_release(
    api: ReleaseAPI,
    *,
    tag: str,
    expected_sha: str,
    event_sha: str,
    checkout_sha: str,
    event_name: str,
    ref: str,
) -> None:
    if re.fullmatch(r"v\d+\.\d+\.\d+", tag) is None:
        raise ReleasePreparationError("Tag must use vMAJOR.MINOR.PATCH form")
    if any(SHA_RE.fullmatch(sha) is None for sha in (expected_sha, event_sha, checkout_sha)):
        raise ReleasePreparationError("Expected, event, and checkout SHA must be full commit SHAs")
    expected_sha = expected_sha.lower()
    if event_name != "workflow_dispatch" or ref != "refs/heads/main":
        raise ReleasePreparationError("Release preparation must be manually run on main")
    if {expected_sha, event_sha.lower(), checkout_sha.lower()} != {expected_sha}:
        raise ReleasePreparationError("Expected, event, and checkout commits do not match")

    _require_main(api, expected_sha)
    _require_no_release(api, tag)
    if api.request("GET", f"git/ref/tags/{tag}", allow_missing=True) is None:
        annotation = api.request(
            "POST",
            "git/tags",
            {"tag": tag, "message": f"Release {tag}", "object": expected_sha, "type": "commit"},
        )
        tag_sha = annotation.get("sha") if isinstance(annotation, dict) else None
        if not isinstance(tag_sha, str) or SHA_RE.fullmatch(tag_sha) is None:
            raise ReleasePreparationError("GitHub did not return an annotated tag object SHA")
        api.request("POST", "git/refs", {"ref": f"refs/tags/{tag}", "sha": tag_sha})

    # Never replace/delete a tag. An identical annotated tag permits a failed dispatch to retry.
    _require_tag(api, tag, expected_sha)
    _require_main(api, expected_sha)
    # GITHUB_TOKEN-created tag pushes do not trigger another workflow, so dispatch explicitly.
    api.request("POST", "actions/workflows/release.yml/dispatches", {"ref": tag})


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", required=True)
    parser.add_argument("--expected-main-sha", required=True)
    args = parser.parse_args()
    subprocess.run(
        [sys.executable, str(ROOT / "tools" / "check_version.py"), "--tag", args.tag],
        cwd=ROOT,
        check=True,
    )
    checkout_sha = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD^{commit}"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()
    prepare_release(
        GitHubAPI(os.environ["GITHUB_REPOSITORY"], os.environ.get("GH_TOKEN", "")),
        tag=args.tag,
        expected_sha=args.expected_main_sha,
        event_sha=os.environ.get("GITHUB_SHA", ""),
        checkout_sha=checkout_sha,
        event_name=os.environ.get("GITHUB_EVENT_NAME", ""),
        ref=os.environ.get("GITHUB_REF", ""),
    )
    print(f"Prepared annotated tag {args.tag}; dispatched the Release workflow on that tag.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print(f"prepare-release: {error}", file=sys.stderr)
        sys.exit(1)
