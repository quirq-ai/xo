"""`xo create space [NAME]` with no KIND: a folder becomes a space, a git repository on GitHub with one
branch per git repository under it. With no NAME the folder it runs in is the space, named after the
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

Its remote is github.com/OWNER/NAME (OWNER is your gh login unless --owner says otherwise), or, in an
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

from xo import gitops
from xo.client import XoError

FILE = "xo-space.json"
SCHEMA = "xo-space/1"
MAIN = "main"
ATTACHED_INDEX = "xo-space"  # the index's branch in a git repository that was there before xo (its main is yours)
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

    def git(self, *args: str, tree: Path | None = None, check: bool = True, config: tuple = (),
            input: bytes | None = None, raw: bool = False) -> str | None:
        where = ["--git-dir", str(self.repo), "-c", f"core.hooksPath={os.devnull}"]
        env = None
        if tree is not None:  # the folder a branch is made from: read only, never written
            where += ["--work-tree", str(tree), "-c", "core.bare=false"]
            env = {"GIT_INDEX_FILE": str(self.scratch / "index")}  # never the repository's own index
        return gitops.run(*config, *where, *args, cwd=self.here, check=check, input=input, raw=raw, env=env)

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
    base = _tip(space, f"refs/remotes/origin/{branch}")

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
        space.git("update-ref", f"refs/heads/{branch}", sha, tip or "")
    return sha


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
    -z for mktree, so no line ending is ever changed on the way (Windows turns \\n into \\r\\n in text)."""
    hashed = digest(branches)
    record = {"schema": SCHEMA, "space": name, "github": github, "hash": hashed, "branches": branches}
    rows = "\n".join(f"| [{b}](../../tree/{b}) | {e['files']} | {shown(e['bytes'])} | `{e['commit'][:7]}` |"
                     for b, e in branches.items())
    readme = (f"# {name}\n\nAn xo space: each branch is one git repository's files, as they were when `xo create "
              f"space` last ran in the space's folder.\n\n| Branch | Files | Size | Commit |\n|---|---|---|---|\n{rows}\n\n"
              f"Hash of all of it: `{hashed}` (sha256 over each branch's git tree id; see {FILE}).\n\nTo update it, run "
              "`xo create space` again in the space's folder, the one that holds these repositories (or `sh update` "
              "there, with this branch's `update` copied in).\n")
    script = ("#!/bin/sh\n# Snapshot every git repository here again and push the branches that changed (xo create "
              f"space, run in the space's folder).\nexec xo create space --owner {github.split('/')[0]} \"$@\"\n")

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
    origin's other branches are left as they are."""
    have = space.git("for-each-ref", "--format=%(objectname) %(refname)", "refs/remotes/origin/") or ""
    for line in have.splitlines():
        sha, _, ref = line.partition(" ")
        branch = ref.removeprefix("refs/remotes/origin/")
        if branch in names and remote.get(branch) != sha:
            space.git("update-ref", "-d", ref, sha)
    for branch, sha in remote.items():
        if branch in names and _branch_ok(branch) and \
                space.git("cat-file", "-e", f"{sha}^{{commit}}", check=False) is not None:
            space.git("update-ref", f"refs/remotes/origin/{branch}", sha)


def build(here: Path, name: str, owner: str, chosen: dict[str, Path], probe: _HereIgnore | None = None,
          remote: dict[str, str] | None = None, repo: str | None = None) -> dict:
    """Make or update the space's repository from the repositories ({branch: folder}), all checked before
    the index moves. remote: the branches GitHub has now (none when it has no such public repository);
    repo: the GitHub repository's name (NAME unless an attached repository's origin says otherwise)."""
    space = Space(here)
    try:
        return _build(space, name, f"{owner}/{repo or name}", chosen, probe, remote)
    finally:
        space.close()


def _theirs(space: Space, remote: dict[str, str], known: set[str]) -> set[str]:
    """In an attached repository, the branches that are yours (here or on GitHub), not the space's."""
    local = space.git("for-each-ref", "--format=%(refname:strip=2)", "refs/heads/") or ""
    head = space.git("symbolic-ref", "--quiet", "--short", "HEAD", check=False)  # checked out, maybe no commit yet
    return ({b for b in local.splitlines() if b} | set(remote) | ({head.strip()} if head else set())) - known


