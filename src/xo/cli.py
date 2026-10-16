"""xo: the command line for the XO Cloud core API (app.xo.builders/api/core/v1).

    xo ENDPOINT [ACTION] [--flag VALUE ...]  one command per API endpoint, made from the spec:
                                            POST /openclaw is `xo openclaw`, GET /spaces/{spaceId}
                                            is `xo spaces --space-id ID`. Its parameters and body
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
                                            rest, add --url https://app.xo.builders with
                                            $XO_API_KEY or --api-key - (not the saved key).
    xo serve [--host H] [--port P]          run the xo server: it answers `xo me` and
                                            `xo user-metadata` with the Clerk credentials it holds,
                                            and passes the creates and GET /spaces/{id} on to XO's
                                            core API.
    xo login [--url URL]                    ask for your API key without showing it, check it with
                                            XO through the xo server, and save it with the URL.
    xo logout                               delete the saved key and URL.
    xo coder github URL [--name NAME]       create an xo-github workspace for a GitHub repository;
                                            uses your Coder login, then prints workspace and public
                                            app URLs as JSON. See `xo coder github --help`.
    xo coder login | logout                 log Coder in to the deployment in this terminal, or out
                                            of it (only if that is where it is logged in).
    xo coder list | show | start | stop | ssh | ... [ARGS]
                                            run that coder command on the deployment. Before any
                                            Coder command xo asks Coder, without naming a
                                            deployment, where it is logged in, and stops unless
                                            it is this one, so Coder's token goes nowhere else.

A request that is not a GET changes things on XO, so it prints one line on stderr first; a create
that stores your API key in the new space says so on a second line. --dry-run prints the request
and sends nothing, with provider keys and bot tokens shown as <hidden> unless --show-secrets is
given. A DELETE also needs --yes.

XO API commands go to one URL: --url, else $XO_URL, else the one xo login saved, else
https://xo.quirq.dev. The core API is under its /api/core/v1. Coder commands use
--coder-url, else $XO_CODER_URL, else https://dev-coder.xo.builders, and Coder's own login.

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

from xo import __version__, coder, commands, spec
from xo.client import (CORE_PATH, DEFAULT_TIMEOUT, KEY_ENV, MAYBE_RECEIVED, MIN_SECRET, Client,
                       CoderClient, Login, XoError, api_key, check_key, coder_url, credentials_path,
                       forget_login, read_key, redact, save_login, saved_login, xo_url)

GLOBAL_DEFAULTS = {"api_key": None, "url": None, "coder_url": None, "spec": None, "timeout": DEFAULT_TIMEOUT}
# Global options that take a value, so the word after one is never read as the command.
_VALUED = {"--api-key", "--url", "--coder-url", "--spec", "--timeout"}
HIDDEN = "<hidden>"
_UNRECOGNIZED = re.compile(r"(unrecognized arguments: )(.*)\Z", re.S)
_ECHOED = re.compile(r"(invalid choice|invalid [\w ]+ value|ignored explicit argument): ('(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"|\S+)")
_JOINED = re.compile(r"(--?[A-Za-z0-9][\w-]*)=\S*")


def _secrets(argv: list[str]) -> list[str]:
    """Every --api-key in argv, plus XO's API key and Coder's session token from the environment."""
    found = [os.environ.get(KEY_ENV, "").strip(), os.environ.get("CODER_SESSION_TOKEN", "").strip()]
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


def _client(args: argparse.Namespace, *, need_key: bool = True, op: spec.Operation | None = None) -> Client:
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
    base = url if op is not None and op.server else url + CORE_PATH
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
          body: Any, headers: dict | None = None, op: spec.Operation | None = None) -> int:
    method = method.upper()
    paths = op.secret_paths if op else []
    args.secret_values = _secret_values(body, paths)
    show = args.dry_run and getattr(args, "show_secrets", False)
    if not show:
        client.hide(args.secret_values)  # never in a response or an error either
    # TODO(suraj): ask XO for a key scoped to one space, or a way to create a space without
    # storing the caller's key in it; until then xo can only say that it happens.
    copies = op is not None and op.copies_key and method != "GET"
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
    _print(client, resp.data, resp.text)
    return 0


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
    op = args.op
    label = " ".join([args.command, *([args.action] if getattr(args, "action", None) else [])])
    params, args.fields = commands.values(args)
    client = _client(args, need_key=not args.dry_run, op=op)
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
    if body is None and op.body_required:
        if not op.required_fields:
            raise XoError(f"{label} needs a body: its field flags, --data or --field")
        body = {}
    if isinstance(body, dict):
        missing = [name for name in op.required_fields if name not in body]
        if missing:
            raise XoError(f"{label} needs " + ", ".join(commands.flag_for(args, n, body=True) for n in missing))
        _objects(args, body)
    return _send(client, args, op.method, path, query=query, body=body, headers=headers, op=op)


# A space id no space has: XO answers 404 "Space not found" to a working key and 401 to a bad one,
# and nothing is created.
PROBE_SPACE = "00000000-0000-0000-0000-000000000000"


def _login(args: argparse.Namespace) -> int:
    """Ask for the key, check it with XO through the xo server, and save it with the URL it goes to."""
    if args.api_key is not None:
        raise XoError("xo login asks for the key itself; leave out --api-key")
    try:
        saved = saved_login()
    except XoError:
        saved = None  # a file xo cannot use is replaced
    url = xo_url(args.url, saved.url if saved else None)
    print(f"xo: logging in to {url}; the key will only ever be sent there", file=sys.stderr, flush=True)
    key = read_key()
    if not key:
        raise XoError("no key entered; nothing saved")
    args.resolved_key = check_key(key, "the key entered")
    if not key.startswith("ak_"):
        # A Clerk Secret Key or a provider key pasted by mistake goes nowhere, not even to the server.
        raise XoError("that is not an XO API key (those start with ak_); nothing sent or saved, and it is not shown")
    args.client = Client(url, key, timeout=args.timeout)
    try:
        args.client.request("GET", f"{CORE_PATH}/spaces/{PROBE_SPACE}")
    except XoError as e:
        # XO's own answer for a working key (the quickstart's check). Anything else, the server's
        # bare "Not found" included (one that does not pass the core API on), saves nothing.
        if not str(e).endswith(f"{PROBE_SPACE}: HTTP 404: Space not found"):
            if str(e).endswith(": HTTP 404: Not found"):  # the server's own 404, not XO's
                raise XoError(f"{e}; the xo server at {url} does not pass the core API on (is it up to date?); "
                              "nothing saved") from None
            raise XoError(f"{e}; nothing saved") from None
    who = _account(args.client)
    path = save_login(Login(url, key))
    print(f"xo: logged in as {who}" if who else "xo: logged in; the key works with XO", file=sys.stderr)
    where = "only you can read it" if os.name != "nt" else "xo cannot limit who reads it on Windows"
    print(f"xo: key and URL saved to {path} ({where}); xo logout deletes them", file=sys.stderr)
    if os.environ.get(KEY_ENV, "").strip():
        print(f"xo: {KEY_ENV} is set in this terminal and is used instead; run unset {KEY_ENV} to use the saved key",
              file=sys.stderr)
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
    path = forget_login()
    print(f"xo: deleted {path}" if path else f"xo: no saved key at {credentials_path()}", file=sys.stderr)
    if os.environ.get(KEY_ENV, "").strip():
        print(f"xo: {KEY_ENV} is still set in this terminal; run unset {KEY_ENV} to clear it", file=sys.stderr)
    return 0


def _coder_github(args: argparse.Namespace) -> int:
    name, parameters = coder.github_parameters(
        args.repo_url, name=args.name, ref=args.ref, start_command=args.start_command,
        capacity=args.capacity, workspace_size=args.workspace_size,
        home_disk_size=args.home_disk_size, wait_timeout=args.wait_timeout)
    args.client = client = CoderClient(coder_url(args.coder_url), timeout=args.timeout)
    client.hide(_secrets(_Parser.argv))
    if args.dry_run:
        print(client.scrub(_json({"deploymentUrl": client.base, "template": "xo-github", "name": name,
                                  "organization": args.org, "parameters": parameters})))
        print("xo: dry run, nothing sent", file=sys.stderr)
        return 0
    owner = client.owner()
    workspace_url = f"{client.base}/@{quote(owner, safe='')}/{quote(name, safe='')}"
    print(client.scrub(f"xo: creating xo-github workspace {name}; its port 3000 app is public"), file=sys.stderr)
    try:
        app_url = client.create_github(name, parameters, org=args.org, wait_timeout=args.wait_timeout)
    except XoError as e:
        raise XoError(f"{e}; workspace: {workspace_url}") from None
    print(client.scrub(_json({"name": name, "repoUrl": parameters["repo_url"],
                              "workspaceUrl": workspace_url, "appUrl": app_url})))
    return 0


# Coder commands passed straight to the coder CLI, on the deployment xo checked Coder is logged in to.
# No delete: xo has no delete command; delete workspaces in the Coder dashboard.
CODER_PASS = ("list", "show", "start", "stop", "restart", "ssh", "open", "ping", "port-forward",
              "update", "logs")


def _ask(question: str) -> bool:
    """Yes only when someone at a terminal types y; without a terminal the answer is no."""
    if not (sys.stdin.isatty() and sys.stderr.isatty()):
        return False
    print(f"xo: {question} [y/N] ", end="", file=sys.stderr, flush=True)
    try:
        return sys.stdin.readline().strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        return False


def _coder_login(args: argparse.Namespace) -> int:
    client = CoderClient(coder_url(args.coder_url), timeout=args.timeout)
    print(f"xo: coder: {client.login(_ask)}", file=sys.stderr)
    return 0


def _coder_logout(args: argparse.Namespace) -> int:
    client = CoderClient(coder_url(args.coder_url), timeout=args.timeout)
    print(f"xo: coder: {client.logout(_ask)}", file=sys.stderr)
    return 0


def _coder_pass(argv: list[str]) -> int | None:
    """`xo [--coder-url URL] coder [--coder-url URL] VERB ARGS...` for a VERB in CODER_PASS: run
    `coder --url URL VERB ARGS...` in this terminal once Coder is known to be logged in to URL.
    None when argv is something else."""
    flag, timeout, i = None, DEFAULT_TIMEOUT, 0
    seen_coder = False
    while i < len(argv):
        arg = argv[i]
        name, eq, value = arg.partition("=")
        if name in _VALUED and (eq or i + 1 < len(argv)):
            # xo's own options; only --coder-url and --timeout matter to Coder commands.
            if not eq:
                value = argv[i + 1]
            if name == "--coder-url":
                flag = value
            elif name == "--timeout":
                try:
                    timeout = _seconds(value)
                except argparse.ArgumentTypeError:
                    return None
            i += 1 if eq else 2
        elif not seen_coder and arg == "coder":
            seen_coder = True
            i += 1
        elif seen_coder and arg in CODER_PASS:
            client = CoderClient(coder_url(flag), timeout=timeout)
            return client.run([arg, *argv[i + 1:]])
        else:
            return None
    return None


def _coder_pass_misplaced(args: argparse.Namespace) -> int:
    raise XoError(f"xo coder {args.action} takes only xo's --coder-url and --timeout before "
                  f"{args.action} (with a valid number of seconds); put coder's own arguments after it. "
                  "Nothing was sent")


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
    c.add_argument("--coder-url", default=argparse.SUPPRESS,
                   help="a Coder deployment, for `xo coder` (default $XO_CODER_URL, "
                        "then https://dev-coder.xo.builders)")
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


def _endpoints(sub: argparse._SubParsersAction, common: argparse.ArgumentParser, argv: list[str]) -> None:
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
            commands.register(sub, spec.operations(local), [common], _add_send, _endpoint)
    except (XoError, ValueError, TypeError, RecursionError, argparse.ArgumentError) as e:
        command = _command_word(argv)
        if command in ("ops", "call", "api", "serve", "login", "logout", "coder"):
            return  # they say for themselves when they need the spec
        reason = str(e) if isinstance(e, XoError) else f"cannot use spec {where}: {type(e).__name__}: {e}"
        print(f"xo: {spec.clean(_redacted(reason, argv))}", file=sys.stderr)
        if command is not None:
            sys.exit(1)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        passed = _coder_pass(argv)
    except XoError as e:
        print(f"xo: {spec.clean(_redacted(str(e), argv))}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    if passed is not None:
        return passed
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
                        help="ask for your XO API key, check it with XO through the xo server, and save it "
                             "with the URL")
    li.set_defaults(run=_login)
    lo = sub.add_parser("logout", parents=[keyless], help="delete the key and URL xo login saved")
    lo.set_defaults(run=_logout)

    s = sub.add_parser("serve", help="run the xo server that holds the Clerk credentials (see docs/server.md)")
    s.add_argument("--host", default="127.0.0.1", help="address to listen on (default 127.0.0.1)")
    s.add_argument("--port", type=int, default=8787, help="port to listen on (default 8787)")
    s.set_defaults(run=_serve)

    cr = sub.add_parser("coder", parents=[common], help="launch workspaces using your Coder login")
    crsub = cr.add_subparsers(dest="action", required=True, metavar="ACTION")
    gh = crsub.add_parser("github", parents=[common], help="deploy a GitHub repository with xo-github")
    gh.add_argument("repo_url", metavar="URL", help="HTTPS GitHub repository URL (without a branch path)")
    gh.add_argument("--name", help="workspace name (default: repository name, normalized to letters/digits/hyphens)")
    gh.add_argument("--ref", default="", help="branch or tag (default: repository default branch)")
    gh.add_argument("--start-command", default="auto",
                    help="auto detects Node apps; manual only clones; otherwise a shell command on port 3000")
    gh.add_argument("--capacity", choices=("spot", "ondemand"), default="spot",
                    help="spot: shared/free; ondemand: dedicated/paid (default: spot)")
    gh.add_argument("--workspace-size", choices=coder.SIZES, default="xo.small", help="default: xo.small")
    gh.add_argument("--home-disk-size", type=int, default=5, metavar="GB", help="1 to 100 (default: 5); fixed at creation")
    gh.add_argument("--org", help="Coder organization name or UUID (default: Coder's configured organization)")
    gh.add_argument("--wait-timeout", type=float, default=600, metavar="SECONDS",
                    help="maximum wait for Coder to create/start the workspace (default: 600)")
    gh.add_argument("--dry-run", action="store_true", help="print the deployment plan; no Coder CLI or login needed")
    gh.set_defaults(run=_coder_github)
    li = crsub.add_parser("login", parents=[common], help="log Coder in to the deployment, in this terminal")
    li.set_defaults(run=_coder_login)
    lo = crsub.add_parser("logout", parents=[common],
                          help="log Coder out, only if it is logged in to the deployment")
    lo.set_defaults(run=_coder_logout)
    for verb in CODER_PASS:
        crsub.add_parser(verb, help=f"coder {verb} ARGS..., on the deployment, after checking Coder's login"
                         ).set_defaults(run=_coder_pass_misplaced)

    _endpoints(sub, common, argv)

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
