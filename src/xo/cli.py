"""xo: the command line for the XO Cloud core API (dev.xo.builders/api/core/v1, through the xo server).

    xo ENDPOINT [ACTION] [--flag VALUE ...]  one command per API endpoint, made from the spec:
                                            GET /spaces/{spaceId} is `xo spaces --space-id ID`; the
                                            space creates (POST /openclaw, ...) are kinds of
                                            `xo create space` instead. Its parameters and body
                                            fields are its flags, nested fields too
                                            (--providers-anthropic-api-key); `xo ENDPOINT --help`
                                            lists them. A path with several methods has actions
                                            (list, get, create, update, replace, delete).
    xo ops [FILTER]                         list the API's operations: id, method, path, summary.
                                            FILTER keeps those whose id, path or tag contains it.
    xo call OPERATION [--param NAME=VALUE ...] [--data JSON|@FILE|-] [--field KEY=VALUE ...]
                                            run one operation from the spec. --param fills path,
                                            query and header parameters; --data or --field give the
                                            JSON body.
    xo api METHOD PATH [--query NAME=VALUE ...] [--data JSON|@FILE|-] [--field KEY=VALUE ...]
                                            send any request, spec or not. Through the xo server
                                            only the creates and GET /spaces/{id} pass; for the
                                            rest, add --url https://dev.xo.builders with
                                            $XO_API_KEY or --api-key - (not the saved key).
    xo serve [--host H] [--port P]          run the xo server: it answers `xo me` and
                                            `xo user-metadata` with the Clerk credentials it holds,
                                            and passes the creates and GET /spaces/{id} on to XO's
                                            core API.
    xo login [xo|me|github] [--url URL]
                                            log in to every service the xo server lists (or the one
                                            named): me (the xo server) and xo take your API key,
                                            asked for without showing it unless a saved key still
                                            works; github runs gh auth login for github.com, unless gh is
                                            already connected.
    xo status                               each service, its address, and whether you are logged in.
    xo projects [add NAME]                  show the projects list in your private metadata, or add
                                            NAME to it. The xo server makes the change and writes only
                                            that list; xo sends just the name.
    xo create project NAME [GITHUB-URL]     add a project and make its folder here. Give a name, a
                                            GitHub URL, or both: a GitHub project is named OWNER/REPO and
                                            cloned, any other name gets `git init` (its repo can be added
                                            later with xo connect project:NAME --repo URL).
    xo create space NAME KIND [flags]       create a space on XO (KIND: claude-code, codex, antigravity,
                                            hermes, openclaw, xo-cowork, nebius-openclaw) named NAME there
                                            too, and record it in your spaces.
    xo connect project:P space:S [--ref R]  set project P in space S at ref R (default main).
    xo connect project:P --repo URL         give project P, made with a name only, its GitHub repo; P
                                            is renamed OWNER/REPO.
    xo connect space:S [--id ID]            a new space entry S in your metadata; --id ties it to an
                                            XO space you already have.
    xo list projects | spaces | shared      show your projects, your spaces, or the invites shared
                                            with you.
    xo share space:S|project:O/R USERNAME   invite that user to a copy of the space (its name and
                                            projects) or the project.
    xo accept ID [--as NAME] [--yes]        show invite ID (from xo list shared), ask, then add a
                                            shared space as a space of your own.
    xo accept ID --space S [--ref R]        the same for a shared project: into your space S, at
                                            ref R (default main).
    xo reject ID                            take invite ID off your list (its sender can share again).
    xo install project:P | space:S          clone or fetch P here; for a space, each of its projects
                                            into ./S/ at its ref.
    xo update project:P | space:S           push P's branch (never forced); for a space, record the
                                            refs checked out under ./S/ (refused if the space moved on).
    xo sync project:P | space:S             pull --rebase and push P; for a space, take its changes,
                                            keep yours, then record the result.
    (the xo key step:)                      ask for your API key without showing it, check it with
                                            XO through the xo server, and save it with the URL.
    xo logout                               delete the saved key, URL and addresses; asks before
                                            logging gh out of github.com.

A request that is not a GET changes things on XO, so it prints one line on stderr first; a create
that stores your API key in the new space says so on a second line. --dry-run prints the request
and sends nothing, with provider keys and bot tokens shown as <hidden> unless --show-secrets is
given. A DELETE also needs --yes.

XO API commands go to one URL: --url, else $XO_URL, else the one xo login saved, else
https://xo.quirq.dev. The core API is under its /api/core/v1.

The API key comes from --api-key - (one line on stdin), $XO_API_KEY, or the key xo login saved,
in that order; the saved key is only ever sent to the URL it was saved with. --api-key KEY also
works but lands in your shell history and the process list. xo sends the key as a bearer token,
never prints it, and never follows a redirect. The spec comes from --spec FILE, $XO_SPEC, or the
copy bundled with xo.

Prints the response as JSON on stdout. Exit 1 on any problem, with one line on stderr saying why.
"""
from __future__ import annotations

import argparse
import difflib
import getpass
import json
import math
import os
import re
import sys
from pathlib import Path
from typing import Any, NoReturn
from urllib.parse import quote

from xo import __version__, commands, github, gitops, names, services, spec, workspace
from xo.client import (CORE_PATH, DEFAULT_TIMEOUT, KEY_ENV, MAYBE_RECEIVED, MIN_SECRET, Client, Login, XoError,
                       api_key, check_key, credentials_path, forget_login, read_key, redact, save_login, saved_login, xo_url)

GLOBAL_DEFAULTS = {"api_key": None, "url": None, "spec": None, "timeout": DEFAULT_TIMEOUT}
# Global options that take a value, so the word after one is never read as the command.
_VALUED = {"--api-key", "--url", "--spec", "--timeout"}
HIDDEN = "<hidden>"
_UNRECOGNIZED = re.compile(r"(unrecognized arguments: )(.*)\Z", re.S)
_ECHOED = re.compile(r"(invalid choice|invalid [\w ]+ value|ignored explicit argument): ('(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"|\S+)")
_JOINED = re.compile(r"(--?[A-Za-z0-9][\w-]*)=\S*")


def _secrets(argv: list[str]) -> list[str]:
    """Every --api-key in argv, plus XO's API key from the environment."""
    found = [os.environ.get(KEY_ENV, "").strip()]
    for i, arg in enumerate(argv):
        if arg == "--api-key" and i + 1 < len(argv):
            found.append(argv[i + 1])
        elif arg.startswith("--api-key="):
            found.append(arg.partition("=")[2])
    # Too short to be a key, and replacing it would garble the message.
    return [s for s in found if len(s) >= 6]


def _long_word(word: str) -> tuple[str, str]:
    """A --word as it may be shown, and the value it may hold.

    --name=value shows --name. A known option with text glued on (--api-keyK, --api-key:K) shows
    --api-key<hidden>. Any other word shows only when it is a near miss of a flag xo made
    (--providers_anthropic_api_key), which no key is; else --<hidden>.
    """
    name, _, value = word.partition("=")
    if name in _Parser.flags:
        return name, value
    prefix = max((f for f in _Parser.flags if name.startswith(f)), key=len, default="")
    if prefix:
        return prefix + HIDDEN, word[len(prefix):]
    if re.fullmatch(r"--[A-Za-z0-9_-]+", name) and difflib.get_close_matches(name, _Parser.flags, n=1, cutoff=0.75):
        return name, value
    return "--" + HIDDEN, word[2:]


