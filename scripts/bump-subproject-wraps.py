#!/usr/bin/env python
# /// script
# dependencies = ["requests"]
# ///
"""
Update Meson wrap file `revision` fields to point to latest release.
"""
from __future__ import annotations
import argparse
import configparser
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import urllib.parse
from pathlib import Path
from dataclasses import dataclass, asdict

import requests


log = logging.getLogger(__name__)


SEMVER_RE = re.compile(
    r"""
    ^v?
    (?P<major>0|[1-9]\d*)\.
    (?P<minor>0|[1-9]\d*)\.
    (?P<patch>0|[1-9]\d*)
    $""",
    re.VERBOSE,
)

# Wrap fields that embed the upstream version and must be rewritten together.
VERSIONED_KEYS = ("directory", "source_url", "source_filename", "source_fallback_url")

VERSION_RE = re.compile(r"\d+(?:\.\d+)+")

ROOT = Path(__file__).resolve().parents[1]
WRAP_DIR = ROOT / "subprojects"
SESSION = requests.Session()
GH_TOKEN = os.getenv("GH_TOKEN", "")
if GH_TOKEN:
    SESSION.headers["Authorization"] = f"Bearer {GH_TOKEN}"
SESSION.headers["Accept"] = "application/vnd.github+json"


def gh_sha_for_tag(owner: str, repo: str, tag: str) -> str:
    data = SESSION.get(
        f"https://api.github.com/repos/{owner}/{repo}/git/ref/tags/{tag}", timeout=30
    ).json()

    # First level: get the object it points to
    obj_type = data["object"]["type"]
    obj_sha = data["object"]["sha"]

    if obj_type == "commit":
        # Lightweight tag
        return obj_sha
    elif obj_type == "tag":
        # Annotated tag: need to dereference
        tag_obj_url = data["object"]["url"]
        tag_data = requests.get(tag_obj_url).json()
        return tag_data["object"]["sha"]
    else:
        raise Exception(f"Unknown object type: {obj_type}")


def gh_latest_release(
    owner: str, repo: str, pattern: re.Pattern
) -> None | tuple[str, str]:
    """
    Return (tag_name, commit_sha) for the most recent matching release.
    """
    releases = SESSION.get(
        f"https://api.github.com/repos/{owner}/{repo}/releases", timeout=30
    ).json()
    viable = [
        t
        for t in releases
        if pattern.match(t["tag_name"])
        and not t.get("draft")
        and not t.get("prerelease")
    ]

    if not viable:
        return None

    tag_name = viable[0]["tag_name"]
    sha = gh_sha_for_tag(owner, repo, tag_name)

    return tag_name, sha


def gh_latest_tag(owner: str, repo: str, pattern: re.Pattern) -> tuple[str, str]:
    """
    Return (tag_name, commit_sha) for the most recent matching tag.
    """
    tags = SESSION.get(
        f"https://api.github.com/repos/{owner}/{repo}/tags", timeout=30
    ).json()
    viable = [t for t in tags if pattern.match(t["name"])]

    if not viable:
        return None

    return viable[0]["name"], viable[0]["commit"]["sha"]


