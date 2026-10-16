"""`xo install`, `xo update` and `xo sync`: a space's projects on this machine, kept in step with
the space recorded in your private metadata.

    xo install space:S    ./S/<project> for each project, at its ref; ./S/.xo/state.json remembers
                          the space's rev and refs as installed
    xo update space:S     record each checkout's branch (or commit) in the space, if the space's rev
                          is still the one installed; refused otherwise: run xo sync
    xo sync space:S       take the remote's changes, keep yours, stop on a project both changed,
                          then record the result
    xo install project:P  ./P cloned (or fetched) from GitHub
    xo update project:P   git push of ./P's branch to its upstream, never forced
    xo sync project:P     git pull --rebase, then that push

The metadata is only ever changed by the xo server; this module reads it and sends refs.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote

from xo import gitops, names
from xo.client import Client, XoError

STATE = Path(".xo") / "state.json"


def target(text: str) -> tuple[str, str]:
    kind, _, name = text.partition(":")
    if kind == "project" and names.project_ok(name):
        return kind, name
    if kind == "space" and names.space_ok(name):
        return kind, name
    raise XoError("name it project:NAME or space:NAME (" + (names.SPACE_RULE if kind == "space" else names.PROJECT_RULE)
                  + ")")


def _say(text: str) -> None:
    print(f"xo: {text}", file=sys.stderr)


def _data(answer: Any, field: str) -> Any:
    found = answer.get("data", {}).get(field) if isinstance(answer, dict) and isinstance(answer.get("data"), dict) \
        else None
    if found is None:
        raise XoError(f"the xo server did not answer with {field} (is it older than this command?)")
    return found


def _pairs(entries: Any, where: str) -> list[tuple[str, str]]:
    """A space's [{project: ref}] as checked pairs: every name and ref is checked again here, since
    git and the filesystem are about to see them."""
    if not isinstance(entries, list):
        raise XoError(f"{where}: projects is not a list")
    out = []
    for e in entries:
        if not (isinstance(e, dict) and len(e) == 1):
            raise XoError(f"{where}: a project entry is not {{PROJECT: REF}}")
        (project, ref), = e.items()
        if not names.project_ok(project):
            raise XoError(f"{where}: {project!r} is not a project name xo uses")
        if not names.ref_ok(ref):
            raise XoError(f"{where}: {ref!r} for {project} is not a ref xo uses")
        out.append((project, ref))
    return out


def _space(client: Client, name: str) -> dict:
    spaces = _data(client.request("GET", "/me/spaces").data, "spaces")
    found = [s for s in spaces if isinstance(s, dict) and str(s.get("name", "")).casefold() == name.casefold()]
    if not found:
        raise XoError(f"you have no space named {name} (xo list spaces shows them)")
    return found[0]


def _rev(space: dict) -> int:
    rev = space.get("rev")
    if not isinstance(rev, int) or isinstance(rev, bool):
        raise XoError(f"space {space.get('name')} has no rev xo can use")
    return rev


def _state(root: Path) -> dict:
    path = root / STATE
    try:
        state = json.loads(path.read_text())
    except FileNotFoundError:
        raise XoError(f"./{root} was not installed by xo; run xo install space:{root.name} first") from None
    except (OSError, ValueError) as e:
        raise XoError(f"cannot read {path}: {e}") from None
    if not isinstance(state, dict) or not isinstance(state.get("rev"), int):
        raise XoError(f"{path} is not one xo wrote; run xo install space:{root.name} again")
    return state


def _save(root: Path, name: str, rev: int, pairs: list[tuple[str, str]]) -> None:
    path = root / STATE
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"space": name, "rev": rev, "projects": [{p: r} for p, r in pairs]}, indent=2) + "\n")
    os.replace(tmp, path)


def _place(dest: Path, project: str, ref: str | None) -> bool:
    """Clone or fetch a GitHub project at dest and put it at ref. False for a project with no
    GitHub remote, which xo cannot fetch: it is left as it is."""
    if not names.is_github(project):
        _say(f"{project} has no GitHub remote; left as it is" + ("" if dest.exists() else " (not here)"))
        return False
    if dest.exists():
        if not (dest / ".git").exists():
            raise XoError(f"./{dest} exists and is not a git checkout; move it and try again")
        if gitops.dirty(dest):
            raise XoError(f"./{dest} has changes that are not committed; commit or stash them first")
        gitops.fetch(dest)
    else:
        gitops.clone(project, dest)
    if ref is not None:
        gitops.checkout(dest, ref)
    return True


def install(client: Client, kind: str, name: str) -> Any:
    if kind == "project":
        projects = _data(client.request("GET", "/me/projects").data, "projects")
        if name not in projects:
            raise XoError(f"{name} is not in your projects (xo list projects shows them)")
        if not _place(Path(name), name, None):
            raise XoError(f"{name} has no GitHub remote, so there is nothing to install")
        _say(f"./{name}: up to date with {gitops.GITHUB}/{name}")
        return {"project": name}
    space = _space(client, name)
    pairs = _pairs(space.get("projects"), f"space {name}")
    root = Path(name)
    for project, ref in pairs:
        if _place(root / project, project, ref):
            _say(f"./{root / project}: {ref}")
    rev = _rev(space)
    _save(root, name, rev, pairs)
    return {"space": name, "rev": rev, "projects": [{p: r} for p, r in pairs]}


def _local(root: Path, base: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Each project's ref as checked out under root; the installed ref when it is not here."""
    out = []
    for project, ref in base:
        dest = root / project
        if not (dest / ".git").exists():
            out.append((project, ref))
            continue
        here = gitops.current(dest)
        if gitops.branch(dest) is None:  # detached: still the installed tag or commit, if HEAD is there
            try:
                here = ref if gitops.resolve(dest, ref)[1] == here else here
            except XoError:
                pass
        out.append((project, here))
    return out


