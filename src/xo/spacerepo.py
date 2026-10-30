"""`xo create space [NAME]` with no KIND: a folder becomes a space, a git repository on GitHub (or on
the quirq hub) with one branch per git repository under it. With no NAME the folder it runs in is the space, named after the
folder (or the name its space already has); with NAME, the folder ./NAME is.

Every git repository under the space's folder (at any depth, but not one inside another) becomes a
branch called after its path there (`api`, or `work/api`), holding that repository's files as they
are now, not its history (its .gitignore and the space folder's followed, secrets and workflows left
out). Folders that are not git repositories and hold none, and files outside the repositories, are
not part of the space. The index branch lists them: a README table of the branches, xo-space.json
with each branch's commit and one hash over all of them, and an update script. Branches are built
with git's plumbing only (a scratch index of xo's own and commit-tree), and the repositories' own
.git, remotes and branches are never touched.

The space's repository is the folder's .git. When there is none, xo makes it bare, so that nothing in
the folder is ever checked out, written over or shown as changed, and main is the index. When the
folder is already a git repository, xo attaches to it: it adds the space's branches and keeps the
index on branch xo-space, never changing your branches, index, staged changes, files or
.git/info/exclude, and refusing a repository whose branch would clash with one of yours.

With --github (or --owner), its remote is github.com/OWNER/NAME (OWNER is your gh login unless
--owner says otherwise), or, in an
attached repository, the GitHub repository its origin already is. When GitHub does not have it, xo
gives the link to make it and waits, then pushes the index and every branch with your own git login.
xo never reads, stores or prints a token: whether the repository exists is asked anonymously (no
credential helper, askpass, .netrc or extra header), so a private one looks missing until the push
itself, and a repository GitHub redirects to (renamed or moved) is refused.

Running it again makes a new commit only for a repository that changed and pushes again, never forced.
A commit is always made on top of what GitHub already has, never on one made here that was not
pushed, so files left out before the first push never reach GitHub in the history; what GitHub has
is asked each run, so a repository deleted and made again starts over. A repository that is gone keeps
the branch GitHub has of it (xo deletes nothing); one never pushed is dropped. Only the index and the
branches it lists are pushed.

Without --github, the space goes to the quirq hub xo login found (xo/quirq.py); with no hub, or
for a space already on GitHub, xo create space stops and says so. On the hub its remote is the hub project AGENT/NAME
(quirq://HOST/AGENT/NAME, the `quirq` remote, made on the hub when it is missing; an attached
repository is pushed by URL and its remotes are left alone), and its index records "hub" in place of
"github". Anyone can read the hub, so a folder or git repository whose origin is not public on
GitHub is refused before anything is made, and one with no origin, or whose built branch holds a
file its HEAD lacks (or whose HEAD is in no branch GitHub has now), needs the space's name typed at a
terminal before the push (never answered by --yes). Everything else is the same; the tips of the hub's branches are
kept under refs/remotes/quirq/ (refs/xo-space/hub/ in an attached repository), and origin is never
used or touched.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Callable, Iterator

from xo import gitops, quirq
from xo.client import XoError

FILE = "xo-space.json"
SCHEMA = "xo-space/1"
MAIN = "main"
ATTACHED_INDEX = "xo-space"  # the index's branch in a git repository that was there before xo (its main is yours)
MADE = "xo-space-branches.json"  # in an attached repository's .git: the branches xo made there (Space.made)
MARK = "xo.space"  # set in .git/config when xo makes the repository, so a run that stopped early can run again
FILE_CAP = 100 * 1024 * 1024  # GitHub refuses any file larger
# Never copied, whatever any .gitignore says (a negation there cannot bring them back), at any depth
# and in any case: environment files, keys and credential stores. A pattern ending in / is a folder.
NEVER = [".env", ".env.*", ".env-*", ".env_*", "*.env", ".envrc", "*.pem", "*.key", "*.p12", "*.pfx", "*.jks",
         "*.keystore", "*.ppk", "id_rsa*", "id_dsa*", "id_ecdsa*", "id_ed25519*", ".npmrc", ".pypirc", ".netrc",
         "_netrc", ".git-credentials", "git/credentials", "gh/hosts.yml", ".pgpass", ".my.cnf", ".s3cfg", ".boto",
         ".vault-token", "*.kdbx", ".aws/", ".ssh/", ".gnupg/", ".azure/", ".kube/", ".docker/", ".password-store/",
         ".config/gcloud/", "credentials.json", "service-account*.json", "application_default_credentials.json",
         "*.tfstate", "*.tfstate.*", ".terraform/", "node_modules/", ".DS_Store"]
# What a committed secret looks like, in a file's bytes or its name; one stops everything before
# anything is pushed. (?P<pw>) is a password in a URL, skipped when it is a placeholder.
SECRET = re.compile(
    rb"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----|PuTTY-User-Key-File-\d"
    rb"|\bgh[pousr]_[A-Za-z0-9]{36,}|\bgithub_pat_[A-Za-z0-9_]{60,}"
    rb"|\b(?:AKIA|ASIA)[0-9A-Z]{16}\b|(?i:aws_secret_access_key)\W{1,4}[A-Za-z0-9/+]{40}"
    rb"|\bxox[abprs]-[A-Za-z0-9-]{10,}|\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{32,}"
    rb"|\b[rs]k_(?:live|test)_[A-Za-z0-9]{24,}|\bsk_[a-f0-9]{48}\b"
    rb"|\bAIza[0-9A-Za-z_-]{35}|\bglpat-[A-Za-z0-9_-]{20,}|\bak_[A-Za-z0-9]{24,}"
    rb"|\bnpm_[A-Za-z0-9]{36}\b|\bhf_[A-Za-z0-9]{30,}|\bpypi-AgE[A-Za-z0-9_-]{50,}"
    rb"|\b[a-z][a-z0-9+.-]{1,20}://[^\s/:@'\"<>]{1,64}:(?P<pw>[^\s/@'\"<>]{3,128})@[A-Za-z0-9.-]+")
_PLACEHOLDER = re.compile(rb"(?i)[$<{%*]|^(?:pass(?:word|wd)?|pwd|secret|changeme|x+|\.+)$|example")
# Folders that hold keys or a whole home: never made a space, and never a branch.
_KEYS = (".ssh", ".aws", ".gnupg", ".kube", ".docker", ".azure", ".password-store", ".config/gcloud")
# Folders in a home folder where programs keep settings and logins (besides any hidden one).
_SETTINGS = ("Library", "AppData")
_BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")  # one part of a branch name (a folder's name)
_DEPTH = 6  # how deep xo looks for git repositories
_SKIP = ("node_modules", "venv", "__pycache__")  # folders never looked into for git repositories
_IDENTITY = ["-c", "user.name=xo", "-c", "user.email=xo@localhost", "-c", "commit.gpgsign=false",
             "-c", f"core.hooksPath={os.devnull}"]


def _say(text: str) -> None:
    print(f"xo: {text}", file=sys.stderr)


def _z(paths: list[str]) -> bytes:
    """Paths for git's -z --stdin, as the bytes git gave them (surrogateescape keeps any name)."""
    return b"".join(p.encode("utf-8", "surrogateescape") + b"\0" for p in paths)


