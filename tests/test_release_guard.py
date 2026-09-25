import json
from copy import deepcopy

import pytest
from scripts.release_guard import ReleaseRejected, validate_release

REPOSITORY = "TheRockPusher/pt_ligacoes"
APP_SLUG = "pt-ligacoes-release"
HEAD_SHA = "a" * 40
ROOT_PACKAGE = """[[package]]
name = "pt-ligacoes"
version = "{version}"
source = {{ virtual = "." }}
dependencies = [{{ name = "example" }}]
"""


def release_contents(version):
    return {
        "pyproject.toml": f'''[project]
name = "pt-ligacoes"
version = "{version}"
requires-python = ">=3.12"
dependencies = ["example>=1.0"]

[tool.ruff]
line-length = 100
''',
        "uv.lock": (
            'version = 1\nrevision = 3\nrequires-python = ">=3.12"\n\n'
            + ROOT_PACKAGE.format(version=version)
            + """
[[package]]
name = "example"
version = "1.0.0"
source = { registry = "https://pypi.org/simple" }
sdist = { url = "https://files.example/example.tar.gz", hash = "sha256:abcd", size = 100 }
"""
        ),
        ".release-please-manifest.json": json.dumps({".": version, "other": "1.0.0"}),
    }


@pytest.fixture
def release():
    return {
        "pr": {
            "user": {"login": f"{APP_SLUG}[bot]", "type": "Bot"},
            "state": "open",
            "draft": False,
            "merged": False,
            "base": {
                "ref": "main",
                "sha": "b" * 40,
                "repo": {"full_name": REPOSITORY, "fork": False},
            },
            "head": {
                "ref": "release-please--branches--main--components--pt-ligacoes",
                "sha": HEAD_SHA,
                "repo": {"full_name": REPOSITORY, "fork": False},
            },
            # release-please v17.6.0 manifest.ts DEFAULT_LABELS, including the space.
            "labels": [{"name": "autorelease: pending"}],
            "changed_files": 4,
        },
        "files": [
            {"filename": path, "status": "modified"}
            for path in (
                "CHANGELOG.md",
                "pyproject.toml",
                "uv.lock",
                ".release-please-manifest.json",
            )
        ],
        "base_contents": release_contents("0.1.1"),
        "head_contents": release_contents("0.2.0"),
        "repository": REPOSITORY,
        "app_slug": APP_SLUG,
    }


def replace_nested(mapping, path, value):
    for key in path[:-1]:
        mapping = mapping[key]
    mapping[path[-1]] = value


@pytest.mark.parametrize("changelog_status", ["added", "modified"])
def test_valid_release_returns_exact_head_without_mutating_inputs(release, changelog_status):
    release["files"][0]["status"] = changelog_status
    before = deepcopy(release)

    assert validate_release(**release) == HEAD_SHA
    assert validate_release(**release) == HEAD_SHA
    assert release == before


def test_version_order_is_numeric_and_content_comparison_is_semantic(release):
    release["base_contents"] = release_contents("0.9.0")
    release["head_contents"] = release_contents("0.10.0")
    release["head_contents"]["pyproject.toml"] += "\n# Generated release metadata\n"
    release["head_contents"]["uv.lock"] += "\n# Unchanged dependency graph\n"
    release["head_contents"][".release-please-manifest.json"] = (
        '{\n  "other": "1.0.0",\n  ".": "0.10.0"\n}\n'
    )

    assert validate_release(**release) == HEAD_SHA


def test_root_package_identity_comes_from_project_name(release):
    for side in ("base_contents", "head_contents"):
        for path in ("pyproject.toml", "uv.lock"):
            release[side][path] = release[side][path].replace("pt-ligacoes", "fixture-project")

    assert validate_release(**release) == HEAD_SHA


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("user", "login"), "release-please[bot]"),
        (("user", "type"), "User"),
        (("head", "repo", "full_name"), "attacker/pt_ligacoes"),
        (("base", "repo", "full_name"), "attacker/pt_ligacoes"),
        (("head", "repo", "fork"), True),
        (("base", "repo", "fork"), True),
        (("head", "ref"), "feature/release"),
        (("base", "ref"), "develop"),
        (("state",), "closed"),
        (("draft",), True),
        (("merged",), True),
        (("merged_at",), "2026-09-25T00:00:00Z"),
        (("labels",), [{"name": "autorelease: tagged"}]),
        (("head", "sha"), "a" * 39),
        (("base", "sha"), "z" * 40),
    ],
)
def test_rejects_untrusted_pull_request_metadata(release, path, value):
    replace_nested(release["pr"], path, value)
    before = deepcopy(release)

    with pytest.raises(ReleaseRejected):
        validate_release(**release)
    assert release == before


