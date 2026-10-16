"""One command per API endpoint, generated from the spec.

    xo <endpoint> [ACTION] [--flag VALUE ...]

The command is the endpoint's path, without a leading /api or /vN and without its {placeholders},
joined with dashes: GET /spaces/{spaceId} is `xo spaces`, GET /me is `xo me`. (XO's space creates,
POST /claude-code and the rest, are kinds of `xo create space` instead; see cli._is_create.)
When several operations share a command (say GET and DELETE on /spaces/{spaceId}), each is an
action named for its method: list, get, create, update, replace, delete. Path, query and header
parameters and the fields of a JSON object body, nested ones too (--providers-anthropic-api-key),
become flags; a body that is not a plain object is given with --data.
"""
from __future__ import annotations

import argparse
import re
from typing import Any, Callable, NamedTuple

from xo.spec import Operation, clean, known_params

# Built-in command names, and names argparse or a reader would take for something else.
RESERVED = {"ops", "call", "api", "serve", "login", "logout", "status", "coder", "projects", "help",
            "version", "create", "connect", "share", "install", "update", "sync", "list"}


class Flag(NamedTuple):
    dest: str
    where: str  # path, query, header or body
    wire: str   # the name the API uses
    flag: str   # the flag, without --
    type: str   # a body field's schema type
    path: tuple[str, ...] = ()  # a body field's path, top level first
    defaults: dict | None = None  # the spec's defaults for the field's siblings
    secret: bool = False  # a key or token: hidden in a dry run and in anything printed


def kebab(text: str) -> str:
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", text)
    text = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    return text or "x"


def command_of(op: Operation) -> str:
    segments = [seg for seg in op.path.strip("/").split("/") if seg and not seg.startswith("{")]
    while segments and (segments[0].lower() == "api" or re.fullmatch(r"v\d+", segments[0].lower())):
        segments = segments[1:]
    name = kebab("-".join(segments)) if segments else "root"
    return f"{name}-api" if name in RESERVED else name


def verb_of(op: Operation) -> str:
    by_id = op.path.rstrip("/").endswith("}")
    return {"GET": "get" if by_id else "list", "POST": "create", "PATCH": "update",
            "PUT": "replace", "DELETE": "delete"}.get(op.method, op.method.lower())


def plan(ops: list[Operation]) -> dict[str, dict[str, Operation]]:
    """command -> action -> operation. A command with one operation has the action ""."""
    by_command: dict[str, list[Operation]] = {}
    for op in ops:
        by_command.setdefault(command_of(op), []).append(op)
    out: dict[str, dict[str, Operation]] = {}
    for command, group in by_command.items():
        if len(group) == 1:
            out[command] = {"": group[0]}
            continue
        actions: dict[str, Operation] = {}
        for op in group:
            name = verb_of(op)
            if name in actions or name in ("help",):
                name = kebab(op.op_id)
            while name in actions:
                name += "-" + op.method.lower()
            actions[name] = op
        out[command] = actions
    return out


def register(sub: argparse._SubParsersAction, ops: list[Operation], parents: list[argparse.ArgumentParser],
             add_send: Callable[[argparse.ArgumentParser], None], run: Callable[[argparse.Namespace], int]) -> None:
    for command, actions in sorted(plan(ops).items()):
        if list(actions) == [""]:
            op = actions[""]
            _operation(sub.add_parser(command, parents=parents, help=_line(op), description=_about(op)),
                       op, add_send, run)
            continue
        # parents: the global options parse here too, so `xo CMD --api-key K ACTION` never takes the
        # key for the ACTION (and never echoes it in an "invalid choice" error).
        g = sub.add_parser(command, parents=parents, help=", ".join(sorted(actions)))
        gsub = g.add_subparsers(dest="action", required=True, metavar="ACTION")
        for name, op in sorted(actions.items()):
            _operation(gsub.add_parser(name, parents=parents, help=_line(op), description=_about(op)),
                       op, add_send, run)


def _esc(text: str) -> str:
    """argparse formats help with %; text from a spec must not be read as a format."""
    return text.replace("%", "%%")


def _line(op: Operation) -> str:
    return _esc(f"{op.method} {clean(op.path)}  {op.summary}".rstrip())


