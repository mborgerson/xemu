#!/usr/bin/env python
# /// script
# dependencies = ["requests"]
# ///
"""
Update Meson wrap file `revision` fields to point to latest release.
"""
from __future__ import annotations
import configparser
import os
import re
from pathlib import Path

import requests


SEMVER_RE = re.compile(
    r"""
    ^v?
    (?P<major>0|[1-9]\d*)\.
    (?P<minor>0|[1-9]\d*)\.
    (?P<patch>0|[1-9]\d*)
    $""",
    re.VERBOSE,
)

ROOT = Path(__file__).resolve().parents[1]
WRAP_DIR = ROOT / "subprojects"
SESSION = requests.Session()
GH_TOKEN = os.getenv("GH_TOKEN", "")
if GH_TOKEN:
    SESSION.headers["Authorization"] = f"Bearer {GH_TOKEN}"
SESSION.headers["Accept"] = "application/vnd.github+json"


def gh_latest_tag(owner: str, repo: str, pattern: re.Pattern) -> tuple[str, str]:
    """
    Return (tag_name, commit_sha) for the most recent matching tag.
    """
    tags = SESSION.get(
        f"https://api.github.com/repos/{owner}/{repo}/tags", timeout=30
    ).json()
    viable = [t for t in tags if pattern.match(t["name"])]

    if not viable:
        raise RuntimeError(f"No matching tags for {owner}/{repo}")

    return viable[0]["name"], viable[0]["commit"]["sha"]


def update_wrap(path: Path) -> bool:
    cp = configparser.ConfigParser(interpolation=None)
    cp.read(path, encoding="utf-8")

    if "wrap-git" not in cp:
        return False

    w = cp["wrap-git"]
    url = w.get("url", "")
    rev = w.get("revision", "").strip()
    m = re.match(r".*github\.com[:/](?P<owner>[^/]+)/(?P<repo>[^/.]+)(?:\.git)?", url)
    if not (m and rev):
        return False

    owner, repo = m.group("owner"), m.group("repo")
    try:
        pattern = cp.get("update", "tag_regex", fallback=None)
        pattern = re.compile(pattern) if pattern else SEMVER_RE
        tag, sha = gh_latest_tag(owner, repo, pattern)
    except Exception as e:
        print(f"⏭  {path.name}: {e}")
        return False

    if sha.startswith(rev):
        print(f"✔  {path.name} already at {tag} ({sha})")
        return False

    print(f"⬆️  {path.name}: {rev} ➜ {tag} ({sha})")

    w["revision"] = sha

    with open(path, "w", encoding="utf-8") as file:
        cp.write(file)

        # XXX: ConfigParser writes two extra newlines. Trim the last one.
        file.seek(file.tell() - 1, 0)
        file.truncate()

    return True


def main():
    for wrap in WRAP_DIR.glob("*.wrap"):
        update_wrap(wrap)


if __name__ == "__main__":
    main()