def sha256_of_url(url: str) -> str:
    """
    Stream `url` and return its SHA-256 digest.
    """
    digest = hashlib.sha256()
    # Deliberately not SESSION: that carries a GitHub API token and Accept
    # header which have no business on a release asset download.
    with requests.get(url, timeout=120, stream=True) as resp:
        resp.raise_for_status()
        for chunk in resp.iter_content(chunk_size=1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def write_wrap(cp: configparser.ConfigParser, path: Path) -> None:
    with open(path, "w", encoding="utf-8") as file:
        cp.write(file)

        # XXX: ConfigParser writes two extra newlines. Trim the last one.
        file.seek(file.tell() - 1, 0)
        file.truncate()


def is_wrapdb_wrap(w: configparser.SectionProxy) -> bool:
    """
    True if the wrap is served by WrapDB, as opposed to a hand-written wrap
    pointing straight at an upstream release tarball.
    """
    return bool(w.get("wrapdb_version")) or "wrapdb.mesonbuild.com" in w.get(
        "patch_url", ""
    )


@dataclass
class UpdatedWrap:
    path: str
    owner: str
    repo: str
    old_rev: str
    new_rev: str
    new_tag: str


def update_wrapdb_wrap(path: Path) -> None | UpdatedWrap:
    """
    Update a wrapdb wrap file using `meson wrap update`.
    """
    wrap_name = path.stem

    # Read current version
    cp_before = configparser.ConfigParser(interpolation=None)
    cp_before.read(path, encoding="utf-8")

    if "wrap-file" not in cp_before:
        return None

    # Extract version info before update
    w_before = cp_before["wrap-file"]
    source_url_before = w_before.get("source_url", "")
    source_hash_before = w_before.get("source_hash", "")
    patch_hash_before = w_before.get("patch_hash", "")
    wrapdb_version_before = w_before.get("wrapdb_version", "")

    if not wrapdb_version_before:
        # `meson wrap update` needs this field to know what it is upgrading
        # from. Without it, it prints "Could not determine current version",
        # leaves the wrap alone, and still exits 0 -- so the wrap would be
        # skipped forever without a word in the log.
        log.warning(
            "%s: no wrapdb_version field, `meson wrap update` cannot update it",
            path.name,
        )
        return None

    old_version = wrapdb_version_before

    # Call meson wrap update
    try:
        result = subprocess.run(
            ["meson", "wrap", "update", wrap_name],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

        if result.returncode != 0:
            log.info("meson wrap update failed for %s: %s", wrap_name, result.stderr)
            return None

    except FileNotFoundError:
        log.error("meson command not found. Cannot update wrapdb wraps.")
        return None
    except Exception as e:
        log.exception(e)
        return None

    # Read updated version
    cp_after = configparser.ConfigParser(interpolation=None)
    cp_after.read(path, encoding="utf-8")

    if "wrap-file" not in cp_after:
        return None

    w_after = cp_after["wrap-file"]
    source_url_after = w_after.get("source_url", "")
    source_hash_after = w_after.get("source_hash", "")
    patch_hash_after = w_after.get("patch_hash", "")
    wrapdb_version_after = w_after.get("wrapdb_version", "")

    # Check if anything changed (compare multiple fields)
    if (
        source_url_before == source_url_after
        and source_hash_before == source_hash_after
        and patch_hash_before == patch_hash_after
        and wrapdb_version_before == wrapdb_version_after
    ):
        log.info("%s already up to date", path.name)
        return None

    # Try to extract new version from wrapdb_version or filename
    new_version = wrapdb_version_after
    if not new_version and source_url_after:
        filename = urllib.parse.urlparse(source_url_after).path.split("/")[-1]
        version_match = re.search(r"[-_]v?(\d+(?:\.\d+)*(?:[.-]\w+)?)", filename)
        if version_match:
            new_version = version_match.group(1)

    # Try to extract GitHub info if the source is on GitHub
    owner, repo = "wrapdb", wrap_name
    old_rev = old_version or "old"
    new_rev = new_version or "new"
    new_tag = new_version or "latest"

    # Check if source_url points to GitHub
    gh_match = re.match(
        r".*github\.com[:/](?P<owner>[^/]+)/(?P<repo>[^/]+)/", source_url_after
    )
    if gh_match:
        owner = gh_match.group("owner")
        repo = gh_match.group("repo")

    log.info(
        "%s updated from %s to %s", path.name, old_version or "?", new_version or "?"
    )

    return UpdatedWrap(str(path), owner, repo, old_rev, new_rev, new_tag)


def update_github_release_wrap(path: Path) -> None | UpdatedWrap:
    """
    Update a hand-written [wrap-file] wrap that points directly at a GitHub
    release tarball, by rewriting the version everywhere it appears and
    re-hashing the new tarball.
    """
    cp = configparser.ConfigParser(interpolation=None)
    cp.read(path, encoding="utf-8")
    w = cp["wrap-file"]

    source_url = w.get("source_url", "")
    m = re.match(r"https?://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/", source_url)
    if not m:
        log.warning("%s: source_url is not a GitHub URL, cannot update", path.name)
        return None
    owner, repo = m.group("owner"), m.group("repo")

    version_match = VERSION_RE.search(w.get("directory", "")) or VERSION_RE.search(
        w.get("source_filename", "")
    )
    if not version_match:
        log.warning("%s: could not determine current version", path.name)
        return None
    old_version = version_match.group(0)

    pattern = cp.get("update", "tag_regex", fallback=None)
    pattern = re.compile(pattern) if pattern else SEMVER_RE

    latest = gh_latest_release(owner, repo, pattern)
    if latest is None:
        log.info("Couldn't find latest release for %s/%s", owner, repo)
        log.info("Searching for tags directly...")
        latest = gh_latest_tag(owner, repo, pattern)
    if latest is None:
        log.warning(
            "%s: no release or tag of %s/%s matches %s",
            path.name,
            owner,
            repo,
            pattern.pattern,
        )
        return None
    new_tag = latest[0]

    version_match = VERSION_RE.search(new_tag)
    if not version_match:
        log.warning("%s: no version in tag %s", path.name, new_tag)
        return None
    new_version = version_match.group(0)

    if new_version == old_version:
        log.info("%s already at %s", path.name, old_version)
        return None

    updated = {
        key: w[key].replace(old_version, new_version)
        for key in VERSIONED_KEYS
        if key in w
    }

    try:
        source_hash = sha256_of_url(updated["source_url"])
    except Exception as e:
        log.error("%s: could not fetch %s: %s", path.name, updated["source_url"], e)
        return None

    for key, value in updated.items():
        w[key] = value
    w["source_hash"] = source_hash

    write_wrap(cp, path)

    log.info("%s updated from %s to %s", path.name, old_version, new_version)

    # Tags, not versions, so the PR body's compare link resolves.
    old_tag = new_tag.replace(new_version, old_version)

    return UpdatedWrap(str(path), owner, repo, old_tag, new_tag, new_version)


def update_wrap(path: Path) -> None | UpdatedWrap:
    """
    Return (tag_name, commit_sha) if updated, otherwise None.
    """
    cp = configparser.ConfigParser(interpolation=None)
    cp.read(path, encoding="utf-8")

    if "wrap-file" in cp:
        if is_wrapdb_wrap(cp["wrap-file"]):
            # Handle wrapdb wraps using meson wrap update
            return update_wrapdb_wrap(path)
        # Hand-written wrap pointing straight at an upstream release tarball
        return update_github_release_wrap(path)

    if "wrap-git" not in cp:
        return None

    w = cp["wrap-git"]
    url = w.get("url", "")
    rev = w.get("revision", "").strip()
    m = re.match(r".*github\.com[:/](?P<owner>[^/]+)/(?P<repo>[^/.]+)(?:\.git)?", url)
    if not (m and rev):
        return None

    owner, repo = m.group("owner"), m.group("repo")
    try:
        pattern = cp.get("update", "tag_regex", fallback=None)
        pattern = re.compile(pattern) if pattern else SEMVER_RE

        latest = gh_latest_release(owner, repo, pattern)
        if latest is None:
            log.info("Couldn't find latest release for %s/%s", owner, repo)
            log.info("Searching for tags directly...")
            latest = gh_latest_tag(owner, repo, pattern)
            if latest is None:
                log.info("Couldn't find latest tag for %s/%s", owner, repo)
                return None
        tag, sha = latest
    except Exception as e:
        log.exception(e)
        return None

    if sha.startswith(rev):
        log.info("%s already at %s (%s)", path.name, tag, sha)
        return None

    log.info("%s updated to %s (%s)", path.name, tag, sha)

    w["revision"] = sha
    write_wrap(cp, path)

    return UpdatedWrap(str(path), owner, repo, rev, sha, tag)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--manifest",
        "-m",
        action="store_true",
        default=False,
        help="Print JSON-formatted updated manifest",
    )
    ap.add_argument(
        "wraps", nargs="*", help="Which wraps to update, or all if unspecified"
    )
    args = ap.parse_args()

    wraps = args.wraps
    if wraps:
        wraps = [Path(p) for p in wraps]
    else:
        wraps = WRAP_DIR.glob("*.wrap")

    logging.basicConfig(level=logging.INFO)

    updated = []
    for wrap in wraps:
        info = update_wrap(wrap)
        if info:
            updated.append(asdict(info))

    if args.manifest:
        json.dump(updated, sys.stdout, indent=2)


if __name__ == "__main__":
    main()