def _published(root: Path, pairs: list[tuple[str, str]], base: dict[str, str]) -> None:
    """Refuse to record a ref others could not install: it must be on the project's remote."""
    for project, ref in pairs:
        dest = root / project
        if base.get(project) == ref or not (dest / ".git").exists():
            continue
        gitops.fetch(dest)
        try:
            gitops.resolve(dest, ref)
        except XoError:
            raise XoError(f"./{dest} is at {ref}, which its remote does not have; push it first "
                          "(xo update project:" + project + ")") from None


def _record(client: Client, name: str, rev: int, pairs: list[tuple[str, str]]) -> dict:
    path = f"/me/spaces/{quote(name, safe='')}"
    print(f"xo: PUT {client.scrub(client.url(path))}", file=sys.stderr)
    return _data(client.request("PUT", path, body={"rev": rev, "projects": [{p: r} for p, r in pairs]}).data,
                 "space")


def update(client: Client, kind: str, name: str) -> Any:
    if kind == "project":
        gitops.push(Path(name))
        _say(f"./{name}: pushed")
        return {"project": name}
    root = Path(name)
    state = _state(root)
    base = _pairs(state.get("projects"), str(root / STATE))
    pairs = _local(root, base)
    _published(root, pairs, dict(base))
    space = _record(client, name, state["rev"], pairs)
    _save(root, name, _rev(space), _pairs(space.get("projects"), f"space {name}"))
    return space


def sync(client: Client, kind: str, name: str) -> Any:
    if kind == "project":
        dest = Path(name)
        gitops.pull_rebase(dest)
        gitops.push(dest)
        _say(f"./{name}: rebased on its upstream and pushed")
        return {"project": name}
    root = Path(name)
    state = _state(root)
    base = dict(_pairs(state.get("projects"), str(root / STATE)))
    local = dict(_local(root, list(base.items())))
    space = _space(client, name)
    remote = _pairs(space.get("projects"), f"space {name}")
    merged, clashes = [], []
    for project, theirs in remote:
        was, mine = base.get(project), local.get(project, base.get(project))
        if mine is None or mine == theirs or mine == was:
            merged.append((project, theirs))
        elif theirs == was:
            merged.append((project, mine))
        else:
            clashes.append(f"{project} (yours {mine}, the space's {theirs}, installed {was})")
    if clashes:
        raise XoError("both you and the space changed " + "; ".join(clashes) + f". Nothing was changed. To take "
                      f"the space's refs, run xo install space:{name}; to keep yours, run that, check yours out "
                      f"again and run xo update space:{name}")
    for project, ref in merged:
        if local.get(project) != ref:
            _place(root / project, project, ref)
    rev = _rev(space)
    if merged != remote:
        _published(root, merged, dict(remote))
        rev = _rev(_record(client, name, rev, merged))
    _save(root, name, rev, merged)
    return {"space": name, "rev": rev, "projects": [{p: r} for p, r in merged]}