def _option_name(word: str) -> str:
    """--name=value -> --name (see _long_word); -kVALUE -> -k."""
    if word.startswith("--"):
        return _long_word(word)[0]
    return word[:2]


def _typed_values(argv: list[str]) -> list[str]:
    """Everything in argv that is not an option name: values, joined values, positionals."""
    values = []
    for word in argv:
        if word.startswith("--"):
            values.append(_long_word(word)[1])
        elif word.startswith("-") and len(word) > 2:
            values.append(word[2:])
        elif not word.startswith("-"):
            values.append(word)
    # Very short words are no key, and hiding them would garble the message.
    return sorted({v for v in values if len(v) >= 3}, key=len, reverse=True)


def safe_error(message: str, argv: list[str]) -> str:
    """An argparse error with nothing the user typed as a value: argparse echoes unrecognised
    arguments, bad choices and ignored values, and a key typed in the wrong place (--api_key K,
    --token=K, -kK, a bare K) would land on stderr. Every value in argv is hidden wherever it shows;
    option names stay, so the message still says what was wrong."""
    def unrecognized(m: re.Match) -> str:
        words = [_option_name(w) if w.startswith("-") else HIDDEN for w in m.group(2).split()]
        return m.group(1) + " ".join(words)
    # "(choose from ...)" lists only names xo made: leave it whole, so a typo still shows the real names.
    message, choose, choices = message.rpartition(" (choose from ")
    if not choose:  # no list: rpartition put the whole message last
        message, choices = choices, ""
    message = _UNRECOGNIZED.sub(unrecognized, message)
    message = _ECHOED.sub(lambda m: f"{m.group(1)}: {HIDDEN}", message)
    message = _JOINED.sub(lambda m: f"{m.group(1)}={HIDDEN}", message)
    for value in [*_secrets(argv), *_typed_values(argv)]:
        message = re.sub(r"(?<![\w-])" + re.escape(value) + r"(?![\w-])", HIDDEN, message)
        message = message.replace(value, HIDDEN) if len(value) >= 6 else message
    return message + choose + choices


def _json(data: Any) -> str:
    """JSON for a terminal: json escapes C0 itself; escape DEL and C1 too (only strings hold them)."""
    out = json.dumps(data, indent=2, ensure_ascii=False)
    return re.sub(r"[\x7f-\x9f]", lambda m: f"\\u{ord(m.group()):04x}", out)


class _Parser(argparse.ArgumentParser):
    """argparse echoes what was typed; never let that echo a key. No abbreviations, so a spec flag
    such as --s is never taken for --spec."""

    argv: list[str] = []
    flags: set[str] = set()  # every --option any parser made, so an error can tell a flag from a value

    def add_argument(self, *args: Any, **kwargs: Any) -> argparse.Action:
        action = super().add_argument(*args, **kwargs)
        _Parser.flags.update(s for s in action.option_strings if s.startswith("--"))
        return action

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("allow_abbrev", False)
        super().__init__(*args, **kwargs)

    def error(self, message: str) -> NoReturn:
        super().error(safe_error(message, _Parser.argv))


