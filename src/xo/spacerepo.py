"""`xo create space NAME` with no KIND: the folder it runs in becomes a space, a git repository on
GitHub with one branch per folder.

Every folder under the current one becomes a branch called after it, holding that folder's files
as they are now (its .gitignore followed, secrets and workflows left out); `main` is the index: a
README table of the branches, xo-space.json with each branch's commit and one hash over all of
them, and an update script. The repository is .git here, made bare so that nothing in this folder
is ever checked out, written over or shown as changed; it is built with git's plumbing only (a
scratch index and commit-tree), and the folders' own .git, remotes and branches are never touched.

Its remote is github.com/OWNER/NAME (OWNER is your gh login unless --owner says otherwise). When
GitHub does not have it, xo gives the link to make it and waits, then pushes main and every branch
with your own git login. xo never reads, stores or prints a token: whether the repository exists is
asked anonymously, so a private one looks missing until the push itself.

Running it again makes a new commit only for a folder that changed and pushes again, never forced;
a folder that is gone keeps its branch (xo deletes nothing).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Callable

from xo import gitops
from xo.client import XoError

FILE = "xo-space.json"
SCHEMA = "xo-space/1"
MAIN = "main"
FILE_CAP = 100 * 1024 * 1024  # GitHub refuses any file larger
WARN_CAP = 50 * 1024 * 1024  # GitHub warns above this
# Never copied, whatever a folder's .gitignore says: environment files, keys and credential stores.
NEVER = [".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "*.jks", "*.keystore", "id_rsa*", "id_dsa*",
         "id_ecdsa*", "id_ed25519*", ".npmrc", ".pypirc", ".netrc", "_netrc", ".git-credentials", "*.kdbx",
         ".aws/", ".ssh/", ".gnupg/", ".docker/config.json", ".kube/config", "credentials.json",
         "service-account*.json", "*.tfstate", "*.tfstate.*", ".terraform/", "node_modules/", ".DS_Store"]
# What a committed secret looks like; a file holding one stops everything before anything is pushed.
SECRET = (r"-----BEGIN ([A-Z]+ )?PRIVATE KEY-----|\bgh[pousr]_[A-Za-z0-9]{36,}\b|\bgithub_pat_[A-Za-z0-9_]{60,}"
          r"|\bAKIA[0-9A-Z]{16}\b|\bxox[abprs]-[A-Za-z0-9-]{10,}|\bsk-(ant-|proj-)?[A-Za-z0-9_-]{32,}"
          r"|\bsk_live_[A-Za-z0-9]{24,}|\bAIza[0-9A-Za-z_-]{35}\b|\bglpat-[A-Za-z0-9_-]{20,}|\bak_[A-Za-z0-9]{24,}")
# Folders that hold keys or a whole home: never made a space, and never a branch.
_KEYS = (".ssh", ".aws", ".gnupg", ".kube", ".docker", ".azure", ".password-store", ".config/gcloud")
_BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
_IDENTITY = ["-c", "user.name=xo", "-c", "user.email=xo@localhost", "-c", "commit.gpgsign=false",
             "-c", f"core.hooksPath={os.devnull}"]


def _say(text: str) -> None:
    print(f"xo: {text}", file=sys.stderr)


class Space:
    """The space's repository: ./.git here, bare, with origin at GitHub."""

    def __init__(self, here: Path) -> None:
        self.here = here
        self.repo = here / ".git"

    def git(self, *args: str, tree: Path | None = None, check: bool = True, config: tuple = ()) -> str | None:
        where = ["--git-dir", str(self.repo)]
        if tree is not None:  # the folder a branch is made from: read only, never written
            where += ["--work-tree", str(tree), "-c", "core.bare=false"]
        return gitops.run(*config, *where, *args, cwd=self.here, check=check)

    def ours(self) -> bool:
        """Whether ./.git is a space repository xo made (bare, its main holds xo-space.json)."""
        if not self.repo.is_dir():
            return False
        bare = self.git("config", "--get", "core.bare", check=False)
        return (bare or "").strip() == "true" and \
            self.git("cat-file", "-e", f"refs/heads/{MAIN}:{FILE}", check=False) is not None


