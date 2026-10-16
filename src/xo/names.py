"""The names and refs xo's projects and spaces may hold.

The server checks them before writing metadata, and the CLI checks them again before git or the
filesystem sees one, since a name or ref can come from metadata someone else wrote. Neither side
imports the other: this module is all they share.
"""
from __future__ import annotations

import re

# A local project: #28's rule, so names already added stay valid. Spaces are allowed.
_PLAIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9 _.-]{0,63}")
# A GitHub project, owner/repo: GitHub's own limits, so every real repository fits.
_OWNER = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}")
_REPO = re.compile(r"[A-Za-z0-9_.][A-Za-z0-9_.-]{0,99}")
# A space: it becomes a folder name, so no spaces and no leading dot.
_SPACE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
# A branch, tag or 40-character commit sha; git's ref rules, never starting with "-".
_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}")
SPACE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}")
GITHUB_URL = re.compile(r"https://github\.com/([^/\s]+)/([^/\s]+?)(?:\.git)?/?")

PROJECT_RULE = ("a project name is 1 to 64 letters, digits, spaces, _ . or - starting with a letter or "
                "digit, or a GitHub owner/repo")
SPACE_RULE = "a space name is 1 to 64 letters, digits, _ . or -, starting with a letter or digit"
REF_RULE = "a ref is a branch, tag or commit sha: letters, digits, . _ / -, starting with a letter or digit"


def is_github(name: str) -> bool:
    return "/" in name


def project_ok(name: str) -> bool:
    if not isinstance(name, str):
        return False
    if "/" not in name:
        return bool(_PLAIN.fullmatch(name)) and not name.endswith(" ")
    owner, _, repo = name.partition("/")
    return (bool(_OWNER.fullmatch(owner)) and bool(_REPO.fullmatch(repo))
            and repo not in (".", "..") and not repo.lower().endswith(".git"))


def space_ok(name: str) -> bool:
    return isinstance(name, str) and bool(_SPACE.fullmatch(name))


def ref_ok(ref: str) -> bool:
    return (isinstance(ref, str) and bool(_REF.fullmatch(ref)) and ".." not in ref and "//" not in ref
            and "/." not in ref and "@{" not in ref and not ref.endswith(("/", ".", ".lock")))


def from_github_url(text: str) -> str | None:
    """owner/repo for an https://github.com/owner/repo URL, or None."""
    m = GITHUB_URL.fullmatch(text.strip())
    if not m:
        return None
    name = f"{m.group(1)}/{m.group(2)}"
    return name if project_ok(name) else None