class Space:
    """The space's repository: ./.git here, with origin at GitHub. Made bare when xo makes it; one that
    was there before (yours, with its own files and branches) is attached to: xo only adds its own
    branches, and never touches your branches, index, files or .git/info/exclude."""

    def __init__(self, here: Path) -> None:
        self.here = here.absolute()  # git runs in here, so --git-dir must not be relative to it
        self.repo = self.here / ".git"
        self._scratch: str | None = None
        self._attached: bool | None = None
        self.hub: quirq.Hub | None = None  # set when the space goes on the quirq hub, not GitHub

    def git(self, *args: str, tree: Path | None = None, check: bool = True, config: tuple = (),
            input: bytes | None = None, raw: bool = False, hub: bool = False) -> str | None:
        where = ["--git-dir", str(self.repo), "-c", f"core.hooksPath={os.devnull}"]
        env = None
        if tree is not None:  # the folder a branch is made from: read only, never written
            where += ["--work-tree", str(tree), "-c", "core.bare=false"]
            env = {"GIT_INDEX_FILE": str(self.scratch / "index")}  # never the repository's own index
        return gitops.run(*config, *where, *args, cwd=self.here, check=check, input=input, raw=raw, env=env, hub=hub)

    @property
    def scratch(self) -> Path:
        """A folder of xo's own for this run (the scratch index and exclude list), removed by close."""
        if self._scratch is None:
            self._scratch = tempfile.mkdtemp(prefix="xo-space-")
        return Path(self._scratch)

    def close(self) -> None:
        if self._scratch is not None:
            shutil.rmtree(self._scratch, ignore_errors=True)
            self._scratch = None

    @property
    def attached(self) -> bool:
        """Whether ./.git is a repository with a work tree that was there before xo (not one xo made bare)."""
        if self._attached is None:
            bare = self.git("rev-parse", "--is-bare-repository", check=False) if self.repo.is_dir() else None
            self._attached = (bare or "").strip() == "false"
        return self._attached

    @property
    def index(self) -> str:
        """The branch that is the space's index: main, or xo-space in a repository that was there before."""
        return ATTACHED_INDEX if self.attached else MAIN

    @property
    def tracking(self) -> str:
        """Where GitHub's (or the hub's) tips of the space's branches are kept: origin's (quirq's) in a
        repository xo made, a ref folder of xo's own in an attached one (so your origin's remote-tracking
        branches stay yours)."""
        if self.attached:
            return "refs/xo-space/hub/" if self.hub else "refs/xo-space/github/"
        return f"refs/remotes/{gitops.QUIRQ}/" if self.hub else "refs/remotes/origin/"

    @property
    def where(self) -> str:
        """The space's repository, in words, once build named it (github.com/OWNER/NAME or the hub's)."""
        return getattr(self, "_where", "GitHub")

    def made(self) -> dict[str, list[str]]:
        """In an attached repository, each branch xo made here and the commit(s) xo set it to, kept in .git
        (never read from GitHub): one, or two while xo is moving it (written before the move, so a run
        stopped in between can run again). A branch is xo's only while its tip is one of them."""
        try:
            data = json.loads((self.repo / MADE).read_text())
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {b: [c for c in cs if isinstance(c, str)] for b, cs in data.items()
                if isinstance(b, str) and isinstance(cs, list)}

    def record(self, branch: str, commits: list[str] | None) -> None:
        if not self.attached:
            return
        made = self.made()
        if not commits:
            made.pop(branch, None)
        else:
            made[branch] = commits
        tmp = self.repo / (MADE + ".tmp")
        tmp.write_text(json.dumps(made, indent=2, sort_keys=True) + "\n")
        os.replace(tmp, self.repo / MADE)

    def checked_out(self) -> set[str]:
        """The branches checked out here or in any linked worktree (maybe with no commit yet)."""
        out = self.git("symbolic-ref", "--quiet", "--short", "HEAD", check=False)
        found = {out.strip()} if out and out.strip() else set()
        for line in (self.git("worktree", "list", "--porcelain", check=False) or "").splitlines():
            if line.startswith("branch refs/heads/"):
                found.add(line.removeprefix("branch refs/heads/"))
        return found

    def symbolic(self, branch: str) -> bool:
        return self.git("symbolic-ref", "--quiet", f"refs/heads/{branch}", check=False) is not None

    def owned(self, branch: str) -> bool:
        """Whether xo may move or delete refs/heads/BRANCH: always in a repository xo made; in an attached
        one only while its tip is a commit xo set it to, it is not checked out in any worktree, and it is
        not a symbolic ref (moving it would move another branch)."""
        if not self.attached:
            return True
        tip = _tip(self, f"refs/heads/{branch}")
        return tip is not None and tip in self.made().get(branch, []) and not self.symbolic(branch) \
            and branch not in self.checked_out()

    def ours(self) -> bool:
        """Whether ./.git is a space repository xo made (bare, and marked so or its main holds xo-space.json)."""
        if not self.repo.is_dir():
            return False
        bare = self.git("config", "--get", "core.bare", check=False)
        return (bare or "").strip() == "true" and (
            self.git("config", "--get", MARK, check=False) is not None
            or self.git("cat-file", "-e", f"refs/heads/{MAIN}:{FILE}", check=False) is not None)


def _homes() -> set[Path]:
    """Your home folder, as $HOME says and (where there is one) as the system's user list says."""
    homes = {Path.home().resolve()}
    try:
        import pwd
        homes.add(Path(pwd.getpwuid(os.getuid()).pw_dir).resolve())
    except (ImportError, KeyError, OSError, AttributeError):
        pass
    return homes


def refuse_here(here: Path) -> None:
    """A space is never made in a home folder, the root, a folder that holds keys or is in one (or in
    any hidden or settings folder of your home), or inside another git repository. A git repository
    here (./.git) is attached to, unless it is a worktree or submodule of another one."""
    real = here.resolve()
    for home in _homes():
        if real == home or real == Path(real.anchor) or real in home.parents:
            raise XoError(f"{real} is your home folder or above it; run xo create space inside the folder that "
                          "holds the git repositories for the space")
        if home in real.parents:
            top = real.relative_to(home).parts[0]
            if top.startswith(".") or top in _SETTINGS:
                raise XoError(f"{real} is {'' if real == home / top else 'in '}{home / top}, where programs keep "
                              "settings, logins and keys, which must never be pushed; run xo create space inside "
                              "the folder that holds the git repositories for the space")
    held = [k for k in _KEYS if (here / k).exists()]
    if held:
        raise XoError(f"{real} holds {', '.join(held)}, which must never be pushed; run xo create space inside "
                      "the folder that holds the git repositories for the space")
    if (gitops.run("rev-parse", "--is-inside-git-dir", cwd=here, check=False) or "").strip() == "true":
        raise XoError(f"{real} is inside a git repository's own .git folder (or a bare repository), whose files "
                      "hold all of its history; run xo create space in a folder outside it")
    top = gitops.run("rev-parse", "--show-toplevel", cwd=here, check=False)
    if top is not None:
        if Path(top.strip()).resolve() == real:
            if (here / ".git").is_dir():
                return
            raise XoError(f"{real} is a worktree or submodule of another git repository (./.git is a file); run xo "
                          "create space in that repository's own folder, or in another folder")
        raise XoError(f"{real} is inside the git repository {Path(top.strip())}; a .git here would hide that "
                      "repository from git in these folders, so run xo create space in a folder outside it")


class _HereIgnore:
    """This folder's own .gitignore, read by git as if this folder were one repository (so the folders'
    .gitignore files count too, as git would count them)."""

    def __init__(self, here: Path) -> None:
        self.here = here
        self.on = (here / ".gitignore").is_file()
        self.tmp: str | None = None

    def ignored(self, paths: list[str]) -> set[str]:
        if not (self.on and paths):
            return set()
        if self.tmp is None:
            self.tmp = tempfile.mkdtemp(prefix="xo-space-")
            gitops.run("init", "--quiet", "--bare", "--end-of-options", self.tmp)
        try:
            out = gitops.run("--git-dir", self.tmp, "--work-tree", str(self.here), "-c", "core.bare=false",
                             "check-ignore", "--no-index", "-z", "--stdin", cwd=self.here, input=_z(paths), raw=True)
        except XoError as e:
            if getattr(e, "code", None) == 1:  # none of them is ignored
                return set()
            raise
        return {p for p in (out or "").split("\0") if p}

    def close(self) -> None:
        if self.tmp is not None:
            shutil.rmtree(self.tmp, ignore_errors=True)