def refuse_here(here: Path) -> None:
    """A space is never made in a home folder, the root, or a folder that holds keys."""
    home = Path.home().resolve()
    real = here.resolve()
    if real == home or real == Path(real.anchor) or real in home.parents:
        raise XoError(f"{real} is your home folder or above it; run xo create space inside the folder whose "
                      "folders should become the space")
    held = [k for k in _KEYS if (here / k).exists()]
    if held:
        raise XoError(f"{real} holds {', '.join(held)}, which must never be pushed; run xo create space inside "
                      "the folder whose folders should become the space")


def folders(here: Path) -> list[Path]:
    """The folders that become branches: every folder here but hidden ones and links."""
    found = sorted((p for p in here.iterdir() if p.is_dir() and not p.is_symlink() and not p.name.startswith(".")),
                   key=lambda p: p.name.casefold())
    bad = [p.name for p in found if not _BRANCH.fullmatch(p.name) or p.name.casefold() == MAIN
           or p.name.endswith((".lock", "."))]
    if bad:
        raise XoError("these folders cannot be branch names (letters, digits, . _ -, starting with a letter or "
                      f"digit, and not {MAIN}): " + ", ".join(bad) + "; rename them or run elsewhere")
    seen: dict[str, str] = {}
    for p in found:
        if p.name.casefold() in seen:
            raise XoError(f"{seen[p.name.casefold()]} and {p.name} differ only in case, which GitHub cannot keep "
                          "apart as branches; rename one")
        seen[p.name.casefold()] = p.name
    return found


def size(folder: Path) -> int:
    """Bytes of the folder's files, not counting .git folders (a quick count, before .gitignore)."""
    total = 0
    for root, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if d != ".git" and not os.path.islink(os.path.join(root, d))]
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total


def shown(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n} B"


def _start(space: Space) -> None:
    if space.repo.exists() and not space.ours():
        raise XoError(f"{space.here.resolve()} is already a git repository (./.git); xo makes a space's "
                      "repository only where there is none, so run it in another folder")
    if not space.repo.exists():
        gitops.run("init", "--quiet", "--bare", f"--initial-branch={MAIN}", "--end-of-options", str(space.repo))
    (space.repo / "info").mkdir(exist_ok=True)
    (space.repo / "info" / "exclude").write_text("\n".join(NEVER) + "\n")


def _tip(space: Space, branch: str) -> str | None:
    out = space.git("rev-parse", "--verify", "--quiet", f"refs/heads/{branch}^{{commit}}", check=False)
    return out.strip() if out else None


def _commit(space: Space, branch: str, tree: str, message: str) -> str:
    """A commit of tree on branch, on top of the branch as it is; none when the tree is the same."""
    tip = _tip(space, branch)
    if tip is not None and space.git("rev-parse", f"{tip}^{{tree}}").strip() == tree:
        return tip
    parent = ["-p", tip] if tip else []
    sha = space.git("commit-tree", tree, *parent, "-m", message, config=tuple(_IDENTITY)).strip()
    space.git("update-ref", f"refs/heads/{branch}", sha, *([tip] if tip else ["0" * 40]))
    return sha


def _snapshot(space: Space, folder: Path) -> tuple[str, list[str]]:
    """The tree of folder's files, built in a scratch index: its .gitignore followed, NEVER left out,
    workflows and nested repositories dropped. The tree and what was left out."""
    index = space.repo / "index"
    index.unlink(missing_ok=True)
    # No filters (git-lfs and the like): the files go in as they are on disk.
    plain = ("-c", "core.autocrlf=false", "-c", "core.safecrlf=false", "-c", "filter.lfs.required=false",
             "-c", "filter.lfs.clean=", "-c", "filter.lfs.process=")
    try:
        space.git("add", "--all", "--", ".", tree=folder, config=plain)
        dropped = []
        staged = space.git("ls-files", "--stage", tree=folder) or ""
        for line in staged.splitlines():
            meta, _, path = line.partition("\t")
            if meta.startswith("160000"):  # a repository inside the folder: its files are not here
                space.git("rm", "--cached", "--quiet", "--", path, tree=folder)
                dropped.append(f"{path} (a git repository of its own)")
        flows = space.git("ls-files", "--", ".github/workflows", tree=folder) or ""
        if flows.strip():
            space.git("rm", "--cached", "-r", "--quiet", "--", ".github/workflows", tree=folder)
            dropped.append(".github/workflows (GitHub would run them on the space's repository)")
        tree = space.git("write-tree", tree=folder).strip()
    finally:
        index.unlink(missing_ok=True)
    return tree, dropped