@pytest.mark.parametrize(
    "change",
    ["unexpected", "deleted", "renamed", "omitted", "duplicate", "truncated", "added-project"],
)
def test_requires_complete_exact_release_file_set(release, change):
    if change == "unexpected":
        release["files"][0]["filename"] = ".github/workflows/ci.yml"
    elif change == "deleted":
        release["files"][0]["status"] = "removed"
    elif change == "renamed":
        release["files"][0].update(status="renamed", previous_filename="README.md")
    elif change == "omitted":
        release["files"].pop()
    elif change == "duplicate":
        release["files"][-1] = deepcopy(release["files"][0])
    elif change == "truncated":
        release["pr"]["changed_files"] = 5
    else:
        release["files"][1]["status"] = "added"

    with pytest.raises(ReleaseRejected):
        validate_release(**release)


@pytest.mark.parametrize("side", ["base_contents", "head_contents"])
@pytest.mark.parametrize("path", ["pyproject.toml", "uv.lock", ".release-please-manifest.json"])
def test_rejects_inconsistent_release_versions(release, side, path):
    release[side][path] = release_contents("0.1.5")[path]

    with pytest.raises(ReleaseRejected):
        validate_release(**release)


@pytest.mark.parametrize(
    "version", ["0.1.0", "0.1.1", "0.02.0", "v0.2.0", "0.2.0-rc.1", "0.2.0+build"]
)
def test_requires_strictly_increasing_canonical_stable_version(release, version):
    release["head_contents"] = release_contents(version)

    with pytest.raises(ReleaseRejected):
        validate_release(**release)


@pytest.mark.parametrize(
    ("path", "old", "new"),
    [
        ("pyproject.toml", "example>=1.0", "example>=2.0"),
        ("pyproject.toml", "line-length = 100", "line-length = 120"),
        ("uv.lock", "sha256:abcd", "sha256:deadbeef"),
        ("uv.lock", 'version = "1.0.0"', 'version = "2.0.0"'),
        ("uv.lock", "https://pypi.org/simple", "https://attacker.example/simple"),
        ("uv.lock", "version = 1\n", "version = true\n"),
        (".release-please-manifest.json", '"other": "1.0.0"', '"other": "2.0.0"'),
    ],
)
def test_rejects_non_release_content_changes(release, path, old, new):
    release["head_contents"][path] = release["head_contents"][path].replace(old, new)

    with pytest.raises(ReleaseRejected):
        validate_release(**release)


@pytest.mark.parametrize("side", ["base_contents", "head_contents"])
@pytest.mark.parametrize("root_change", ["absent", "duplicate", "wrong-name", "non-virtual"])
def test_requires_unique_matching_virtual_root_package(release, side, root_change):
    version = "0.1.1" if side == "base_contents" else "0.2.0"
    root = ROOT_PACKAGE.format(version=version)
    lock = release[side]["uv.lock"]
    if root_change == "absent":
        lock = lock.replace(root, "")
    elif root_change == "duplicate":
        lock += "\n" + root
    elif root_change == "wrong-name":
        lock = lock.replace('name = "pt-ligacoes"', 'name = "unrelated"')
    else:
        lock = lock.replace('virtual = "."', 'editable = "."')
    release[side]["uv.lock"] = lock

    with pytest.raises(ReleaseRejected):
        validate_release(**release)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("pr",), None),
        (("pr", "user"), None),
        (("pr", "head", "repo"), None),
        (("pr", "draft"), "false"),
        (("pr", "labels"), ["autorelease: pending"]),
        (("pr", "changed_files"), "4"),
        (("files",), None),
        (("files",), [{"filename": "CHANGELOG.md"}, None, {}, {}]),
        (("head_contents", "pyproject.toml"), "[project"),
        (("base_contents", "uv.lock"), "[[package]"),
        (("head_contents", ".release-please-manifest.json"), "[]"),
        (("base_contents", ".release-please-manifest.json"), "{broken"),
        (("head_contents", "pyproject.toml"), None),
    ],
)
def test_malformed_inputs_fail_closed(release, path, value):
    replace_nested(release, path, value)

    with pytest.raises(ReleaseRejected):
        validate_release(**release)


@pytest.mark.parametrize(
    "path",
    [
        ("pr", "merged"),
        ("pr", "user", "login"),
        ("pr", "head", "sha"),
        ("head_contents", "uv.lock"),
    ],
)
def test_missing_required_data_fails_closed(release, path):
    mapping = release
    for key in path[:-1]:
        mapping = mapping[key]
    del mapping[path[-1]]

    with pytest.raises(ReleaseRejected):
        validate_release(**release)