def _is_repo(folder: Path) -> bool:
    """A git repository's work tree: .git is a folder (a clone) or a file (a worktree or submodule)."""
    return (folder / ".git").is_dir() or (folder / ".git").is_file()


def _branch_ok(branch: str) -> bool:
    """A branch name xo makes: each part a folder name git and GitHub both take."""
    return all(_BRANCH.fullmatch(part) and ".." not in part and part.upper() != "HEAD"
               and not part.endswith(("_HEAD", ".lock", ".")) for part in branch.split("/")) and \
        branch.split("/")[0].casefold() not in (MAIN, ATTACHED_INDEX) and len(branch) <= 200


def _inside(a: str, b: str) -> bool:
    """Whether branch b sits inside branch a's name (a/...), in any case: git cannot hold both."""
    return b.casefold().startswith(a.casefold() + "/")


def folders(here: Path, probe: _HereIgnore | None = None) -> tuple[dict[str, Path], list[str]]:
    """The git repositories that become branches, {branch: folder}: every repository under here (not
    one inside another, nor in hidden folders, links, node_modules or what this folder's .gitignore
    leaves out); and the folders left out by that .gitignore."""
    found: dict[str, Path] = {}
    skipped: list[str] = []

    def look(at: Path, rel: str, depth: int) -> None:
        kids = sorted((p for p in at.iterdir() if p.is_dir() and not p.is_symlink() and not p.name.startswith(".")
                       and p.name not in _SKIP), key=lambda p: p.name.casefold())
        names = [f"{rel}{p.name}/" for p in kids]
        ignored = probe.ignored(names) if probe else set()
        for p, name in zip(kids, names):
            if name in ignored:
                skipped.append(name.rstrip("/"))
            elif _is_repo(p):
                found[name.rstrip("/")] = p
            elif depth < _DEPTH:
                look(p, name, depth + 1)

    look(here, "", 1)
    bad = [b for b in found if not _branch_ok(b)]
    if bad:
        raise XoError("these git repositories cannot be branch names (each folder name letters, digits, . _ -, "
                      f"starting with a letter or digit, no .., not HEAD, and not {MAIN} or {ATTACHED_INDEX} or in a folder "
                      f"of those names): "
                      + ", ".join(bad) +
                      "; rename them or run elsewhere")
    seen: dict[str, str] = {}
    for b in found:
        if b.casefold() in seen:
            raise XoError(f"{seen[b.casefold()]} and {b} differ only in case, which GitHub cannot keep "
                          "apart as branches; rename one")
        seen[b.casefold()] = b
    inside = sorted(f"{a} and {b}" for a in found for b in found if _inside(a, b))
    if inside:
        raise XoError("one git repository's branch would sit inside another's name, which git cannot hold (the "
                      "folders differ only in case): " + ", ".join(inside) + "; rename one")
    return found, skipped


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


def _never(path: str) -> bool:
    """Whether a path (with /) is one NEVER leaves out: a file name anywhere, a run of folders and a
    file name at its end, or a folder (or run of folders) anywhere above it; case is ignored."""
    parts = path.casefold().split("/")
    for pattern in NEVER:
        want = pattern.casefold().rstrip("/").split("/")
        if pattern.endswith("/"):
            hay = parts[:-1]
            starts = range(len(hay) - len(want) + 1)
        else:
            hay = parts
            starts = range(len(hay) - len(want), len(hay) - len(want) + 1) if len(hay) >= len(want) else range(0)
        if any(all(fnmatchcase(hay[i + j], w) for j, w in enumerate(want)) for i in starts):
            return True
    return False


def _your_excludes(space: Space) -> list[str]:
    """The lines of your own excludes file (core.excludesFile, or git's default ~/.config/git/ignore),
    which xo's list replaces in an attached repository and so must carry on."""
    at = space.git("config", "--path", "--get", "core.excludesFile", check=False)
    if at and at.strip():
        path = Path(at.strip())
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
        path = Path(base) / "git" / "ignore"
    try:
        return path.read_text(errors="replace").splitlines() if path.is_file() else []
    except OSError:
        return []


def _exclude_lines() -> list[str]:
    """NEVER as git exclude patterns, matching at any depth (so those files are not even read)."""
    return [("**/" + p) if "/" in p.rstrip("/") else p for p in NEVER]


def _start(space: Space, name: str) -> None:
    if space.repo.is_file():
        raise XoError(f"{space.here.resolve()} is a worktree or submodule of another git repository (./.git is a "
                      "file); run xo create space in that repository's own folder, or in another folder")
    if space.attached:
        fmt = (space.git("rev-parse", "--show-object-format", check=False) or "").strip()
        if fmt != "sha1":
            raise XoError(f"{space.here.resolve()}/.git uses {fmt or 'an object format xo cannot read'}, which GitHub "
                          "does not take; nothing was changed")
        return
    if space.repo.exists() and not space.ours():
        raise XoError(f"{space.here.resolve()}/.git is a bare git repository xo did not make; run xo create space "
                      "in another folder")
    if not space.repo.exists():
        gitops.run("init", "--quiet", "--bare", "--object-format=sha1", f"--initial-branch={MAIN}",
                   "--end-of-options", str(space.repo))
        space.git("config", MARK, name)
    (space.repo / "info").mkdir(exist_ok=True)


def _tip(space: Space, ref: str) -> str | None:
    out = space.git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False)
    return out.strip() if out else None


def _commit(space: Space, branch: str, tree: str, message: str) -> str:
    """A commit of tree on branch, on top of what GitHub has of it (origin's branch as last pushed), never
    on a commit made here that was not pushed: that one could hold files left out since. None is made
    when nothing changed."""
    tip = _tip(space, f"refs/heads/{branch}")
    base = _tip(space, f"{space.tracking}{branch}")

    def tree_of(commit: str) -> str:
        return space.git("rev-parse", f"{commit}^{{tree}}").strip()

    if tip is not None and tree_of(tip) == tree:
        if tip == base or space.git("rev-list", "--parents", "-n", "1", tip).split()[1:] == ([base] if base else []):
            return tip
    if base is not None and tree_of(base) == tree:
        sha = base
    else:
        sha = space.git("commit-tree", tree, *(["-p", base] if base else []), "-m", message,
                        config=tuple(_IDENTITY)).strip()
    if sha != tip:
        _move(space, branch, sha, tip)
    else:
        space.record(branch, [sha])
    return sha


def _move(space: Space, branch: str, new: str | None, tip: str | None) -> None:
    """Point refs/heads/BRANCH at new (None deletes it), only if it is still at tip. In an attached
    repository it must also still be the commit xo last set it to and not be checked out anywhere, checked
    again here, right before the change: you may have checked it out or committed on it while xo ran."""
    changed = XoError(f"{branch} changed while xo ran (or is no longer where xo left it); xo never moves a branch "
                      "it did not set, so nothing was pushed. Run again")
    if space.attached:
        if branch in space.checked_out():
            raise XoError(f"{branch} was checked out while xo ran; xo never moves a branch you are on, so nothing "
                          "was pushed. Run again")
        if space.symbolic(branch):
            raise changed
        tip = _tip(space, f"refs/heads/{branch}")
        if tip is not None and tip not in space.made().get(branch, []):
            raise changed
        space.record(branch, [c for c in (tip, new) if c])  # before the move: a stop in between can run again
    ref = f"refs/heads/{branch}"
    try:
        if new is None:
            space.git("update-ref", "--no-deref", "-d", ref, tip or "")
        else:
            space.git("update-ref", "--no-deref", ref, new, tip or "")
    except XoError:
        raise changed from None
    space.record(branch, [new] if new else None)