def _check(space: Space, name: str, tree: str) -> None:
    """Stop before anything is pushed: a file GitHub refuses, a git-lfs pointer, or a secret."""
    big, lfs = [], []
    for line in (space.git("ls-tree", "-r", "-l", "--full-tree", tree) or "").splitlines():
        meta, _, path = line.partition("\t")
        parts = meta.split()
        if len(parts) == 4 and parts[1] == "blob" and parts[3].isdigit():
            if int(parts[3]) > FILE_CAP:
                big.append(f"{path} ({shown(int(parts[3]))})")
            elif int(parts[3]) < 200:
                head = space.git("cat-file", "blob", parts[2]) or ""
                if head.startswith("version https://git-lfs.github.com/spec/"):
                    lfs.append(path)
    if big:
        raise XoError(f"{name}: GitHub takes no file over {shown(FILE_CAP)}: " + ", ".join(big) +
                      "; add them to its .gitignore. Nothing was pushed")
    if lfs:
        raise XoError(f"{name}: git-lfs pointers, whose data would stay behind: " + ", ".join(lfs[:10]) +
                      "; nothing was pushed")
    found = space.git("grep", "-I", "-l", "-E", "-e", SECRET, tree, "--", check=False)
    if found:
        files = [line.partition(":")[2] for line in found.splitlines()]
        raise XoError(f"{name}: these files look like they hold a secret (a key or token): " + ", ".join(files[:10]) +
                      "; take it out or add them to its .gitignore. Nothing was pushed")


