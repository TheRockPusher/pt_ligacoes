"""Authorize only version-only Release Please PRs, without checking out PR code."""

import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Mapping
from typing import Any

_VERSION_FILES = ("pyproject.toml", "uv.lock", ".release-please-manifest.json")
_RELEASE_FILES = frozenset((*_VERSION_FILES, "CHANGELOG.md"))
_RELEASE_BRANCH = "release-please--branches--main--components--pt-ligacoes"
_SHA = re.compile(r"[0-9a-fA-F]{40}")
_SEMVER = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
_REPOSITORY = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_][A-Za-z0-9_.-]*")
_APP_SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


class ReleaseRejected(ValueError):
    """The PR cannot safely be authorized for automatic merging."""


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise ReleaseRejected(reason)


def _object(value: object, reason: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ReleaseRejected(reason)
    return value


def _matches(pattern: re.Pattern[str], value: object) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _validate_identity(repository: str, app_slug: str) -> None:
    _require(_matches(_REPOSITORY, repository), "Invalid repository identifier")
    _require(_matches(_APP_SLUG, app_slug), "Invalid release App slug")


def _validate_metadata(pr: object, repository: str, app_slug: str) -> tuple[str, str]:
    _validate_identity(repository, app_slug)
    data = _object(pr, "Invalid pull request metadata")
    user = _object(data.get("user"), "Missing PR author")
    _require(
        user.get("login") == f"{app_slug}[bot]" and user.get("type") == "Bot",
        "Unexpected PR author",
    )
    _require(
        data.get("state") == "open"
        and data.get("draft") is False
        and data.get("merged") is False
        and data.get("merged_at") is None,
        "PR must be open, unmerged and ready for review",
    )
    labels = data.get("labels")
    _require(
        isinstance(labels, list)
        and all(isinstance(label, dict) and isinstance(label.get("name"), str) for label in labels)
        and any(label["name"] == "autorelease: pending" for label in labels),
        "Missing or invalid pending release label",
    )
    _require(
        type(data.get("changed_files")) is int and data["changed_files"] == 4,
        "Release must change exactly four files",
    )
    shas: list[str] = []
    for side, branch in (("base", "main"), ("head", _RELEASE_BRANCH)):
        commit = _object(data.get(side), "Missing PR commit metadata")
        repo = _object(commit.get("repo"), "Missing PR repository metadata")
        _require(
            repo.get("full_name") == repository and repo.get("fork") is False,
            "Fork or unexpected repository",
        )
        _require(commit.get("ref") == branch, "Unexpected release branch")
        sha = commit.get("sha")
        if not isinstance(sha, str) or _SHA.fullmatch(sha) is None:
            raise ReleaseRejected("Invalid commit SHA")
        shas.append(sha)
    return shas[0], shas[1]


def _validate_files(files: object) -> None:
    _require(isinstance(files, list) and len(files) == 4, "Incomplete or unexpected changed files")
    if not isinstance(files, list):
        raise ReleaseRejected("Invalid changed files response")
    paths: set[str] = set()
    for entry in files:
        item = _object(entry, "Invalid changed file metadata")
        path = item.get("filename")
        if not isinstance(path, str) or path not in _RELEASE_FILES:
            raise ReleaseRejected("Unexpected changed file")
        _require(path not in paths, "Duplicate changed file")
        paths.add(path)
        permitted = ("added", "modified") if path == "CHANGELOG.md" else ("modified",)
        _require(
            item.get("status") in permitted and "previous_filename" not in item,
            "Release files cannot be renamed or deleted",
        )
    _require(paths == _RELEASE_FILES, "Missing release files")


def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        _require(key not in result, "Duplicate JSON object key")
        result[key] = value
    return result


def _invalid_constant(_value: str) -> Any:
    raise ReleaseRejected("Invalid JSON constant")


def _parse_json(text: str) -> Any:
    return json.loads(text, object_pairs_hook=_json_object, parse_constant=_invalid_constant)


def _parse_contents(contents: object) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    if not isinstance(contents, Mapping):
        raise ReleaseRejected("Invalid version file contents")
    for path in _VERSION_FILES:
        _require(isinstance(contents.get(path), str), "Missing version file contents")
    project = tomllib.loads(contents["pyproject.toml"])
    lock = tomllib.loads(contents["uv.lock"])
    manifest = _object(_parse_json(contents[".release-please-manifest.json"]), "Invalid manifest")
    return project, lock, manifest


def _version(value: object) -> tuple[int, int, int]:
    _require(_matches(_SEMVER, value), "Versions must be canonical stable SemVer")
    if not isinstance(value, str):
        raise ReleaseRejected("Invalid version")
    major, minor, patch = value.split(".")
    return int(major), int(minor), int(patch)


def _extract_version(
    project: dict[str, Any], lock: dict[str, Any], manifest: dict[str, Any]
) -> tuple[int, int, int]:
    package = _object(project.get("project"), "Missing project metadata")
    name = package.get("name")
    _require(isinstance(name, str) and bool(name), "Invalid project package name")
    packages = lock.get("package")
    _require(isinstance(packages, list), "Missing lock packages")
    if not isinstance(packages, list):
        raise ReleaseRejected("Invalid lock packages")
    roots = []
    for entry in packages:
        item = _object(entry, "Invalid lock package")
        source = _object(item.get("source"), "Missing lock package source")
        if source.get("virtual") == ".":
            roots.append(item)
    _require(len(roots) == 1 and roots[0].get("name") == name, "Invalid virtual-root lock package")
    root = roots[0]
    version = package.pop("version", None)
    manifest_version = manifest.pop(".", None)
    lock_version = root.pop("version", None)
    parsed_version = _version(version)
    _require(
        _version(manifest_version) == parsed_version and _version(lock_version) == parsed_version,
        "Release versions disagree",
    )
    return parsed_version


def _same_data(left: Any, right: Any) -> bool:
    # Python normally considers True == 1 and 1 == 1.0; these are data changes here.
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(
            _same_data(value, right[key]) for key, value in left.items()
        )
    if isinstance(left, (list, tuple)):
        return len(left) == len(right) and all(
            _same_data(a, b) for a, b in zip(left, right, strict=True)
        )
    return left == right


def validate_release(
    pr: object,
    files: object,
    base_contents: Mapping[str, str],
    head_contents: Mapping[str, str],
    *,
    repository: str,
    app_slug: str,
) -> str:
    """Return the approved head SHA, or reject; never mutate caller-owned data."""
    try:
        _, head_sha = _validate_metadata(pr, repository, app_slug)
        _validate_files(files)
        base = _parse_contents(base_contents)
        head = _parse_contents(head_contents)
        base_version = _extract_version(*base)
        head_version = _extract_version(*head)
        _require(head_version > base_version, "Release version must strictly increase")
        _require(_same_data(base, head), "Release changes data beyond the three versions")
        return head_sha
    except ReleaseRejected:
        raise
    except (ValueError, TypeError, KeyError, RecursionError, OverflowError) as exc:
        raise ReleaseRejected("Malformed release metadata or version files") from exc


def _api(gh: str, endpoint: str) -> Any:
    try:
        result = subprocess.run(  # noqa: S603 -- trusted gh binary, no shell or PR command input.
            [gh, "api", "--hostname", "github.com", "--method", "GET", endpoint],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        _require(result.returncode == 0, "GitHub API request failed")
        return _parse_json(result.stdout)
    except ReleaseRejected:
        raise
    except (OSError, subprocess.SubprocessError, ValueError, RecursionError) as exc:
        raise ReleaseRejected("GitHub API request or response failed") from exc


def _fetch_contents(gh: str, repository: str, sha: str) -> dict[str, str]:
    contents = {}
    for path in _VERSION_FILES:
        response = _object(
            _api(gh, f"repos/{repository}/contents/{path}?ref={sha}"),
            "Invalid GitHub file response",
        )
        _require(
            response.get("type") == "file"
            and response.get("path") == path
            and response.get("encoding") == "base64"
            and isinstance(response.get("content"), str),
            "Invalid GitHub version file response",
        )
        try:
            encoded = response["content"].replace("\n", "").replace("\r", "")
            contents[path] = base64.b64decode(encoded, validate=True).decode("utf-8")
        except ValueError as exc:
            raise ReleaseRejected("Invalid encoded version file") from exc
    return contents


def _pull_request(gh: str, endpoint: str, number: int) -> dict[str, Any]:
    pr = _object(_api(gh, endpoint), "Invalid pull request response")
    _require(type(pr.get("number")) is int and pr["number"] == number, "Unexpected PR number")
    return pr


def main() -> int:
    try:
        repository = os.environ.get("GITHUB_REPOSITORY", "")
        app_slug = os.environ.get("RELEASE_APP_SLUG", "")
        number = os.environ.get("RELEASE_PR_NUMBER", "")
        output_path = os.environ.get("GITHUB_OUTPUT", "")
        _validate_identity(repository, app_slug)
        _require(_matches(re.compile(r"[1-9][0-9]{0,9}"), number), "Invalid release PR number")
        _require(bool(os.environ.get("GH_TOKEN")), "Missing GitHub authentication")
        _require(bool(output_path), "Missing GitHub output file")
        gh = shutil.which("gh")
        if gh is None:
            raise ReleaseRejected("GitHub CLI is not installed")
        endpoint = f"repos/{repository}/pulls/{number}"
        pr = _pull_request(gh, endpoint, int(number))
        base_sha, head_sha = _validate_metadata(pr, repository, app_slug)
        comparison = _object(
            _api(gh, f"repos/{repository}/compare/{base_sha}...{head_sha}?page=1&per_page=1"),
            "Invalid immutable commit comparison",
        )
        # The first comparison page includes all files (up to 300), independently
        # of commit pagination. Requiring exactly four rejects truncation too.
        files = comparison.get("files")
        _validate_files(files)
        base_contents = _fetch_contents(gh, repository, base_sha)
        head_contents = _fetch_contents(gh, repository, head_sha)
        approved_sha = validate_release(
            pr,
            files,
            base_contents,
            head_contents,
            repository=repository,
            app_slug=app_slug,
        )
        current = _pull_request(gh, endpoint, int(number))
        _require(
            _validate_metadata(current, repository, app_slug) == (base_sha, head_sha),
            "PR head or base changed during validation",
        )
        with open(output_path, "a", encoding="utf-8") as output:
            output.write(f"head_sha={approved_sha}\n")
        print("Release PR authorized for protected auto-merge.")
        return 0
    except ReleaseRejected as exc:
        # These reasons are fixed messages, never API payloads or credential values.
        print(f"Release PR rejected: {exc}.", file=sys.stderr)
        return 1
    except OSError:
        print("Release PR rejected: GitHub output could not be written.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