def _pairs(items: list[str] | None, flag: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in items or []:
        name, sep, value = item.partition("=")
        if not sep or not name:
            # Not the word itself: it may be a key typed in the wrong place.
            raise XoError(f"{flag} wants NAME=VALUE; one of them has no name or no =")
        out[name] = value
    return out


def _value(text: str) -> Any:
    """A --field value: JSON when it parses (numbers, true, null, objects), else the plain string."""
    try:
        return json.loads(text, parse_constant=lambda c: text)  # NaN and Infinity stay strings
    except json.JSONDecodeError:
        return text


def _body(args: argparse.Namespace) -> Any:
    body: Any = None
    if args.data is not None:
        raw = args.data
        if raw == "-":
            raw = sys.stdin.read()
        elif raw.startswith("@"):
            try:
                raw = Path(raw[1:]).read_text()
            except OSError as e:
                raise XoError(f"--data @FILE: cannot read the file ({e.strerror})") from None
        try:
            body = json.loads(raw)
        except json.JSONDecodeError as e:
            raise XoError(f"--data is not JSON: {e}") from None
    fields = _pairs(args.field, "--field")
    if fields:
        if body is None:
            body = {}
        if not isinstance(body, dict):
            raise XoError("--field needs --data to be a JSON object")
        for name, value in fields.items():
            body[name] = _value(value)
    return body


def _client(args: argparse.Namespace, *, need_key: bool = True, op: spec.Operation | None = None,
            server: bool = False) -> Client:
    """A client for the API, or for an xo server when the operation is one of its routes; a dry run
    needs no key."""
    readers = [*(["--api-key -"] if args.api_key == "-" else []),
               *(["--data -"] if getattr(args, "data", None) == "-" else []),
               *(f"--{f.flag} -" for f, v in getattr(args, "fields", []) if v == "-" and f.secret)]
    if len(readers) > 1:
        raise XoError(f"{' and '.join(readers)} all read stdin; give only one of them as -, "
                      "and the body with --data @FILE")
    url, key = _target(args, need_key=need_key)
    args.resolved_key = key
    base = url if server or (op is not None and op.server) else url + CORE_PATH
    args.client = Client(base, key, timeout=args.timeout)
    return args.client


def _target(args: argparse.Namespace, *, need_key: bool = True) -> tuple[str, str | None]:
    """The one URL a command talks to, and the key it sends there."""
    try:
        saved, broken = saved_login(), None
    except XoError as e:
        saved, broken = None, e
    url = xo_url(args.url, saved.url if saved else None)
    try:
        return url, api_key(args.api_key, url, saved)
    except XoError:
        if not need_key:
            return url, None
        if broken is not None and not args.api_key and not os.environ.get(KEY_ENV, "").strip():
            raise broken from None  # the saved file is why there is no key
        raise


def _print(client: Client, data: Any, text: str) -> None:
    if data is not None:
        out = _json(data)
    else:
        out = text
    if out:
        print(client.scrub(out))


def _secret_values(body: Any, paths: list[tuple[str, ...]]) -> list[str]:
    """The keys and tokens a body holds at the spec's secret paths."""
    found = []
    for p in paths:
        node = body
        for part in p:
            node = node.get(part) if isinstance(node, dict) else None
        if node not in (None, ""):
            found.append(node if isinstance(node, str) else json.dumps(node))
    return found


def _masked(body: Any, paths: list[tuple[str, ...]]) -> Any:
    """A copy of the body with every key or token at a secret path shown as <hidden>."""
    body = json.loads(json.dumps(body))
    for p in paths:
        node = body
        for part in p[:-1]:
            node = node.get(part) if isinstance(node, dict) else None
        if isinstance(node, dict) and node.get(p[-1]) not in (None, ""):
            node[p[-1]] = HIDDEN
    return body


def _send(client: Client, args: argparse.Namespace, method: str, path: str, *, query: dict,
          body: Any, headers: dict | None = None, op: spec.Operation | None = None,
          paths: list[tuple[str, ...]] | None = None, copies: bool = False) -> int:
    method = method.upper()
    paths = op.secret_paths if op else (paths or [])
    args.secret_values = _secret_values(body, paths)
    show = args.dry_run and getattr(args, "show_secrets", False)
    if not show:
        client.hide(args.secret_values)  # never in a response or an error either
    # TODO(suraj): ask XO for a key scoped to one space, or a way to create a space without
    # storing the caller's key in it; until then xo can only say that it happens.
    copies = (copies or (op is not None and op.copies_key)) and method != "GET"
    if args.dry_run:
        shown = body if show or body is None else _masked(body, paths)
        plan = {"method": method, "url": client.url(path, query), "headers": headers or {}, "body": shown}
        if body is not None:
            client.encode(body)  # the same JSON check a real send makes
        print(client.scrub(_json(plan)))
        if copies:
            print(f"xo: {commands.COPIES_KEY}", file=sys.stderr)
        print("xo: dry run, nothing sent", file=sys.stderr)
        return 0
    if method == "DELETE" and not getattr(args, "yes", False):
        raise XoError(f"DELETE {path} needs --yes (or --dry-run to see the request first)")
    if method != "GET":
        print(f"xo: {method} {spec.clean(client.scrub(client.url(path)))}", file=sys.stderr)
    if copies:
        print(f"xo: {commands.COPIES_KEY}", file=sys.stderr)
    resp = client.request(method, path, query=query, body=body, headers=headers)
    data = _profile(resp.data) if op is not None and op.server and op.op_id == "get_me" else resp.data
    _print(client, data, resp.text)
    return 0


_PROFILE_FIRST = ("username", "imageUrl", "firstName", "lastName", "email", "id", "createdAt")
_BUCKETS = (("publicMetadata", "public_data"), ("privateMetadata", "private_data"))


def _profile(data: Any) -> Any:
    """`xo me`: the xo server's answer as one profile under "xo", its metadata last as public_data
    and private_data. Anything that is not a successful answer is shown as it came."""
    if not (isinstance(data, dict) and data.get("success") is True and isinstance(data.get("data"), dict)):
        return data
    user = data["data"]
    names = [k for k in _PROFILE_FIRST if k in user]
    names += [k for k in user if k not in names and k not in dict(_BUCKETS)]
    profile = {k: user[k] for k in names}
    for name, shown in _BUCKETS:
        if name in user:
            profile[shown] = user[name] if user[name] is not None else {}
    return {"xo": profile}


def _ops(args: argparse.Namespace) -> int:
    ops = spec.operations(spec.load(args.spec))
    if args.filter:
        f = args.filter.lower()
        ops = [o for o in ops if f in o.op_id.lower() or f in o.path.lower()
               or any(f in t.lower() for t in o.tags)]
    if args.json:
        print(_json([{"id": o.op_id, "method": o.method, "path": o.path, "summary": o.summary,
                      "tags": o.tags, "params": [p.__dict__ for p in o.params],
                      "body": o.has_body} for o in ops]))
    else:
        for o in ops:
            print(f"{o.op_id}\t{o.method}\t{spec.clean(o.path)}\t{o.summary}")
    if not ops:
        print("xo: " + ("no operations match" if args.filter else "the spec lists no operations"),
              file=sys.stderr)
        return 1
    return 0


def _call(args: argparse.Namespace) -> int:
    op = spec.find(spec.load(args.spec), args.operation)
    client = _client(args, need_key=not args.dry_run, op=op)
    path, query, headers = spec.build(op, _pairs(args.param, "--param"), label=f"call {op.op_id}")
    body = _body(args)
    if op.body_required and body is None:
        raise XoError(f"{op.op_id} needs a JSON body: --data JSON|@FILE|- or --field KEY=VALUE")
    if body is not None and not op.has_body:
        raise XoError(f"{op.op_id} takes no body")
    return _send(client, args, op.method, path, query=query, body=body, headers=headers, op=op)


def _api(args: argparse.Namespace) -> int:
    client = _client(args, need_key=not args.dry_run)
    try:  # the matching operation, if the spec has one, says which body fields are secrets
        op = spec.match(spec.operations(spec.load_local(args.spec) or {}), args.method, args.path)
    except (XoError, ValueError, TypeError, RecursionError):
        op = None
    return _send(client, args, args.method, args.path, query=_pairs(args.query, "--query"), body=_body(args),
                 op=op)


def _read_secret(flag: commands.Flag) -> str:
    """A key or token given as -: read without echo on a terminal, else one line of stdin."""
    typed = getpass.getpass(f"--{flag.flag}: ") if sys.stdin.isatty() else sys.stdin.readline()
    if not typed.strip():
        raise XoError(f"--{flag.flag} -: read nothing from stdin")
    return typed.strip()


def _set(body: dict, flag: commands.Flag, value: Any, label: str) -> None:
    """Put one field flag's value at its path in the body, with the spec's defaults for its siblings
    (so --providers-anthropic-api-key K also sends "enabled": true)."""
    node = body
    for i, part in enumerate(flag.path[:-1]):
        child = node.setdefault(part, {})
        if not isinstance(child, dict):
            raise XoError(f"{label}: --{flag.flag} needs {'.'.join(flag.path[:i + 1])} to be a JSON object")
        node = child
    node[flag.path[-1]] = value
    for name, default in (flag.defaults or {}).items():
        node.setdefault(name, default)


def _unknown_fields(op: spec.Operation, body: dict, args: argparse.Namespace, label: str) -> None:
    """The server drops a top-level field it does not know, and a space cannot be deleted through
    the API, so a typo in --data or --field would create a space without that setting. `xo api`
    still sends anything."""
    if not op.top_fields:
        return
    for name in body:
        if name in op.top_fields:
            continue
        nested = [f"--{f.flag}" for f in args.flags if f.where == "body" and ".".join(f.path) == name]
        near = nested or [f"{n!r}" for n in difflib.get_close_matches(name, op.top_fields, n=1, cutoff=0.6)]
        shown = repr(spec.clean(name)) if re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", name) else "a name"
        raise XoError(f"{label} has no body field {shown}"
                      + (f"; did you mean {near[0]}?" if near else f"; it takes {', '.join(op.top_fields)}")
                      + " (xo api sends any body as given)")


def _objects(args: argparse.Namespace, body: dict) -> None:
    """A field the spec types as an object must be one: null, [] or text that is not JSON could
    wipe or corrupt what the server stores, so refuse it here."""
    for f in args.flags:
        if f.where != "body" or f.type != "object":
            continue
        node: Any = body
        for part in f.path:
            if not isinstance(node, dict) or part not in node:
                break
            node = node[part]
        else:
            if not isinstance(node, dict):
                raise XoError(f"--{f.flag} must be a JSON object, like '{{\"plan\": \"pro\"}}'")


def _endpoint(args: argparse.Namespace) -> int:
    client, op, path, query, headers, body = _request(args)
    return _send(client, args, op.method, path, query=query, body=body, headers=headers, op=op)


def _request(args: argparse.Namespace, *, server: bool = False, preset: dict | None = None) -> tuple:
    """The client, operation, path, query, headers and body an endpoint command's flags make.
    preset: body fields set by the command itself (create space's name), which --data may not change."""
    op = args.op
    label = " ".join([args.command, *(getattr(args, k) for k in ("what", "kind", "action") if getattr(args, k, None))])
    params, args.fields = commands.values(args)
    client = _client(args, need_key=not args.dry_run, op=op, server=server)
    path, query, headers = spec.build(op, params, lambda n: commands.flag_for(args, n), label=label)
    body = _body(args)
    if isinstance(body, dict):
        _unknown_fields(op, body, args, label)
    if args.fields:
        if body is None:
            body = {}
        if not isinstance(body, dict):
            raise XoError(f"{label}: body field flags need --data to be a JSON object")
        for flag, value in args.fields:
            if flag.secret and value == "-":
                value = _read_secret(flag)
            # A string field keeps the text as typed: --prompt true sends "true", not true.
            _set(body, flag, value if flag.type == "string" else _value(value), label)
    if preset:
        if body is None:
            body = {}
        if not isinstance(body, dict):
            raise XoError(f"{label}: --data must be a JSON object")
        for k, v in preset.items():
            if k in body and body[k] != v:
                raise XoError(f"{label}: the body's {k} comes from the command line; leave it out of --data and --field")
            body[k] = v
    if body is None and op.body_required:
        if not op.required_fields:
            raise XoError(f"{label} needs a body: its field flags, --data or --field")
        body = {}
    if isinstance(body, dict):
        missing = [name for name in op.required_fields if name not in body]
        if missing:
            raise XoError(f"{label} needs " + ", ".join(commands.flag_for(args, n, body=True) for n in missing))
        _objects(args, body)
    return client, op, path, query, headers, body


# A space id no space has: XO answers 404 "Space not found" to a working key and 401 to a bad one,
# and nothing is created.
PROBE_SPACE = "00000000-0000-0000-0000-000000000000"


# `xo login NAME` for the XO key: the list's names, and the names before xo-services/2.
KEY_SERVICES = ("xo", "me", "xo-server", "xo-platform")


def _login(args: argparse.Namespace) -> int:
    """Log in to every service in the xo server's list (or the one named), one line each."""
    if args.api_key is not None:
        raise XoError("xo login asks for the key itself; leave out --api-key")
    try:
        saved = saved_login()
    except XoError:
        saved = None  # a file xo cannot use is replaced
    url = xo_url(args.url, saved.url if saved else None)
    listed, notes = services.fetch(url, args.timeout)
    platform = listed["xo"].url
    print(f"xo: services from the xo server at {url}:", file=sys.stderr)
    print(f"xo:   me     {url} (the xo server; your XO key is only sent here)", file=sys.stderr)
    print(f"xo:   xo     {platform} (creates and xo spaces; your key reaches it through the xo server)",
          file=sys.stderr)
    print(f"xo:   github https://{github.HOST} (gh's own login; xo never sees the token)", file=sys.stderr)
    for note in notes:
        print(f"xo: {spec.clean(note)}", file=sys.stderr)
    last = services.saved_addresses()
    before = dict(last)
    results: list[tuple[str, str | None, str]] = []  # (service, error or None, line)

    def report(name: str, error: str | None, line: str) -> None:
        # Printed as each step ends, so the key's result shows before gh's sign-in starts.
        results.append((name, error, line))
        text = f"failed: {error}" if error else line
        print(f"xo: {name}: {spec.clean(_redacted(text, _Parser.argv, getattr(args, 'resolved_key', None)))}",
              file=sys.stderr, flush=True)
    if args.service in (None, *KEY_SERVICES):
        try:
            report("me", None, _login_xo(args, url, saved))
            last["me"], last["xo"] = url, platform
        except XoError as e:
            report("me", str(e), "")
    if args.service in (None, "github"):
        if not github.installed():
            report("github", None, f"skipped: the GitHub CLI is not installed ({github.INSTALL})")
        else:
            try:
                report("github", None, github.GhClient(timeout=args.timeout).login(_ask))
            except XoError as e:
                report("github", str(e), "")
    if last != before:
        try:
            services.save_addresses(last)
        except XoError as e:
            print(f"xo: {spec.clean(str(e))}; the next xo login asks again about any changed address",
                  file=sys.stderr)
    if os.environ.get(KEY_ENV, "").strip() and args.service in (None, *KEY_SERVICES):
        print(f"xo: {KEY_ENV} is set in this terminal and is used instead; run unset {KEY_ENV} to use the saved key",
              file=sys.stderr)
    return 1 if any(error for _, error, _ in results) else 0


def _login_xo(args: argparse.Namespace, url: str, saved: Login | None) -> str:
    """The xo key step: reuse a saved key that still works for this URL, else ask for one, check
    it with XO through the server, and save it with the URL."""
    if saved is not None and saved.url == url:
        args.client = Client(url, saved.key, timeout=args.timeout)
        if _key_works(args.client):
            who = _account(args.client)
            return f"already logged in{f' as {who}' if who else ''}; key saved for {url}"
    print(f"xo: logging in to {url}; the key will only ever be sent there", file=sys.stderr, flush=True)
    key = read_key()
    if not key:
        raise XoError("no key entered; nothing saved")
    args.resolved_key = check_key(key, "the key entered")
    if not key.startswith("ak_"):
        # A Clerk Secret Key or a provider key pasted by mistake goes nowhere, not even to the server.
        raise XoError("that is not an XO API key (those start with ak_); nothing sent or saved, and it is not shown")
    args.client = Client(url, key, timeout=args.timeout)
    _check_with_xo(args.client, url)
    who = _account(args.client)
    path = save_login(Login(url, key))
    where = "only you can read it" if os.name != "nt" else "xo cannot limit who reads it on Windows"
    return (f"logged in as {who}" if who else "logged in; the key works with XO") + (
        f"; key and URL saved to {path} ({where}); xo logout deletes them")


def _key_works(client: Client) -> bool:
    try:
        _check_with_xo(client, client.base, "the saved key")
        return True
    except XoError:
        return False


def _check_with_xo(client: Client, url: str, what: str = "the key") -> None:
    # The check goes through the server to XO and can take a while; say so, so the wait never looks stuck.
    print(f"xo: checking {what} with XO through {url} ...", file=sys.stderr, flush=True)
    try:
        client.request("GET", f"{CORE_PATH}/spaces/{PROBE_SPACE}")
    except XoError as e:
        # XO's own answer for a working key (the quickstart's check). Anything else, the server's
        # bare "Not found" included (one that does not pass the core API on), saves nothing.
        if not str(e).endswith(f"{PROBE_SPACE}: HTTP 404: Space not found"):
            if str(e).endswith(": HTTP 404: Not found"):  # the server's own 404, not XO's
                raise XoError(f"{e}; the xo server at {url} does not pass the core API on (is it up to date?); "
                              "nothing saved") from None
            raise XoError(f"{e}; nothing saved") from None


def _projects(args: argparse.Namespace) -> int:
    """`xo projects`: the list in your private metadata. `xo projects add NAME`: the server adds
    NAME to it; xo sends only the name, never metadata, and the server changes nothing else."""
    client = _client(args, server=True)
    if args.action == "add":
        print(f"xo: POST {spec.clean(client.scrub(client.url('/me/projects')))}", file=sys.stderr)
        data = client.request("POST", "/me/projects", body={"name": args.name}).data
    else:
        data = client.request("GET", "/me/projects").data
    projects = data.get("data", {}).get("projects") if isinstance(data, dict) and isinstance(data.get("data"), dict) \
        else None
    if projects is None:
        raise XoError("the xo server did not answer with a projects list (is it older than xo projects?)")
    print(client.scrub(_json(projects)))
    return 0


def _answer(data: Any, field: str, what: str) -> Any:
    found = data.get("data", {}).get(field) if isinstance(data, dict) and isinstance(data.get("data"), dict) else None
    if found is None:
        raise XoError(f"the xo server did not answer with {what} (is it older than this command?)")
    return found


def _post_server(client: Client, path: str, body: dict) -> Any:
    print(f"xo: POST {spec.clean(client.scrub(client.url(path)))}", file=sys.stderr)
    return client.request("POST", path, body=body).data


def _create_project(args: argparse.Namespace) -> int:
    """`xo create project NAME [URL]`: the server adds the project to your projects, then its folder
    is made here. A GitHub project is named OWNER/REPO and cloned; any other name gets an empty
    repository, and can get its repo later with `xo connect project:NAME --repo URL`. NAME and URL
    may both be given only when they name the same repository."""
    name = _project_target(args.target.strip())
    if args.repo is not None:
        repo = _project_target(args.repo.strip())
        if not names.is_github(repo):
            raise XoError("a project URL must be https://github.com/OWNER/REPO")
        if repo.casefold() != name.casefold():
            raise XoError(f"a project with a repo is named after it ({repo}): run xo create project {repo}, "
                          f"or keep {name} without one and add it later with xo connect project:{name} --repo "
                          f"{gitops.GITHUB}/{repo}")
        name = repo
    dest = Path(name)
    if dest.exists() or dest.is_symlink():
        raise XoError(f"./{name} already exists here; nothing was changed")
    client = _client(args, server=True)
    projects = _answer(_post_server(client, "/me/projects", {"name": name}), "projects", "a projects list")
    try:
        gitops.clone(name, dest) if names.is_github(name) else gitops.init(dest)
    except XoError as e:
        raise XoError(f"{name} is in your projects, but its folder was not made: {e}") from None
    how = f"cloned {gitops.GITHUB}/{name}" if names.is_github(name) else "an empty git repository"
    print(f"xo: ./{name}: {how}", file=sys.stderr)
    print(client.scrub(_json(projects)))
    return 0


def _project_target(text: str) -> str:
    """A project's name from what was typed: a GitHub URL becomes OWNER/REPO."""
    name = names.from_github_url(text) if text.startswith(("https:", "http:", "git@", "ssh:")) else text
    if name is None:
        raise XoError("a project URL must be https://github.com/OWNER/REPO")
    if not names.project_ok(name):
        raise XoError(names.PROJECT_RULE)
    return name


def _create_space(args: argparse.Namespace) -> int:
    """`xo create space NAME KIND [flags]`: the body KIND's create takes, with NAME as the space's
    name on XO too, sent to the xo server, which creates the space through XO with your key and
    records it in your spaces as NAME."""
    if not names.xo_name_ok(args.space_name):
        raise XoError(names.XO_NAME_RULE)
    name = args.space_name.lower()
    if name != args.space_name:
        # XO keeps space names in lowercase; record the name XO will show.
        print(f"xo: XO names spaces in lowercase, so this space is {name}", file=sys.stderr)
    client, op, _, _, _, body = _request(args, server=True, preset={"name": name})
    wrapped = {"kind": op.path.strip("/"), "name": name, "body": body if body is not None else {}}
    return _send(client, args, "POST", "/me/spaces", query={}, body=wrapped,
                 paths=[("body", *p) for p in op.secret_paths], copies=op.copies_key)


def _moved(kind: str) -> str:
    """What replaced `xo KIND`, the create before `xo create space`."""
    return (f"xo {kind} is now xo create space NAME {kind} [flags]; NAME is the space's name on XO and in "
            "your spaces, so leave out --name")


def _connect(args: argparse.Namespace) -> int:
    """`xo connect project:P space:S [--ref R]` or `xo connect space:S [--id ID]`."""
    kind, _, value = args.first.partition(":")
    if kind == "project" and args.repo is not None:
        if args.second is not None or args.ref is not None or args.id is not None:
            raise XoError("give a project its repo with: xo connect project:NAME --repo https://github.com/OWNER/REPO")
        if not names.project_ok(value) or names.is_github(value):
            raise XoError(f"{value} already names a repo" if names.project_ok(value) else names.PROJECT_RULE)
        repo = _project_target(args.repo.strip())
        if not names.is_github(repo):
            raise XoError("a project URL must be https://github.com/OWNER/REPO")
        client = _client(args, server=True)
        data = _post_server(client, "/me/projects/connect", {"name": value, "repo": repo})
        projects = _answer(data, "projects", "a projects list")
        _local_repo(Path(value), repo)
        print(client.scrub(_json(projects)))
        return 0
    if kind == "project":
        where, _, space = (args.second or "").partition(":")
        if where != "space" or not space or args.id is not None:
            raise XoError("connect a project with: xo connect project:NAME space:NAME [--ref REF]")
        if not names.project_ok(value):
            raise XoError(names.PROJECT_RULE)
        if not names.space_ok(space):
            raise XoError(names.SPACE_RULE)
        if args.ref is not None and not names.ref_ok(args.ref):
            raise XoError(names.REF_RULE)
        client = _client(args, server=True)
        body = {"project": value, **({"ref": args.ref} if args.ref is not None else {})}
        data = _post_server(client, f"/me/spaces/{quote(space, safe='')}/projects", body)
    elif kind == "space":
        if args.second is not None or args.ref is not None:
            raise XoError("make a space entry with: xo connect space:NAME [--id XO_SPACE_ID]")
        if not names.space_ok(value):
            raise XoError(names.SPACE_RULE)
        if args.id is not None and not names.SPACE_ID.fullmatch(args.id):
            raise XoError("a space id is letters, digits, _ or -")
        client = _client(args, server=True)
        data = _post_server(client, "/me/spaces/connect",
                            {"name": value, **({"id": args.id} if args.id is not None else {})})
    else:
        raise XoError("connect what? project:NAME space:NAME, or space:NAME [--id ID]")
    print(client.scrub(_json(_answer(data, "space", "a space"))))
    return 0


def _local_repo(dest: Path, repo: str) -> None:
    """After the project is renamed OWNER/REPO: point its folder here, if any, at the repo, and move it
    to ./OWNER/REPO, where xo install and xo sync look for it."""
    if not dest.is_dir():
        print(f"xo: ./{dest} is not here; xo install project:{repo} clones it", file=sys.stderr)
        return
    try:
        print(f"xo: ./{dest}: {gitops.set_origin(dest, repo)}", file=sys.stderr)
    except XoError as e:
        print(f"xo: ./{dest} was left as it is and not moved: {e}", file=sys.stderr)
        return
    target = Path(repo)
    if target.exists() or target.is_symlink():
        print(f"xo: ./{target} already exists, so ./{dest} was not moved there; move it yourself", file=sys.stderr)
        return
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        dest.rename(target)
    except OSError as e:
        # The project is already renamed; only the folder stayed where it was.
        print(f"xo: ./{dest} could not be moved to ./{target} ({e.strerror or type(e).__name__}); move it yourself",
              file=sys.stderr)
        return
    print(f"xo: ./{dest} moved to ./{target}", file=sys.stderr)


def _workspace(args: argparse.Namespace) -> int:
    """`xo install | update | sync project:P | space:S` (see xo/workspace.py)."""
    kind, name = workspace.target(args.target)
    client = _client(args, server=True) if kind == "space" or args.command == "install" else None
    result = getattr(workspace, args.command)(client, kind, name)
    print(_json(result) if client is None else client.scrub(_json(result)))
    return 0


def _share(args: argparse.Namespace) -> int:
    """`xo share space:S|project:OWNER/REPO USERNAME`: an invite in that user's `shared`, which they
    accept or reject. The xo server writes it; the invite holds the space's name and projects."""
    kind, name = workspace.target(args.target)
    if kind == "project" and not names.is_github(name):
        raise XoError("only an OWNER/REPO project can be shared: nobody else could install a local one")
    client = _client(args, server=True)
    data = _post_server(client, "/me/share", {"item": f"{kind}:{name}", "to": args.username})
    print(client.scrub(_json(_answer(data, "shared", "the invite"))))
    return 0


def _sender_note(invite: dict) -> str:
    """What else the accept prompt says about who sent an invite."""
    if invite.get("senderGone"):
        return " (account gone)"
    if invite.get("senderUnchecked"):
        return " (name not checked)"
    return f" (shared as {invite['sharedAs']})" if invite.get("sharedAs") else ""


def _invite(args: argparse.Namespace) -> int:
    """`xo accept ID [--as NAME | --space S [--ref R]] [--yes]` or `xo reject ID`: answer an invite
    in your `shared`, by the id `xo list shared` shows. accept first shows what it will add and asks:
    a shared space becomes a space of your own, a shared project goes into your space S."""
    if not re.fullmatch(r"[0-9a-f]{16}", args.id):
        raise XoError("an invite id is the 16 characters xo list shared shows")
    as_name, into, ref = (getattr(args, k, None) for k in ("as_name", "space", "ref"))
    if as_name is not None and not names.space_ok(as_name):
        raise XoError("--as names the space you accept it as: " + names.SPACE_RULE)
    if into is not None and not names.space_ok(into):
        raise XoError("--space names one of your spaces: " + names.SPACE_RULE)
    if ref is not None and not names.ref_ok(ref):
        raise XoError("--ref: " + names.REF_RULE)
    client = _client(args, server=True)
    body = {"id": args.id, **{k: v for k, v in (("as", as_name), ("space", into), ("ref", ref)) if v is not None}}
    if args.command == "accept":
        invites = _answer(client.request("GET", "/me/shared").data, "shared", "your invites")
        found = [e for e in invites if isinstance(e, dict) and e.get("id") == args.id]
        if not found:
            raise XoError(f"you have no invite {args.id} (xo list shared shows them)")
        invite = found[0]
        kind, _, name = str(invite.get("item", "")).partition(":")
        if kind == "space" and (into is not None or ref is not None):
            raise XoError(f"invite {args.id} is a space: it becomes a space of your own (rename it with --as); "
                          "--space and --ref are for a shared project")
        if kind != "space" and as_name is not None:
            raise XoError(f"invite {args.id} is a project: put it in one of your spaces with --space NAME")
        if kind != "space" and into is None:
            raise XoError(f"invite {args.id} is a project: say which of your spaces it goes in with --space NAME")
        refs = [f"{p}@{r}" for e in invite.get("projects") or [] if isinstance(e, dict) for p, r in e.items()]
        what = (f"space {as_name or name} with " + (", ".join(refs) if refs else "no projects")
                if kind == "space" else f"project {name}@{ref or 'main'} into your space {into}")
        # Names and refs are checked by the server; drop anything unprintable all the same, since
        # someone else wrote them and they go to a terminal.
        line = "".join(c for c in client.scrub(f"from {invite.get('from')}{_sender_note(invite)}: accept {what}?")
                       if c.isprintable())
        if not args.yes and not _ask(line):
            raise XoError("not accepted (answer y at a terminal, or pass --yes)")
    data = _post_server(client, f"/me/shared/{args.command}", body)
    print(client.scrub(_json(_answer(data, "accepted" if args.command == "accept" else "rejected", "an answer"))))
    return 0


def _list(args: argparse.Namespace) -> int:
    """`xo list projects | spaces | shared`: that list in your private metadata."""
    client = _client(args, server=True)
    found = _answer(client.request("GET", f"/me/{args.what}").data, args.what, f"a {args.what} list")
    print(client.scrub(_json(found)))
    return 0


def _account(client: Client) -> str | None:
    """Whose key it is, from the xo server's /me, or None when the server cannot say."""
    try:
        data = client.request("GET", "/me").data
    except XoError:
        return None  # the key works with XO; the server's Clerk may not know it (development keys)
    user = data.get("data") if isinstance(data, dict) else None
    if not isinstance(user, dict):
        return None
    name = user.get("username") or " ".join(str(user.get(k)) for k in ("firstName", "lastName") if user.get(k))
    # Redact before cutting it short, so a cut can never leave part of the key showing.
    shown = client.scrub(" ".join(f"{name or ''} ({user.get('id') or 'no id'})".split()))
    return spec.clean(shown)[:120]


def _logout(args: argparse.Namespace) -> int:
    """Delete the saved key and addresses; log gh out of github.com if you say so."""
    # The XO key goes first, whatever else is wrong.
    path = forget_login()
    print(f"xo: me: deleted {path}" if path else f"xo: me: no saved key at {credentials_path()}",
          file=sys.stderr)
    services.forget_addresses()
    if os.environ.get(KEY_ENV, "").strip():
        print(f"xo: {KEY_ENV} is still set in this terminal; run unset {KEY_ENV} to clear it", file=sys.stderr)
    failed = 0
    if github.installed():
        try:
            print(f"xo: github: {spec.clean(github.GhClient(timeout=args.timeout).logout(_ask))}", file=sys.stderr)
        except XoError as e:
            print(f"xo: github: failed: {spec.clean(str(e))}", file=sys.stderr)
            failed = 1
    return failed


def _status(args: argparse.Namespace) -> int:
    """Each service, its address, and whether you are logged in. Never a key. Only the GitHub CLI
    is asked anything: who it is logged in as on github.com (up to two gh calls, one of them to
    GitHub)."""
    try:
        saved = saved_login()
        problem = None
    except XoError as e:
        saved, problem = None, str(e)
    last = services.saved_addresses()
    url = xo_url(args.url, saved.url if saved else None)
    if os.environ.get(KEY_ENV, "").strip():
        state = f"{KEY_ENV} is set in this terminal and is used"
    elif problem:
        state = f"the saved key cannot be used: {problem}"
    elif saved and saved.url == url:
        state = "key saved by xo login"
    elif saved:
        state = f"not logged in here; the saved key is for {saved.url}"
    else:
        state = "not logged in; run xo login"
    platform = last.get("xo") or services.builtin()["xo"].url
    gh_state = (github.GhClient(timeout=args.timeout).status() if github.installed()
                else "the GitHub CLI is not installed")
    rows = [("me", url, state), ("xo", platform, "uses the same key, through the xo server (me)"),
            ("github", f"https://{github.HOST}", gh_state)]
    for name, where, what in rows:
        print(spec.clean(f"{name:<6} {where:<34} {what}"))
    return 0


def _ask(question: str) -> bool:
    """Yes only when someone at a terminal types y; without a terminal the answer is no."""
    if not (sys.stdin.isatty() and sys.stderr.isatty()):
        return False
    print(f"xo: {question} [y/N] ", end="", file=sys.stderr, flush=True)
    try:
        return sys.stdin.readline().strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        return False


def _seconds(text: str) -> float:
    """A --timeout value: a finite number of seconds above zero."""
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid number of seconds: {text!r}") from None
    if not (math.isfinite(value) and value > 0):
        raise argparse.ArgumentTypeError(f"invalid number of seconds: {text!r}")
    return value


def _serve(args: argparse.Namespace) -> int:
    from xo import server  # imported only here: the CLI never loads server code

    return server.serve(args.host, args.port)


def _add_send(p: argparse.ArgumentParser) -> None:
    p.add_argument("--data", metavar="JSON|@FILE|-", help="the JSON body, inline, from a file, or stdin")
    p.add_argument("--field", action="append", metavar="KEY=VALUE",
                   help="set one top-level body field; VALUE is JSON when it parses. Repeat it")
    p.add_argument("--dry-run", action="store_true",
                   help="print the request and send nothing; the API key never shows, and the provider keys "
                        "and bot tokens the spec marks as secrets show as <hidden>")
    p.add_argument("--show-secrets", action="store_true",
                   help="with --dry-run, show those provider keys and bot tokens as given")


def _common(key: bool = True) -> argparse.ArgumentParser:
    """Options accepted before or after the command, so a misplaced --api-key is still parsed."""
    c = _Parser(add_help=False)
    if key:
        c.add_argument("--api-key", default=argparse.SUPPRESS, metavar="KEY|-",
                       help="the XO API key; - reads it from stdin (default $XO_API_KEY, then the key xo login "
                            "saved, both safer)")
    c.add_argument("--url", default=argparse.SUPPRESS,
                   help="the server XO API commands go to (default $XO_URL, then the one xo login saved, "
                        "then https://xo.quirq.dev)")
    c.add_argument("--spec", default=argparse.SUPPRESS, metavar="FILE",
                   help="an OpenAPI JSON file (default $XO_SPEC, then the one bundled with xo)")
    c.add_argument("--timeout", type=_seconds, default=argparse.SUPPRESS, help="seconds per request (default 30)")
    return c


def _redacted(text: str, argv: list[str], key: str | None = None, others: list[str] | None = None) -> str:
    """xo's own error text with every key it could hold replaced: the one in use, $XO_API_KEY, any
    --api-key value wherever the user typed it, and the body's provider keys and bot tokens."""
    for secret in (key, *_secrets(argv), *(s for s in others or [] if len(s) >= MIN_SECRET)):
        if secret and secret != "-":
            text = redact(text, secret)
    return text


def _command_word(argv: list[str]) -> str | None:
    """The first word that is not an option or a global option's value: the command."""
    skip = False
    for arg in argv:
        if skip:
            skip = False
        elif arg == "--":
            return None
        elif arg in _VALUED:
            skip = True
        elif not arg.startswith("-"):
            return arg
    return None


def _is_create(op: spec.Operation) -> bool:
    """One of XO's space creates, which `xo create space NAME KIND` runs instead of `xo KIND`."""
    return op.method == "POST" and not op.server and op.path.strip("/") in names.SPACE_KINDS


def _endpoints(sub: argparse._SubParsersAction, common: argparse.ArgumentParser, argv: list[str],
               space_kinds: argparse._SubParsersAction | None = None) -> None:
    """One command per endpoint, from a local spec (never the network, so --help stays offline).

    A spec that cannot be used leaves the built-in commands working; any other command stops with
    one line naming the spec and why.
    """
    pre = _Parser(add_help=False)
    pre.add_argument("--spec")
    known, _ = pre.parse_known_args(argv)
    where = known.spec or os.environ.get(spec.SPEC_ENV) or "bundled with xo"
    try:
        local = spec.load_local(known.spec)
        if local is not None:
            ops = spec.operations(local)
            creates = [op for op in ops if _is_create(op)]
            commands.register(sub, [op for op in ops if op not in creates], [common], _add_send, _endpoint)
            for op in creates:  # `xo KIND` itself is gone: main() says what replaced it
                kind = op.path.strip("/")
                if space_kinds is not None:
                    p = space_kinds.add_parser(kind, parents=[common], help=commands._line(op),
                                               description=commands._about(op))
                    commands.add_operation(p, op, _add_send, _create_space, skip={"name"})
    except (XoError, ValueError, TypeError, RecursionError, argparse.ArgumentError) as e:
        command = _command_word(argv)
        if command in ("ops", "call", "api", "serve", "login", "logout", "status"):
            return  # they say for themselves when they need the spec
        reason = str(e) if isinstance(e, XoError) else f"cannot use spec {where}: {type(e).__name__}: {e}"
        print(f"xo: {spec.clean(_redacted(reason, argv))}", file=sys.stderr)
        if command is not None:
            sys.exit(1)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if _command_word(argv) in names.SPACE_KINDS:  # nothing is parsed or sent, so no value is echoed
        print(f"xo: {_moved(_command_word(argv))}", file=sys.stderr)
        return 1
    _Parser.argv = argv
    _Parser.flags = set()
    common = _common()
    parser = _Parser(prog="xo", description=__doc__, parents=[common],
                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=f"xo {__version__}")
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    o = sub.add_parser("ops", parents=[common], help="list the API's operations")
    o.add_argument("filter", nargs="?", help="keep operations whose id, path or tag contains this")
    o.add_argument("--json", action="store_true", help="print them as JSON, with their parameters")
    o.set_defaults(run=_ops)

    c = sub.add_parser("call", parents=[common], help="run one operation from the spec")
    c.add_argument("operation", help="its id, as `xo ops` prints it")
    c.add_argument("--param", action="append", metavar="NAME=VALUE",
                   help="a path, query or header parameter; repeat it")
    _add_send(c)
    c.add_argument("--yes", action="store_true", help="needed to send a DELETE")
    c.set_defaults(run=_call)

    a = sub.add_parser("api", parents=[common],
                       help="send any request (through the xo server, only the creates and GET /spaces/{id} "
                            "pass); non-GET changes things on XO, DELETE needs --yes")
    a.add_argument("method", type=str.upper, choices=[m.upper() for m in spec.METHODS])
    a.add_argument("path", help="starting with /, relative to the API base")
    a.add_argument("--query", action="append", metavar="NAME=VALUE", help="a query parameter; repeat it")
    _add_send(a)
    a.add_argument("--yes", action="store_true", help="needed to send a DELETE")
    a.set_defaults(run=_api)

    keyless = _common(key=False)
    li = sub.add_parser("login", parents=[keyless],
                        help="log in to every service the xo server lists (me and xo with your XO API key), "
                             "and to GitHub with gh's login, or to the one named")
    li.add_argument("service", nargs="?", choices=(*KEY_SERVICES, "github"),
                    help="only this one (xo and me both mean the XO API key; xo-server and xo-platform "
                         "are their old names)")
    li.set_defaults(run=_login)
    lo = sub.add_parser("logout", parents=[keyless],
                        help="delete the key and addresses xo login saved, and log gh out of github.com "
                             "(asked first)")
    lo.set_defaults(run=_logout)
    st = sub.add_parser("status", parents=[keyless],
                        help="each service, its address, and whether you are logged in (never a key)")
    st.set_defaults(run=_status)

    s = sub.add_parser("serve", help="run the xo server that holds the Clerk credentials (see docs/server.md)")
    s.add_argument("--host", default="127.0.0.1", help="address to listen on (default 127.0.0.1)")
    s.add_argument("--port", type=int, default=8787, help="port to listen on (default 8787)")
    s.set_defaults(run=_serve)

    pj = sub.add_parser("projects", parents=[common],
                        help="show the projects list in your private metadata, or add one (the xo server makes "
                             "the change and touches nothing else)")
    pjsub = pj.add_subparsers(dest="action", metavar="ACTION")
    pa = pjsub.add_parser("add", parents=[common], help="add NAME to your projects")
    pa.add_argument("name", metavar="NAME", help="1 to 64 letters, digits, spaces, _ . or -")
    pj.set_defaults(run=_projects, action=None)

    mk = sub.add_parser("create", parents=[common], help="create a project or a space and record it in your "
                                                         "private metadata (the xo server makes the change)")
    mksub = mk.add_subparsers(dest="what", required=True, metavar="WHAT")
    mp = mksub.add_parser("project", parents=[common],
                          help="add a project and make its folder here: a GitHub URL or OWNER/REPO is cloned, "
                               "any other name gets an empty git repository (add its repo later with xo connect)")
    mp.add_argument("target", metavar="NAME|URL", help=names.PROJECT_RULE + ", or https://github.com/OWNER/REPO")
    mp.add_argument("repo", nargs="?", metavar="URL",
                    help="the project's https://github.com/OWNER/REPO, when NAME is also given (they must match)")
    mp.set_defaults(run=_create_project)
    ms = mksub.add_parser("space", parents=[common],
                          help="create a space on XO and record it in your spaces: xo create space NAME KIND")
    ms.add_argument("space_name", metavar="NAME", help=names.XO_NAME_RULE)
    space_kinds = ms.add_subparsers(dest="kind", required=True, metavar="KIND")
    cn = sub.add_parser("connect", parents=[common],
                        help="set a project's ref in one of your spaces, give a project its repo, or make a new "
                             "space entry")
    cn.add_argument("first", metavar="project:NAME|space:NAME")
    cn.add_argument("second", nargs="?", metavar="space:NAME", help="the space a project goes in")
    cn.add_argument("--ref", help="a branch, tag or commit sha for the project (default main)")
    cn.add_argument("--id", help="with space:NAME, an XO space you already have (XO must confirm your key "
                                 "can read it)")
    cn.add_argument("--repo", metavar="URL", help="with project:NAME alone, the project's GitHub repo; the project "
                                                  "is renamed OWNER/REPO")
    cn.set_defaults(run=_connect)
    for verb, about in (("install", "clone or fetch a project here, or each of a space's projects into ./SPACE at its "
                                    "ref"),
                        ("update", "push a project's branch, or record the refs checked out under ./SPACE in the "
                                   "space (refused if the space moved on: run xo sync)"),
                        ("sync", "pull --rebase and push a project, or merge a space's changes with yours and "
                                 "record the result")):
        w = sub.add_parser(verb, parents=[common], help=about)
        w.add_argument("target", metavar="project:NAME|space:NAME")
        w.set_defaults(run=_workspace)
    sh = sub.add_parser("share", parents=[common],
                        help="invite a user to a copy of one of your spaces or GitHub projects (it waits in "
                             "their shared list until they accept or reject it)")
    sh.add_argument("target", metavar="space:NAME|project:OWNER/REPO")
    sh.add_argument("username", metavar="USERNAME", help="their XO username")
    sh.set_defaults(run=_share)
    for verb, about in (("accept", "add an invite from your shared list: a space as a space of your own, a "
                                   "project into one of your spaces (shows what it adds and asks first)"),
                        ("reject", "take an invite off your shared list (its sender can share it again)")):
        iv = sub.add_parser(verb, parents=[common], help=about)
        iv.add_argument("id", metavar="ID", help="the invite's id, as xo list shared shows it")
        if verb == "accept":
            iv.add_argument("--as", dest="as_name", metavar="NAME",
                            help="accept a space under another name (when you already have one called that)")
            iv.add_argument("--space", metavar="NAME", help="the space of yours a shared project goes in")
            iv.add_argument("--ref", metavar="REF", help="the shared project's branch, tag or sha (default main)")
            iv.add_argument("--yes", action="store_true", help="accept without asking")
        iv.set_defaults(run=_invite)
    ls = sub.add_parser("list", parents=[common], help="show your projects, your spaces or the invites shared with you")
    ls.add_argument("what", choices=("projects", "spaces", "shared"))
    ls.set_defaults(run=_list)

    _endpoints(sub, common, argv, space_kinds)

    args = parser.parse_args(argv)
    for name, default in GLOBAL_DEFAULTS.items():
        if not hasattr(args, name):
            setattr(args, name, default)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="backslashreplace")
    try:
        code = args.run(args)
        sys.stdout.flush()
        return code
    except BrokenPipeError:
        # `xo ops | head -1`: the reader left. Point stdout at devnull so the exit flush is quiet.
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        except (OSError, ValueError):
            pass
        return 0
    except XoError as e:
        text = _redacted(str(e), argv, getattr(args, "resolved_key", None), getattr(args, "secret_values", None))
        print(f"xo: {spec.clean(text)}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        client = getattr(args, "client", None)
        sent = client is not None and client.in_flight
        print("xo: interrupted" + (f"; {MAYBE_RECEIVED}" if sent else ""), file=sys.stderr)
        return 130
    except Exception as e:  # noqa: BLE001 - one line, never a traceback that could carry the key
        text = _redacted(str(e), argv, getattr(args, "resolved_key", None), getattr(args, "secret_values", None))
        print(f"xo: unexpected {type(e).__name__}: {spec.clean(text)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