def digest(branches: dict[str, dict]) -> str:
    """The space's hash: sha256 over each branch's tree id, written one way. A tree id is git's hash of
    every file under it, so this one hash covers all of the space's data."""
    text = json.dumps({b: e["tree"] for b, e in branches.items()}, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


def _index(space: Space, name: str, owner: str, branches: dict[str, dict]) -> str:
    """main's tree: README.md, xo-space.json and update, written as blobs (nothing on disk)."""
    hashed = digest(branches)
    record = {"schema": SCHEMA, "space": name, "github": f"{owner}/{name}", "hash": hashed, "branches": branches}
    rows = "\n".join(f"| [{b}](../../tree/{b}) | {e['files']} | {shown(e['bytes'])} | `{e['commit'][:7]}` |"
                     for b, e in branches.items())
    readme = (f"# {name}\n\nAn xo space: each branch is one folder, as it was when `xo create space {name}` last "
              f"ran.\n\n| Branch | Files | Size | Commit |\n|---|---|---|---|\n{rows}\n\nHash of all of it: "
              f"`{hashed}` (sha256 over each branch's git tree id; see {FILE}).\n\nTo update it, run `xo create "
              f"space {name}` again in the folder that holds these folders (or `sh update` there, with this "
              "branch's `update` copied in).\n")
    script = ("#!/bin/sh\n# Snapshot every folder here again and push the branches that changed (xo create "
              f"space).\nexec xo create space {name} --owner {owner} \"$@\"\n")

    def blob(text: str) -> str:
        return gitops.run("--git-dir", str(space.repo), "hash-object", "-w", "--stdin", cwd=space.here,
                          input=text).strip()

    entries = (f"100644 blob {blob(readme)}\tREADME.md\n"
               f"100755 blob {blob(script)}\tupdate\n"
               f"100644 blob {blob(json.dumps(record, indent=2, sort_keys=True) + chr(10))}\t{FILE}\n")
    return gitops.run("--git-dir", str(space.repo), "mktree", cwd=space.here, input=entries).strip()


def build(here: Path, name: str, owner: str, chosen: list[Path]) -> dict:
    """Make or update the space's repository from the folders, all checked before main moves."""
    space = Space(here)
    _start(space)
    trees = {}
    for folder in chosen:
        tree, dropped = _snapshot(space, folder)
        _check(space, folder.name, tree)
        for d in dropped:
            _say(f"{folder.name}: left out {d}")
        trees[folder.name] = tree
    branches = {}
    for folder in chosen:
        sha = _commit(space, folder.name, trees[folder.name], f"{folder.name}: files as of xo create space {name}")
        listing = space.git("ls-tree", "-r", "-l", "--full-tree", trees[folder.name]) or ""
        sizes = [int(p[3]) for p in (line.partition("\t")[0].split() for line in listing.splitlines())
                 if len(p) == 4 and p[3].isdigit()]
        branches[folder.name] = {"commit": sha, "tree": trees[folder.name], "files": len(sizes), "bytes": sum(sizes)}
    old = _old_branches(space)
    for gone in sorted(set(old) - set(branches), key=str.casefold):
        _say(f"{gone}: no such folder here now; its branch is kept as it was")
        branches[gone] = old[gone]
    branches = dict(sorted(branches.items(), key=lambda kv: kv[0].casefold()))
    tree = _index(space, name, owner, branches)
    commit = _commit(space, MAIN, tree, f"xo create space {name}: {digest(branches)}")
    return {"space": name, "github": f"{owner}/{name}", "hash": digest(branches), "main": commit,
            "branches": {b: e["commit"] for b, e in branches.items()}}


def _old_branches(space: Space) -> dict[str, dict]:
    out = space.git("cat-file", "blob", f"refs/heads/{MAIN}:{FILE}", check=False)
    try:
        record = json.loads(out) if out else {}
    except ValueError:
        return {}
    found = record.get("branches") if isinstance(record, dict) else None
    return found if isinstance(found, dict) else {}


def on_github(owner: str, name: str) -> bool:
    """Whether github.com/OWNER/NAME answers, asked with no login at all (a private one looks missing)."""
    out = gitops.run("-c", "credential.helper=", "-c", "core.askPass=", "ls-remote", "--heads", "--end-of-options",
                     f"{gitops.GITHUB}/{owner}/{name}", check=False, env={"GIT_TERMINAL_PROMPT": "0"})
    return out is not None


def push(here: Path, owner: str, name: str) -> None:
    """main and every branch to github.com/OWNER/NAME with your own git login, never forced."""
    space = Space(here)
    url = f"{gitops.GITHUB}/{owner}/{name}"
    if space.git("remote", "get-url", "origin", check=False) is None:
        space.git("remote", "add", "origin", url)
    else:
        space.git("remote", "set-url", "origin", url)
    space.git("push", "--atomic", "--porcelain", "origin", "refs/heads/*:refs/heads/*")


def create(here: Path, name: str, owner: str, ask: Callable[[str], bool], wait: Callable[[str], bool]) -> dict:
    """The whole command after the name checks: refuse, show, ask, build, then push."""
    refuse_here(here)
    chosen = folders(here)
    if not chosen:
        raise XoError(f"{here.resolve()} has no folders to make branches of; run xo create space inside the folder "
                      "that holds them")
    exists = on_github(owner, name)
    lines = [f"  {f.name:<30} {shown(size(f)):>10}" + ("  (a git clone: its files only, not its history)"
                                                      if (f / ".git").exists() else "") for f in chosen]
    _say(f"xo create space {name} makes {here.resolve()} a space: each folder here becomes its own branch of "
         f"github.com/{owner}/{name}, and main lists them.\n" + "\n".join(lines) + "\n"
         "  Each folder's .gitignore is followed; .env files, keys and .github/workflows are left out, and a file "
         "that looks like it holds a secret stops it before anything is pushed. Everything else is pushed to "
         + (f"the repository github.com/{owner}/{name}, which is already there."
            if exists else f"a new public repository github.com/{owner}/{name}, which xo asks you to make.")
         + " Nothing in these folders is changed.")
    if not ask("Make this folder the space?"):
        raise XoError("nothing was made. To make a space of other folders, run xo create space inside the folder "
                      "that holds them")
    made = build(here, name, owner, chosen)
    _say(f"./.git: {len(made['branches'])} branches and main, {made['hash']}")
    if not on_github(owner, name):
        link = f"https://github.com/new?name={name}&owner={owner}&visibility=public"
        _say(f"github.com/{owner}/{name} does not exist yet (or is private). Make it here, empty (no README, license "
             f"or .gitignore):\n  {link}")
        if not wait("Press Enter once it exists, or n to stop here"):
            _say(f"not pushed; run xo create space {name} here again once it exists")
            made["pushed"] = False
            return made
    push(here, owner, name)
    _say(f"pushed main and {len(made['branches'])} branches to https://github.com/{owner}/{name}")
    made["pushed"] = True
    return made
