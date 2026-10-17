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
# The kinds of space XO's core API creates: `xo create space NAME KIND`, and the creates the server
# passes on (server.CORE_ROUTES is built from it). One list, so the CLI's kinds and the server's routes
# cannot drift apart.
SPACE_KINDS = ("claude-code", "codex", "antigravity", "hermes", "openclaw", "xo-cowork", "nebius-openclaw")
# XO's own space name: `xo create space NAME KIND` uses NAME as the space's name on XO too.
_XO_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,31}")
_XO_RESERVED = {"new", "create"}
SPACE_ID_MAX = 64  # what the server forwards in GET /spaces/{id}
SPACE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,%d}" % (SPACE_ID_MAX - 1))
GITHUB_URL = re.compile(r"https://github\.com/([^/\s]+)/([^/\s]+?)(?:\.git)?/?")

PROJECT_RULE = ("a project name is 1 to 64 letters, digits, spaces, _ . or - starting with a letter or "
                "digit, or a GitHub owner/repo")
SPACE_RULE = "a space name is 1 to 64 letters, digits, _ . or -, starting with a letter or digit"
XO_NAME_RULE = ("a new space's name is 1 to 32 letters, digits or hyphens, starting with a letter or digit, "
                "and not new or create (it is also the space's name on XO)")
REF_RULE = "a ref is a branch, tag or commit sha: letters, digits, . _ / -, starting with a letter or digit"


def is_github(name: str) -> bool:
    return "/" in name


# Folders a project never gets: .xo holds a space's own state.
_RESERVED_FOLDERS = {".xo"}


def short(project: str) -> str:
    """What a project is called: a GitHub project's repo name, a plain name as it is."""
    return project.partition("/")[2] if is_github(project) else project


def folder(project: str, projects: list | tuple = ()) -> str:
    """The folder a project gets among projects (the others in the same place): a GitHub project's
    repo name (juanfont/headscale is ./headscale), or OWNER/REPO, as before short names, when another
    of projects is called the same, the repo name is reserved, or the repo name is the OWNER folder
    another project needs (see _nested). A plain name as it is. The project is always recorded as
    OWNER/REPO."""
    if not is_github(project):
        return project
    return project if project.casefold() in _nested([*projects, project]) else short(project)


def owner_folders(projects: list | tuple) -> set[str]:
    """The OWNER folders (casefolded) that projects' OWNER/REPO folders sit in."""
    return {p.partition("/")[0] for p in _nested(projects)}


def _nested(projects: list | tuple) -> set[str]:
    """The GitHub projects (casefolded) that get OWNER/REPO: those whose repo name another project
    shares or is reserved, then, until none is left, each whose repo name is the OWNER folder one of
    those sits in, so no project's folder is ever inside another's (suraj/suraj beside suraj/react)."""
    every = {p.casefold() for p in projects if isinstance(p, str)}
    github = {p for p in every if is_github(p)}
    count: dict[str, int] = {}
    for p in every:
        count[short(p)] = count.get(short(p), 0) + 1
    nested = {p for p in github if short(p) in _RESERVED_FOLDERS or count[short(p)] > 1}
    while True:
        owners = {p.partition("/")[0] for p in nested}
        more = {p for p in github - nested if short(p) in owners}
        if not more:
            return nested
        nested |= more


def named(projects: list, typed: str) -> list[str]:
    """The projects a typed name means (any case): the project of exactly that name if there is one
    (OWNER/REPO, or a plain name); otherwise, for a plain name, each GitHub project whose repo has it."""
    key = typed.casefold()
    exact = [p for p in projects if isinstance(p, str) and p.casefold() == key]
    if exact or is_github(typed):
        return exact
    return [p for p in projects if isinstance(p, str) and is_github(p) and short(p).casefold() == key]


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


def xo_name_ok(name: str) -> bool:
    """A name XO takes for a new space, which `xo create space` also records it under."""
    return (isinstance(name, str) and bool(_XO_NAME.fullmatch(name)) and space_ok(name)
            and name.lower() not in _XO_RESERVED)


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