def _staged(space: Space, folder: Path) -> list[tuple[str, str]]:
    out = space.git("ls-files", "-z", "--stage", tree=folder, raw=True) or ""
    return [(meta, path) for meta, _, path in (rec.partition("\t") for rec in out.split("\0") if rec)]


def _snapshot(space: Space, branch: str, folder: Path, probe: _HereIgnore | None = None) -> tuple[str, list[str]]:
    """The tree of folder's files, built in a scratch index: its .gitignore (and this folder's, and a
    clone's own .git/info/exclude) followed, NEVER left out, workflows and nested repositories dropped.
    The tree and what was left out. The index is xo's own scratch file, never the repository's."""
    index = space.scratch / "index"
    index.unlink(missing_ok=True)
    exclude = _exclude_lines()
    own = None
    if (folder / ".git").exists():  # a clone: its own exclude, also where .git is a file (worktree, submodule)
        at = gitops.run("rev-parse", "--git-path", "info/exclude", cwd=folder, check=False)
        own = folder / at.strip() if at and at.strip() else None
    if own is not None and own.is_file():
        exclude += ["# the folder's own .git/info/exclude", *own.read_text(errors="replace").splitlines()]
    # No filters (git-lfs and the like): the files go in as they are on disk. No fsmonitor, split index or
    # untracked cache from the repository's config: they belong to its own work tree, not this folder.
    plain = ("-c", "core.autocrlf=false", "-c", "core.safecrlf=false", "-c", "filter.lfs.required=false",
             "-c", "filter.lfs.clean=", "-c", "filter.lfs.process=", "-c", "core.fsmonitor=false",
             "-c", "core.splitIndex=false", "-c", "core.untrackedCache=false")
    if space.attached:  # your .git/info/exclude stays yours: xo's list goes in a file of its own
        listed = space.scratch / "exclude"
        listed.write_text("\n".join(exclude + ["# your own excludes file", *_your_excludes(space)]) + "\n")
        plain += ("-c", f"core.excludesFile={listed}")
    else:
        (space.repo / "info" / "exclude").write_text("\n".join(exclude) + "\n")
    try:
        try:
            space.git("add", "--all", "--", ".", tree=folder, config=plain)
        except XoError as e:
            if "does not have a commit checked out" in (getattr(e, "detail", "") or ""):
                raise XoError(f"{branch}: it holds a git repository with no commit yet; commit in it or move "
                              "it, then run again. Nothing was pushed") from None
            raise
        dropped, drop, flows, never = [], [], False, 0
        staged = _staged(space, folder)
        for meta, path in staged:
            if meta.startswith("160000"):  # a repository inside the folder: its files are not here
                drop.append(path)
                dropped.append(f"{path} (a git repository of its own)")
            elif path == ".github/workflows" or path.startswith(".github/workflows/"):
                drop.append(path)
                flows = True
            elif _never(path):
                drop.append(path)
                never += 1
        if flows:
            dropped.append(".github/workflows (GitHub would run them on the space's repository)")
        if never:
            dropped.append(f"{never} file(s) xo never pushes (.env files, keys, credential stores)")
        gone = set(drop)
        keep = [path for _, path in staged if path not in gone]
        here_ignored = probe.ignored([f"{branch}/{p}" for p in keep]) if probe else set()
        if here_ignored:
            out = [p for p in keep if f"{branch}/{p}" in here_ignored]
            drop += out
            dropped.append(f"{len(out)} file(s) this folder's .gitignore leaves out")
            keep = [p for p in keep if f"{branch}/{p}" not in here_ignored]
        if drop:
            space.git("update-index", "--force-remove", "-z", "--stdin", tree=folder, input=_z(drop))
        _refuse_encrypted(space, branch, folder, keep)
        _refuse_bare(branch, folder, keep)
        tree = space.git("write-tree", tree=folder).strip()
    finally:
        index.unlink(missing_ok=True)
    return tree, dropped


def _refuse_encrypted(space: Space, branch: str, folder: Path, paths: list[str]) -> None:
    """Stop on files a git filter keeps encrypted in their repository (git-crypt, transcrypt): here
    they are decrypted, and xo would push them so."""
    if not paths:
        return
    out = space.git("check-attr", "-z", "--stdin", "filter", tree=folder, input=_z(paths), raw=True) or ""
    fields = out.split("\0")
    bad = [fields[i] for i in range(0, len(fields) - 2, 3)
           if fields[i + 2] not in ("unspecified", "unset", "set", "lfs")]
    if bad:
        raise XoError(f"{branch}: " + ", ".join(bad[:10]) + " go through a git filter (such as git-crypt) that "
                      "keeps them encrypted in their repository; here they are decrypted, and xo would push them "
                      "so. Add them to its .gitignore. Nothing was pushed")


def _refuse_bare(branch: str, folder: Path, paths: list[str]) -> None:
    """Stop on a bare git repository among the files: pushing it would publish its whole history."""
    for p in paths:
        if p == "HEAD" or p.endswith("/HEAD"):
            where = folder / p[:-4] if p != "HEAD" else folder
            if (where / "objects").is_dir() and (where / "config").is_file():
                shown_at = p[:-5] or "the folder itself"
                raise XoError(f"{branch}: {shown_at} is a git repository (a bare one), and pushing it would "
                              "publish its whole history; move it out or add it to its .gitignore. Nothing was "
                              "pushed")