def _build(space: Space, name: str, github: str, chosen: dict[str, Path], probe: _HereIgnore | None,
           remote: dict[str, str] | None) -> dict:
    _start(space, name)
    index = space.index
    if space.attached:
        if space.git("rev-parse", "--verify", "--quiet", f"refs/heads/{index}", check=False) is not None and \
                space.git("cat-file", "-e", f"refs/heads/{index}:{FILE}", check=False) is None:
            raise XoError(f"{space.here.resolve()}/.git already has a branch {index} that xo did not make, and xo "
                          "keeps the space's index there; rename that branch, then run again. Nothing was changed")
        known = {index, *_old_branches(space)}
        theirs = _theirs(space, remote or {}, known)
        clash = sorted(b for b in chosen for t in theirs
                       if b.casefold() == t.casefold() or _inside(b, t) or _inside(t, b))
        if clash:
            raise XoError("these git repositories would be branches that clash with your own branches (here or on "
                          f"github.com/{github}): " + ", ".join(clash) + "; xo never changes your branches, so rename "
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
        base = _tip(space, f"refs/remotes/origin/{gone}") if _branch_ok(gone) else None
        if base is None:  # never pushed, or not on GitHub now: it goes nowhere
            space.git("update-ref", "-d", f"refs/heads/{gone}", check=False)
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
        space.git("update-ref", f"refs/heads/{gone}", base)
        _say(f"{gone}: no such git repository here now; its branch is kept as GitHub has it")
        branches[gone] = _entry(space, base)
    branches = dict(sorted(branches.items(), key=lambda kv: kv[0].casefold()))
    tree = _index(space, name, github, branches)
    commit = _commit(space, index, tree, f"xo create space {name}: {digest(branches)}")
    return {"space": name, "github": github, "hash": digest(branches), "index": index, index: commit,
            "branches": {b: e["commit"] for b, e in branches.items()}}


def _old_branches(space: Space) -> dict[str, dict]:
    """The branches the space's index lists, here (or as GitHub had it when last asked, when there is no
    index here yet)."""
    out = None
    for ref in (f"refs/heads/{space.index}", f"refs/remotes/origin/{space.index}"):
        out = space.git("cat-file", "blob", f"{ref}:{FILE}", check=False)
        if out:
            break
    try:
        record = json.loads(out) if out else {}
    except ValueError:
        return {}
    found = record.get("branches") if isinstance(record, dict) else None
    return found if isinstance(found, dict) else {}


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


def push(here: Path, owner: str, name: str, branches: list[str]) -> None:
    """The index and the space's branches to github.com/OWNER/NAME with your own git login, never forced,
    and never to a repository GitHub redirects to. No other branch here is pushed. An attached
    repository's origin is left as it is (attached_origin checked it is this repository), or added
    when it has none."""
    space = Space(here)
    url = f"{gitops.GITHUB}/{owner}/{name}"
    to = "origin"
    if space.git("remote", "get-url", "origin", check=False) is None:
        space.git("remote", "add", "origin", url)
    elif space.attached:
        to = url
    else:
        space.git("remote", "set-url", "origin", url)
    try:
        space.git("push", "--atomic", "--porcelain", "--no-follow-tags", "--no-recurse-submodules", to,
                  *(f"refs/heads/{b}:refs/heads/{b}" for b in [space.index, *branches]),
                  config=("-c", "http.followRedirects=false", "-c", "push.gpgSign=false"))
    except XoError as e:
        why = getattr(e, "detail", "") or ""
        if "Updates were rejected" in why or "non-fast-forward" in why:
            raise XoError(f"github.com/{owner}/{name} has commits xo did not make (a README or license made with it, "
                          "or an edit on GitHub) or is private (xo builds only on what anyone can see there), so "
                          "nothing was pushed; make the repository empty and public, or pick another NAME") from None
        raise


def create(here: Path, name: str, owner: str, ask: Callable[[str], bool], wait: Callable[[str], bool],
           repo: str | None = None) -> dict:
    """The whole command after the name checks: refuse, show, ask, build, then push. repo: the GitHub
    repository's name, when an attached repository's origin names one (attached_origin)."""
    here = here.absolute()  # git runs in it and in its repositories, each given as a path
    refuse_here(here)
    repo = repo or name
    gh = f"{owner}/{repo}"
    attached = Space(here).attached
    probe = _HereIgnore(here)
    try:
        chosen, skipped = folders(here, probe)
        if not chosen:
            raise XoError(f"{here.resolve()} has no git repositories in it to make branches of; run xo create space "
                          "inside the folder that holds them")
        now = heads(owner, repo)
        exists = now is not None
        lines = [f"  {b:<30} {shown(size(f)):>10}" for b, f in chosen.items()]
        where = (f"./.git is already a git repository, so xo adds the space to it: one branch per repository, and "
                 f"the index on branch {ATTACHED_INDEX} (your branches, files and staged changes are not touched)"
                 if attached else f"each git repository here becomes its own branch of a new ./.git, and main lists them")
        _say(f"xo create space {name} makes {here.resolve()} a space on github.com/{gh}: {where}. A branch holds "
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
             + (f"the repository github.com/{gh}, which is already there and public."
                if exists else f"a new public repository github.com/{gh}, which xo asks you to make.")
             + " Anyone can read it, and what is pushed stays public. Nothing in these folders is changed"
             + (" outside ./.git." if attached else "."))
        if not ask("Make this folder the space?"):
            raise XoError("nothing was made. To make a space of other git repositories, run xo create space inside "
                          "the folder that holds them")
        made = build(here, name, owner, chosen, probe, now or {}, repo=repo)
    finally:
        probe.close()
    _say(f"./.git: {len(made['branches'])} branches and the index ({made['index']}), {made['hash']}")
    if not on_github(owner, repo):
        link = f"https://github.com/new?name={repo}&owner={owner}&visibility=public"
        _say(f"github.com/{gh} does not exist yet (or is private). Make it here, empty (no README, license "
             f"or .gitignore):\n  {link}")
        if not wait("Press Enter once it exists, or n to stop here"):
            _say(f"not pushed; once it exists, run xo create space again in {here.resolve()}")
            made["pushed"] = False
            return made
    push(here, owner, repo, list(made["branches"]))
    _say(f"pushed {made['index']} and {len(made['branches'])} branches to https://github.com/{gh}")
    made["pushed"] = True
    return made