COPIES_KEY = "the new space gets a copy of this API key (as xo_api_key)"


def _about(op: Operation) -> str:
    # argparse %-formats a description only when it holds "%(prog)", so escape only then.
    text = f"{op.summary}\n\n{op.method} {clean(op.path)}  (operation {op.op_id})".strip()
    if op.copies_key:
        text += f"\n\nNote: {COPIES_KEY}. Anything that can read the space's environment can use it."
    return _esc(text) if "%(prog)" in text else text


def add_operation(p: argparse.ArgumentParser, op: Operation, add_send: Callable[[argparse.ArgumentParser], None],
                  run: Callable[[argparse.Namespace], int], skip: set[str] = frozenset()) -> None:
    """One operation's flags on a parser of the caller's own, such as `xo create space NAME KIND`;
    skip names top-level body fields the command sets itself, which get no flag."""
    _operation(p, op, add_send, run, skip)


def _operation(p: argparse.ArgumentParser, op: Operation, add_send: Callable[[argparse.ArgumentParser], None],
               run: Callable[[argparse.Namespace], int], skip: set[str] = frozenset()) -> None:
    flags: list[Flag] = []
    taken = {"data", "field", "dry-run", "show-secrets", "yes", "api-key", "url", "coder-url", "spec", "timeout",
             "help"}
    for prm in known_params(op).values():
        flag = _unique(kebab(prm.name), taken)
        dest = f"p_{len(flags)}"
        need = " (required)" if prm.required else ""
        p.add_argument(f"--{flag}", dest=dest, metavar="VALUE",
                       help=_esc(f"{prm.where} parameter {clean(prm.name)}{need}. {prm.description}".strip()))
        flags.append(Flag(dest, prm.where, prm.name, flag, ""))
    for bf in op.body_fields:
        if len(bf.path) == 1 and bf.path[0] in skip:
            continue
        flag = _unique(kebab("-".join(bf.path)), taken)
        dest = f"p_{len(flags)}"
        need = " (required)" if bf.required else ""
        how = "text" if bf.type == "string" else "JSON when it parses"
        how += "; - reads it from stdin" if bf.secret else ""
        p.add_argument(f"--{flag}", dest=dest, metavar="VALUE",
                       help=_esc(f"body field {clean(bf.name)}{need}; {how}. {bf.description}".strip()))
        flags.append(Flag(dest, "body", bf.name, flag, bf.type, bf.path, bf.defaults, bf.secret))
    if op.has_body:
        add_send(p)
    else:
        p.add_argument("--dry-run", action="store_true", help="print the request and send nothing")
        p.set_defaults(data=None, field=None, show_secrets=False)
    if op.method == "DELETE":
        p.add_argument("--yes", action="store_true", help="needed to send a DELETE")
    p.set_defaults(run=run, op=op, flags=flags)


def _unique(flag: str, taken: set[str]) -> str:
    name = flag
    while name in taken:
        name += "-param"
    taken.add(name)
    return name


def values(args: argparse.Namespace) -> tuple[dict[str, str], list[tuple[Flag, str]]]:
    """(params by wire name, body fields with their Flag) the user gave. Body fields stay a list:
    a top-level "x.y" and a nested x.y share a dotted name but not a path."""
    params: dict[str, str] = {}
    fields: list[tuple[Flag, str]] = []
    for f in args.flags:
        value: Any = getattr(args, f.dest, None)
        if value is None:
            continue
        if f.where == "body":
            fields.append((f, value))
        else:
            params[f.wire] = value
    return params, fields


def flag_for(args: argparse.Namespace, wire: str, *, body: bool = False) -> str:
    """The flag the user types for a parameter (or, with body=True, a body field), as its help shows it."""
    for f in args.flags:
        if f.wire == wire and (f.where == "body") == body:
            return f"--{f.flag}"
    # A body object set through its fields' flags: name a few of them, its keys and tokens first,
    # since an object with only "enabled" set is refused by the server.
    within = [f for f in args.flags if body and f.where == "body" and f.path[:1] == (wire,)]
    inner = [f"--{f.flag}" for f in ([f for f in within if f.secret] or within)]
    if inner:
        return "one of " + ", ".join(inner[:3]) + (", ..." if len(inner) > 3 else "")
    return f"--{kebab(wire)}"