def _blobs(space: Space, shas: list[str]) -> Iterator[tuple[str, bytes]]:
    """(sha, bytes) of each blob, read with one git cat-file --batch."""
    if not shas:
        return
    proc = subprocess.Popen(["git", *gitops.SAFE, "--git-dir", str(space.repo), "cat-file", "--batch"],
                            cwd=space.here, env=gitops.environment(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL)

    def feed() -> None:
        try:
            proc.stdin.write("".join(s + "\n" for s in shas).encode())
            proc.stdin.close()
        except OSError:
            pass
    writer = threading.Thread(target=feed, daemon=True)
    writer.start()
    try:
        for _ in shas:
            head = proc.stdout.readline().split()
            if len(head) != 3 or head[1] != b"blob":
                raise XoError("git cat-file gave an answer xo cannot read; nothing was pushed")
            size_ = int(head[2])
            data = proc.stdout.read(size_)
            proc.stdout.read(1)
            if len(data) != size_:
                raise XoError("git cat-file stopped early; nothing was pushed")
            yield head[0].decode(), data
    finally:
        proc.stdout.close()
        writer.join()
        proc.wait()


def _secret(data: bytes) -> bool:
    """Whether data holds a secret; text in UTF-16 (as Windows PowerShell writes it) is read with its
    NUL bytes taken out."""
    for text in (data, data.replace(b"\0", b"")) if b"\0" in data else (data,):
        for m in SECRET.finditer(text):
            if m.group("pw") is None or not _PLACEHOLDER.search(m.group("pw")):
                return True
    return False


def _check(space: Space, name: str, tree: str) -> None:
    """Stop before anything is pushed: a file GitHub refuses, a git-lfs pointer, or a secret in any
    file (text or not) or in a file's name. If the check itself fails, nothing is pushed."""
    entries = []
    for rec in (space.git("ls-tree", "-r", "-l", "-z", "--full-tree", tree, raw=True) or "").split("\0"):
        meta, _, path = rec.partition("\t")
        parts = meta.split()
        if len(parts) == 4 and parts[1] == "blob" and parts[3].isdigit():
            entries.append((path, parts[2], int(parts[3])))
    big = [f"{p} ({shown(n)})" for p, _, n in entries if n > FILE_CAP]
    if big:
        raise XoError(f"{name}: GitHub takes no file over {shown(FILE_CAP)}: " + ", ".join(big) +
                      "; add them to its .gitignore. Nothing was pushed")
    paths: dict[str, list[str]] = {}
    for p, sha, _ in entries:
        paths.setdefault(sha, []).append(p)
    lfs, found = [], [p for p, _, _ in entries if _secret(p.encode("utf-8", "surrogateescape"))]
    for sha, data in _blobs(space, list(paths)):
        if len(data) < 200 and data.startswith(b"version https://git-lfs.github.com/spec/"):
            lfs += paths[sha]
        if _secret(data):
            found += paths[sha]
    if lfs:
        raise XoError(f"{name}: git-lfs pointers, whose data would stay behind: " + ", ".join(lfs[:10]) +
                      "; nothing was pushed")
    if found:
        raise XoError(f"{name}: these files look like they hold a secret (a key, token or password), in them or in "
                      "their name: " + ", ".join(sorted(set(found))[:10]) + "; take it out or add them to its "
                      ".gitignore. Nothing was pushed")


def digest(branches: dict[str, dict]) -> str:
    """The space's hash: sha256 over each branch's tree id, written one way. A tree id is git's hash of
    every file under it, so this one hash covers all of the space's data."""
    text = json.dumps({b: e["tree"] for b, e in branches.items()}, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


def _index(space: Space, name: str, github: str, branches: dict[str, dict]) -> str:
    """main's tree: README.md, xo-space.json and update, written as blobs (nothing on disk). Bytes in and
    -z for mktree, so no line ending is ever changed on the way (Windows turns \\n into \\r\\n in text).
    On the hub, github is the space's quirq URL, kept as "hub" in xo-space.json."""
    hashed = digest(branches)
    record = {"schema": SCHEMA, "space": name, **({"hub": github} if space.hub else {"github": github}),
              "hash": hashed, "branches": branches}
    rows = "\n".join(f"| [{b}](../../tree/{b}) | {e['files']} | {shown(e['bytes'])} | `{e['commit'][:7]}` |"
                     for b, e in branches.items())
    readme = (f"# {name}\n\nAn xo space: each branch is one git repository's files, as they were when `xo create "
              f"space` last ran in the space's folder.\n\n| Branch | Files | Size | Commit |\n|---|---|---|---|\n{rows}\n\n"
              f"Hash of all of it: `{hashed}` (sha256 over each branch's git tree id; see {FILE}).\n\nTo update it, run "
              "`xo create space` again in the space's folder, the one that holds these repositories (or `sh update` "
              "there, with this branch's `update` copied in).\n")
    script = ("#!/bin/sh\n# Snapshot every git repository here again and push the branches that changed (xo create "
              "space, run in the space's folder).\nexec xo create space " +
              ("" if space.hub else f"--owner {github.split('/')[0]} ") + "\"$@\"\n")

    def blob(text: str) -> str:
        return space.git("hash-object", "-w", "--stdin", input=text.encode()).strip()

    entries = (f"100644 blob {blob(readme)}\tREADME.md\0"
               f"100755 blob {blob(script)}\tupdate\0"
               f"100644 blob {blob(json.dumps(record, indent=2, sort_keys=True) + chr(10))}\t{FILE}\0")
    return space.git("mktree", "-z", input=entries.encode()).strip()


def _entry(space: Space, commit: str) -> dict:
    tree = space.git("rev-parse", f"{commit}^{{tree}}").strip()
    listing = space.git("ls-tree", "-r", "-l", "--full-tree", tree) or ""
    sizes = [int(p[3]) for p in (line.partition("\t")[0].split() for line in listing.splitlines())
             if len(p) == 4 and p[3].isdigit()]
    return {"commit": commit, "tree": tree, "files": len(sizes), "bytes": sum(sizes)}


def _sync_origin(space: Space, remote: dict[str, str], names: set[str]) -> None:
    """Make origin's branches here that the space uses (names) exactly what GitHub has now (remote), so
    nothing is built on, or kept from, a repository that was deleted, made again or is another one.
    Other branches are left as they are; in an attached repository these refs are xo's own (Space.tracking)."""
    have = space.git("for-each-ref", "--format=%(objectname) %(refname)", space.tracking) or ""
    for line in have.splitlines():
        sha, _, ref = line.partition(" ")
        branch = ref.removeprefix(space.tracking)
        if branch in names and remote.get(branch) != sha:
            space.git("update-ref", "-d", ref, sha)
    for branch, sha in remote.items():
        if branch in names and (branch == space.index or _branch_ok(branch)) and \
                space.git("cat-file", "-e", f"{sha}^{{commit}}", check=False) is not None:
            space.git("update-ref", f"{space.tracking}{branch}", sha)


def build(here: Path, name: str, owner: str, chosen: dict[str, Path], probe: _HereIgnore | None = None,
          remote: dict[str, str] | None = None, repo: str | None = None, hub: quirq.Hub | None = None) -> dict:
    """Make or update the space's repository from the repositories ({branch: folder}), all checked before
    the index moves. remote: the branches GitHub (or, with hub, the hub) has now (none when it has no
    such public repository); repo: the GitHub repository's name (NAME unless an attached repository's
    origin says otherwise)."""
    space = Space(here)
    space.hub = hub
    space._where = f"the hub's {name} ({hub.space_url(name)})" if hub else f"github.com/{owner}/{repo or name}"
    try:
        return _build(space, name, hub.space_url(name) if hub else f"{owner}/{repo or name}", chosen, probe, remote)
    finally:
        space.close()


def _theirs(space: Space, remote: dict[str, str]) -> set[str]:
    """In an attached repository, the branches that are yours, not xo's: here, every branch whose tip is
    not the commit xo last set it to, and the one checked out (maybe with no commit yet); on GitHub,
    every branch whose tip is not one xo set here."""
    local = space.git("for-each-ref", "--format=%(refname:strip=2)", "refs/heads/") or ""
    made = space.made()
    return ({b for b in local.splitlines() if b and not space.owned(b)} | space.checked_out()
            | {b for b, sha in remote.items() if sha not in made.get(b, [])})


def _build(space: Space, name: str, github: str, chosen: dict[str, Path], probe: _HereIgnore | None,
           remote: dict[str, str] | None) -> dict:
    _start(space, name)
    index = space.index
    if space.attached:
        theirs = _theirs(space, remote or {})
        if index in theirs:
            raise XoError(f"{space.here.resolve()}/.git (or {space.where}) already has a branch {index} that "
                          "xo did not make here, and xo keeps the space's index there; rename that branch, then run "
                          "again. Nothing was changed")
        clash = sorted(b for b in chosen for t in theirs
                       if b.casefold() == t.casefold() or _inside(b, t) or _inside(t, b))
        if clash:
            raise XoError("these git repositories would be branches that clash with your own branches (here or on "
                          f"{space.where}): " + ", ".join(clash) + "; xo never changes your branches, so rename "
                          "the folder or the branch, then run again. Nothing was changed")
        if space.git("config", "--get", MARK, check=False) is None:
            space.git("config", MARK, name)
    if remote is not None:
        _sync_origin(space, remote, {index, *chosen, *_old_branches(space)})
    trees = {}
    for branch, folder in chosen.items():
        tree, dropped = _snapshot(space, branch, folder, probe)
        _check(space, branch, tree)
        for d in dropped:
            _say(f"{branch}: left out {d}")
        trees[branch] = tree
    old = _old_branches(space)
    clash = {b.casefold(): b for b in old if b not in trees and b.casefold() in {t.casefold() for t in trees}}
    if clash:
        raise XoError("these repositories differ only in case from a branch the space already has: " +
                      ", ".join(clash.values()) + "; GitHub cannot keep them apart, so rename the folder back")
    kept = {}
    for gone in sorted(set(old) - set(trees), key=str.casefold):
        if not space.owned(gone):  # an attached repository's branch that is no longer xo's: never touched
            _say(f"{gone}: no such git repository here now, and the branch is no longer xo's; left as it is, out "
                 "of the space")
            continue
        base = _tip(space, f"{space.tracking}{gone}") if _branch_ok(gone) else None
        if base is not None and space.attached and base not in space.made().get(gone, []):
            _say(f"{gone}: no such git repository here now, and GitHub's branch is not the one xo pushed; left as "
                 "it is, out of the space")
            continue
        if base is None:  # never pushed, or not on GitHub now: it goes nowhere
            tip = _tip(space, f"refs/heads/{gone}")
            if tip is not None:
                _move(space, gone, None, tip)
            _say(f"{gone}: no such git repository here now, and GitHub does not have its branch; left out")
            continue
        kept[gone] = base
    nested = sorted(f"{o} and {n}" for o in kept for n in trees if _inside(o, n) or _inside(n, o))
    if nested:
        raise XoError("a branch cannot sit inside another branch's name, as these would: " + ", ".join(nested) +
                      "; GitHub has the first and xo deletes nothing, so delete that branch on GitHub (or move "
                      "the repository), then run again. Nothing was pushed")
    branches = {}
    for branch in chosen:
        sha = _commit(space, branch, trees[branch], f"{branch}: files as of xo create space {name}")
        branches[branch] = _entry(space, sha)
    for gone, base in kept.items():
        tip = _tip(space, f"refs/heads/{gone}")
        if tip != base:
            _move(space, gone, base, tip)
        _say(f"{gone}: no such git repository here now; its branch is kept as GitHub has it")
        branches[gone] = _entry(space, base)
    branches = dict(sorted(branches.items(), key=lambda kv: kv[0].casefold()))
    tree = _index(space, name, github, branches)
    commit = _commit(space, index, tree, f"xo create space {name}: {digest(branches)}")
    return {"space": name, ("hub" if space.hub else "github"): github, "hash": digest(branches), "index": index, index: commit,
            "branches": {b: e["commit"] for b, e in branches.items()}}


def _old_branches(space: Space) -> dict[str, dict]:
    """The branches the space's index here lists (in an attached repository, only while the index is
    still xo's; never what GitHub's copy says)."""
    if not space.owned(space.index):
        return {}
    out = space.git("cat-file", "blob", f"refs/heads/{space.index}:{FILE}", check=False)
    try:
        record = json.loads(out) if out else {}
    except ValueError:
        return {}
    found = record.get("branches") if isinstance(record, dict) else None
    return found if isinstance(found, dict) else {}


def published(here: Path) -> dict[str, str]:
    """Where the space in here/.git was last put, as its index here says: {"github": "OWNER/REPO"} or
    {"hub": URL}, or {} (no space of xo's here yet, or an index xo cannot read)."""
    if not (here / ".git").is_dir():
        return {}
    space = Space(here)
    if not space.owned(space.index):
        return {}
    out = space.git("cat-file", "blob", f"refs/heads/{space.index}:{FILE}", check=False)
    try:
        record = json.loads(out) if out else {}
    except ValueError:
        return {}
    for key in ("hub", "github"):
        if isinstance(record, dict) and isinstance(record.get(key), str):
            return {key: record[key]}
    return {}


def on_github(owner: str, name: str) -> bool:
    """Whether github.com/OWNER/NAME answers someone with no login at all (see heads)."""
    return heads(owner, name) is not None


def heads(owner: str, name: str) -> dict[str, str] | None:
    """The branches github.com/OWNER/NAME has ({name: sha}), asked with no login at all: no credential
    helper, askpass, .netrc, extra header or user config (so a private one looks missing: None). A
    repository GitHub redirects to (renamed or moved, maybe to another owner) is refused: xo pushes
    only to the one it names."""
    empty = tempfile.mkdtemp(prefix="xo-anon-")
    env = {"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "", "SSH_ASKPASS": "", "HOME": empty,
           "XDG_CONFIG_HOME": empty, "GIT_CONFIG_GLOBAL": os.devnull}
    try:
        out = gitops.run("-c", "credential.helper=", "-c", "core.askPass=", "-c", "http.extraHeader=",
                         "-c", "http.followRedirects=false", "ls-remote", "--heads", "--end-of-options",
                         f"{gitops.GITHUB}/{owner}/{name}", cwd=Path(empty), env=env)
    except XoError as e:
        if re.search(r"returned error: 30[1278]", getattr(e, "detail", "") or ""):
            raise XoError(f"GitHub sends github.com/{owner}/{name} on to another repository (it was renamed or moved); "
                          "xo pushes only to the repository it names, so pick another NAME") from None
        return None
    finally:
        shutil.rmtree(empty, ignore_errors=True)
    found = {}
    for line in (out or "").splitlines():
        sha, _, ref = line.partition("\t")
        if ref.startswith("refs/heads/"):
            found[ref.removeprefix("refs/heads/")] = sha.strip()
    return found


def hub_heads(here: Path, hub: quirq.Hub, name: str) -> dict[str, str] | None:
    """The branches the hub project AGENT/NAME has ({name: sha}), or None when there is no such space."""
    try:
        out = gitops.run("ls-remote", "--heads", "--end-of-options", hub.space_url(name), cwd=here.absolute(), hub=True)
    except XoError as e:
        if re.search(r"does not exist|does not appear to be a git repository", getattr(e, "detail", "") or ""):
            return None
        raise
    found = {}
    for line in (out or "").splitlines():
        sha, _, ref = line.partition("\t")
        if ref.startswith("refs/heads/"):
            found[ref.removeprefix("refs/heads/")] = sha.strip()
    return found


def push_hub(here: Path, hub: quirq.Hub, name: str, refs: dict[str, str]) -> None:
    """push for a space on the hub: the index and the space's branches to the hub project AGENT/NAME,
    never forced. In a repository xo made, its remote is quirq (added when there is none; one that
    points elsewhere is refused). An attached one is pushed by URL, and its remotes are not touched."""
    space = Space(here)
    url = hub.space_url(name)
    if not space.attached:
        have = (space.git("remote", "get-url", quirq.REMOTE, check=False) or "").strip()
        if not have:
            space.git("remote", "add", "--no-tags", quirq.REMOTE, url)
        elif have != url:
            raise XoError(f"{here.resolve()}/.git has a {quirq.REMOTE} remote that is not {url}; remove it (git "
                          f"remote remove {quirq.REMOTE} there), then run again. Nothing was pushed")
    to = url if space.attached else quirq.REMOTE  # by URL in yours: your remote-tracking refs stay yours
    try:
        _push(space, to, refs, ("-c", "push.gpgSign=false"), hub=True)
    except XoError as e:
        why = getattr(e, "detail", "") or ""
        if "Updates were rejected" in why or "non-fast-forward" in why or "fetch first" in why:
            raise XoError(f"the hub's space {name} has commits xo did not make here (pushed from another folder?), so "
                          "nothing was pushed; run xo create space in the folder it came from, or pick another NAME") \
                from None
        raise


def visibility(folder: Path) -> tuple[str, dict[str, str]]:
    """Whether the git repository at folder is one anyone can read on GitHub: "public" (every origin
    URL is one github.com repository that answers with no login), "none" (no origin) or "private"
    (anything else: a private or missing repository, another host, more than one repository). URLs are
    never shown, since they can hold a login. With "public", the branches GitHub has now ({name: sha})."""
    # The URLs as written (not remote get-url, which applies url.*.insteadOf and could make a private
    # origin read as a public one).
    found_urls = [u.strip() for key in ("remote.origin.url", "remote.origin.pushurl")
                  for u in (gitops.run("config", "--get-all", key, cwd=folder.absolute(), check=False)
                            or "").splitlines() if u.strip()]
    if not found_urls:
        return "none", {}
    matched = [_github_url(u) for u in found_urls]
    found = {(m.group(1).casefold(), m.group(2).casefold()) for m in matched if m}
    if not all(matched) or len(found) != 1:
        return "private", {}
    owner, repo = found.pop()
    try:
        now = heads(owner, repo)
    except XoError:  # GitHub sends it on elsewhere: not the repository its origin names
        return "private", {}
    return ("public", now) if now is not None else ("private", {})


def _hub_check(here: Path, chosen: dict[str, Path], attached: bool) -> dict[str, tuple[str, dict[str, str]]]:
    """Before anything is made for the hub, which anyone can read: refuse a folder, or a git repository in
    it, whose origin is not public on GitHub. The repositories with no origin at all, which xo cannot
    check, and those whose files hold what GitHub does not have (_create asks for the space's name to be
    typed for these)."""
    if attached and visibility(here)[0] == "private":
        raise XoError(f"{here.resolve()}/.git has an origin that is not a public GitHub repository, and anyone can "
                      "read the hub, so nothing was made; keep the space on GitHub with --github")
    looked = {b: visibility(f) for b, f in chosen.items()}
    private = sorted(b for b, (v, _) in looked.items() if v == "private")
    if private:
        raise XoError("these git repositories have an origin that is not a public GitHub repository, and anyone can "
                      f"read the hub, so nothing was made: {', '.join(private)}; move them out of the folder, or keep "
                      "the space on GitHub with --github")
    return looked


def _files(out: str | None) -> set[tuple[str, str]] | None:
    """(path, blob) for each file in ls-tree -r -z output, or None when git failed."""
    if out is None:
        return None
    found = set()
    for entry in out.split("\0"):
        meta, _, path = entry.partition("\t")
        if path and len(meta.split()) == 3 and meta.split()[1] == "blob":
            found.add((path, meta.split()[2]))
    return found


def _beyond_github(space: Space, commit: str, folder: Path, github: dict[str, str]) -> bool:
    """Whether the branch xo built (commit) holds anything the repository's HEAD does not, or HEAD is in
    none of the branches GitHub has now (github, just asked with no login; never the refs here, which
    can be stale or set by hand). The tree xo built is compared, not git status, so no index flag
    (assume-unchanged, skip-worktree) or repository config can hide a file from this check. Anything git
    cannot answer counts as beyond."""
    built = _files(space.git("ls-tree", "-r", "-z", "--full-tree", commit, check=False, raw=True))
    head = _files(gitops.run("ls-tree", "-r", "-z", "--full-tree", "HEAD", cwd=folder.absolute(), check=False,
                             raw=True))
    if built is None or head is None or not built <= head:
        return True
    return not any(gitops.run("merge-base", "--is-ancestor", "HEAD", sha, cwd=folder.absolute(), check=False)
                   is not None for sha in set(github.values()))  # a sha not here fails: not counted


def _github_url(url: str) -> re.Match | None:
    """OWNER and REPO of a GitHub URL in the https, git@ or ssh:// form, with no login in it."""
    return re.fullmatch(f"(?:{re.escape(gitops.GITHUB)}/|https://github\\.com/|git@github\\.com:|ssh://git@github\\.com/)"
                        r"([A-Za-z0-9][A-Za-z0-9-]{0,38})/([A-Za-z0-9._-]{1,100}?)(?:\.git)?/?", url, re.IGNORECASE)


def existing_name(here: Path) -> str | None:
    """The name the space in here/.git already has (xo marks its repository with it), if any."""
    if not (here / ".git").is_dir():
        return None
    out = Space(here).git("config", "--get", MARK, check=False)
    return out.strip() if out and out.strip() else None


def attached_origin(here: Path) -> tuple[str, str] | None:
    """(OWNER, REPO) of the GitHub repository an attached repository's origin is (every fetch and push
    URL), or None when ./.git is not one that was there before xo, or has no origin. Any other origin
    is refused; its URL is never shown, since it can hold a login."""
    space = Space(here)
    if space.repo.is_file() or not space.attached:
        return None
    urls = [u.strip() for kind in (["--all"], ["--push", "--all"])
            for u in (space.git("remote", "get-url", *kind, "origin", check=False) or "").splitlines() if u.strip()]
    if not urls:
        return None
    matched = [_github_url(u) for u in urls]
    found = {(m.group(1), m.group(2)) for m in matched if m}
    if not all(matched) or len({(o.casefold(), r.casefold()) for o, r in found}) != 1:
        raise XoError(f"{here.resolve()}/.git has an origin that is not one github.com repository xo can read "
                      "(xo never shows a remote's URL, which can hold a login); xo puts a space on GitHub, so point "
                      "origin at https://github.com/OWNER/REPO, then run again")
    owner, repo = sorted(found)[0]
    if repo in (".", "..") or repo.casefold().endswith(".git"):
        raise XoError(f"{here.resolve()}/.git's origin is not a GitHub repository name xo can use")
    return owner, repo


def _push(space: Space, to: str, refs: dict[str, str], config: tuple, hub: bool = False) -> None:
    """git push of refs ({branch: commit}) to `to`, never forced. In an attached repository, which is
    pushed by URL, your remote-tracking refs are as they were afterwards: from git 2.56, a push to a URL
    that is also one of your remotes' moves that remote's refs, as a push to the remote itself does, so
    whatever the push moved under refs/remotes/ is put back."""
    before = _tracking(space) if space.attached else None
    try:
        space.git("push", "--atomic", "--porcelain", "--no-follow-tags", "--no-recurse-submodules", to,
                  *(f"{c}:refs/heads/{b}" for b, c in refs.items()), config=config, hub=hub)
    finally:
        if before is not None:
            _put_back(space, before)


def _tracking(space: Space) -> dict[str, str]:
    """The remote-tracking refs here, {ref: commit}; a symbolic one (origin/HEAD) follows its target, so
    it is left out."""
    found = {}
    for line in (space.git("for-each-ref", "--format=%(refname) %(objectname) %(symref)", "refs/remotes/") or "").splitlines():
        ref, sha, symref = (line.split(" ") + ["", ""])[:3]
        if ref and not symref:
            found[ref] = sha
    return found


def _put_back(space: Space, before: dict[str, str]) -> None:
    """Remote-tracking refs back to `before`, in one transaction, each only if it is still where the push
    left it (a fetch that moved one since stands)."""
    after = _tracking(space)
    moves = ([f"update {ref} {before[ref]} {sha}" for ref, sha in after.items() if ref in before and sha != before[ref]]
             + [f"delete {ref} {sha}" for ref, sha in after.items() if ref not in before]
             + [f"create {ref} {sha}" for ref, sha in before.items() if ref not in after])
    if moves:
        space.git("update-ref", "--stdin", input=("\n".join(moves) + "\n").encode())


def push(here: Path, owner: str, name: str, refs: dict[str, str]) -> None:
    """The index and the space's branches ({branch: commit}, the exact commits build made and checked, not
    whatever the branches point at by now) to github.com/OWNER/NAME with your own git login, never forced,
    and never to a repository GitHub redirects to. No other branch here is pushed. An attached
    repository's origin is left as it is (attached_origin checked it is this repository), or added
    when it has none."""
    space = Space(here)
    url = f"{gitops.GITHUB}/{owner}/{name}"
    to = url if space.attached else "origin"  # by URL: a push to origin would move your origin/* refs too
    if space.git("remote", "get-url", "origin", check=False) is None:
        space.git("remote", "add", "origin", url)
    elif not space.attached:
        space.git("remote", "set-url", "origin", url)
    try:
        _push(space, to, refs, ("-c", "http.followRedirects=false", "-c", "push.gpgSign=false"))
    except XoError as e:
        why = getattr(e, "detail", "") or ""
        if "Updates were rejected" in why or "non-fast-forward" in why:
            raise XoError(f"github.com/{owner}/{name} has commits xo did not make (a README or license made with it, "
                          "or an edit on GitHub) or is private (xo builds only on what anyone can see there), so "
                          "nothing was pushed; make the repository empty and public, or pick another NAME") from None
        raise


def create(here: Path, name: str, owner: str | None, ask: Callable[[str], bool], wait: Callable[[str], bool],
           repo: str | None = None, hub: quirq.Hub | None = None,
           typed: Callable[[str], str | None] | None = None) -> dict:
    """The whole command after the name checks: refuse, show, ask, build, then push. repo: the GitHub
    repository's name, when an attached repository's origin names one (attached_origin). hub: the space
    goes to the hub project AGENT/NAME instead of GitHub (owner and repo are not used then; see
    _hub_check, and typed: what someone at a terminal types when asked, never answered by --yes). A
    folder that is not there yet is made first (an empty space: its index lists no branches until git
    repositories are put in it), and taken away again, still empty, if anything stops before the space
    is built."""
    here = here.absolute()  # git runs in it and in its repositories, each given as a path
    new = not (here.exists() or here.is_symlink())
    if new:
        try:
            os.mkdir(here)  # never through a link put there since: that would make a space of its target
        except OSError as e:
            raise XoError(f"could not make the folder {here}: {e.strerror or e}") from None
    elif not here.is_dir():
        raise XoError(f"{here} is not a folder (a file, or a link to nothing); xo create space makes a folder a space")
    try:
        return _create(here, name, owner, ask, wait, repo, new, hub, typed)
    except BaseException:
        if new:
            try:
                if not here.is_symlink() and here.is_dir() and not any(here.iterdir()):
                    here.rmdir()  # xo has no delete, so it leaves nothing behind
            except OSError:
                pass  # what stopped the run is the error to show, not this
        raise


def _create(here: Path, name: str, owner: str | None, ask: Callable[[str], bool], wait: Callable[[str], bool],
            repo: str | None, new: bool, hub: quirq.Hub | None = None,
            typed: Callable[[str], str | None] | None = None) -> dict:
    refuse_here(here)
    fresh = not (here / ".git").exists() and not here.joinpath(".git").is_symlink()  # this run makes ./.git
    if hub is not None and "--" in name:
        raise XoError(f"{name} has -- in it, and the hub keeps names with -- for copies of GitHub projects "
                      "(OWNER--REPO); pick a name without it")
    from_origin = repo is not None
    repo = repo or name
    gh = f"{owner}/{repo}"
    at = f"the quirq hub's space {hub.agent}/{name} ({hub.space_url(name)})" if hub else f"github.com/{gh}"
    attached = Space(here).attached
    probe = _HereIgnore(here)
    try:
        chosen, skipped = folders(here, probe)
        # An empty space (made with a new folder, then stopped at the GitHub step) can be run again as it is.
        empty = new or (Space(here).ours() and not _old_branches(Space(here)))
        if not chosen and not empty:
            raise XoError(f"{here.resolve()} has no git repositories in it to make branches of; run xo create space "
                          "inside the folder that holds them")
        looked = _hub_check(here, chosen, attached) if hub else {}
        now = hub_heads(here, hub, name) if hub else heads(owner, repo)
        exists = now is not None
        lines = [f"  {b:<30} {shown(size(f)):>10}" for b, f in chosen.items()]
        where = (f"./.git is already a git repository, so xo adds the space to it: one branch per repository, and "
                 f"the index on branch {ATTACHED_INDEX} (your branches, files and staged changes are not touched)"
                 if attached else "each git repository here becomes its own branch of a new ./.git, and main lists them")
        if not chosen:
            where = ("there is no such folder yet, so xo makes it, with a new ./.git whose main lists no branches"
                     if new else "it has no git repositories in it yet, so main lists no branches")
            where += " yet (put git repositories in it and run xo create space again in it to add them)"
        _say(f"xo create space {name} makes {here.resolve()} a space on {at}: {where}. A branch holds "
             "the repository's files as they are now, not its history.\n"
             + "\n".join(lines) + "\n"
             + (f"  Left out by ./.gitignore: {', '.join(skipped)}.\n" if skipped else "")
             + "  Folders that are not git repositories (and hold none), files outside the repositories, hidden "
             f"folders, node_modules and venv, and repositories more than {_DEPTH} folders down are not part of the "
             "space. Each repository's .gitignore (and this folder's) is followed; .env "
             "files, keys, credential stores, repositories inside a repository and .github/workflows are left out, "
             "and a file that looks like it holds a key, token or password stops it before anything is pushed. xo "
             "cannot find every secret (one in an archive, say), so look through these repositories first. "
             "Everything else is pushed to "
             + (f"{at}, {'which is already there' if exists else 'which xo makes'}, not to GitHub. Anyone can read "
                "what is pushed to the hub, so only repositories whose origin is public on GitHub and that hold "
                "nothing GitHub lacks go there without asking (xo create space --github puts the space on "
                "GitHub instead)."
                if hub else
                f"the repository github.com/{gh}, which is already there and public. Anyone can read it, and what "
                "is pushed stays public." if exists else
                f"github.com/{gh}, this folder's origin, which xo cannot see without a login: if it is private, "
                "what is pushed goes there and is as private as it is; if it does not exist yet, xo asks you to make "
                "it, public." if from_origin else
                f"a new public repository github.com/{gh}, which xo asks you to make. Anyone can read it, and what "
                "is pushed stays public.")
             + " Nothing in these folders is changed"
             + (" outside ./.git." if attached else "."))
        if not ask("Make this folder the space?"):
            raise XoError("nothing was made. To make a space of other git repositories, run xo create space inside "
                          "the folder that holds them")
        made = build(here, name, owner, chosen, probe, now or {}, repo=repo, hub=hub)
    finally:
        probe.close()
    _say(f"./.git: {len(made['branches'])} branches and the index ({made['index']}), {made['hash']}")
    if hub is not None:
        space = Space(here)
        unseen = sorted(f"{b} (no origin)" if v == "none" else f"{b} (files GitHub does not have)"
                        for b, (v, github) in looked.items()
                        if v == "none" or _beyond_github(space, made["branches"][b], chosen[b], github))
        if unseen:
            _say("These git repositories hold files xo cannot tell are meant to be public (no origin on GitHub, or "
                 "files that are not in a commit GitHub has, and a branch holds the files as they are now): "
                 f"{', '.join(unseen)}. On the hub anyone can read them.")
            if typed is None or typed(f"Type {name} to send them to the hub anyway") != name:
                if fresh:
                    shutil.rmtree(here / ".git", ignore_errors=True)  # the one this run made: nothing is left
                raise XoError(("nothing was pushed or kept" if fresh else "nothing was pushed (./.git holds the "
                               "space, here only)") + ". Push each of those repositories "
                              "to a public GitHub origin (and commit or ignore what is left), keep the space on GitHub "
                              "with --github, or run xo create space at a terminal and type the space's name "
                              "(--yes does not answer this)")
        if not exists:
            quirq.make(hub, name, f"xo: the space {name}, made by xo create space")
        push_hub(here, hub, name, {made["index"]: made[made["index"]], **made["branches"]})
        _say(f"pushed {made['index']} and {len(made['branches'])} branches to {hub.space_url(name)}")
        made["pushed"] = True
        return made
    if not on_github(owner, repo):
        link = f"https://github.com/new?name={repo}&owner={owner}&visibility=public"
        _say(f"github.com/{gh} does not exist yet (or is private). Make it here, empty (no README, license "
             f"or .gitignore):\n  {link}")
        if not wait("Press Enter once it exists, or n to stop here"):
            _say(f"not pushed; once it exists, run xo create space again in {here.resolve()}")
            made["pushed"] = False
            return made
    push(here, owner, repo, {made["index"]: made[made["index"]], **made["branches"]})
    _say(f"pushed {made['index']} and {len(made['branches'])} branches to https://github.com/{gh}")
    made["pushed"] = True
    return made
