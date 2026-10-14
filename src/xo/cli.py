"""xo: the command line for the XO Cloud API (app.xo.builders).

    xo ENDPOINT [ACTION] [--flag VALUE ...]  one command per API endpoint, made from the spec:
                                            POST /claude-code is `xo claude-code`, and a path with
                                            several methods has actions (list, get, create,
                                            update, replace, delete). Its parameters and body
                                            fields are its flags; `xo ENDPOINT --help` lists them.
    xo ops [FILTER]                         list the API's operations: id, method, path, summary.
                                            FILTER keeps those whose id, path or tag contains it.
    xo call OPERATION [--param NAME=VALUE ...] [--data JSON|@FILE|-] [--field KEY=VALUE ...]
                                            run one operation from the spec. --param fills path,
                                            query and header parameters; --data or --field give the
                                            JSON body.
    xo api METHOD PATH [--query NAME=VALUE ...] [--data JSON|@FILE|-] [--field KEY=VALUE ...]
                                            send any request, spec or not.

`call` and `api` change things on XO when the method is not GET: each such request prints one line
on stderr first. --dry-run prints the request and sends nothing; a DELETE also needs --yes.

The API key comes from $XO_API_KEY, or from --api-key - (one line on stdin). --api-key KEY also
works but lands in your shell history and the process list; prefer the variable. xo sends the key
as a bearer token, never prints it, and never follows a redirect. The API base is $XO_BASE_URL or
https://app.xo.builders. The spec comes from --spec FILE, $XO_SPEC, the copy bundled with xo, or
the API itself.

Prints the response as JSON on stdout. Exit 1 on any problem, with one line on stderr saying why.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, NoReturn

from xo import __version__, commands, spec
from xo.client import DEFAULT_TIMEOUT, KEY_ENV, Client, XoError, api_key, base_url, redact

GLOBAL_DEFAULTS = {"api_key": None, "base_url": None, "spec": None, "timeout": DEFAULT_TIMEOUT}
# Global options that take a value, so the word after one is never read as the command.
_VALUED = {"--api-key", "--base-url", "--spec", "--timeout"}
HIDDEN = "<hidden>"
_UNRECOGNIZED = re.compile(r"(unrecognized arguments: )(.*)\Z", re.S)
_ECHOED = re.compile(r"(invalid choice|invalid [\w ]+ value|ignored explicit argument): ('(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"|\S+)")
_JOINED = re.compile(r"(--?[A-Za-z0-9][\w-]*)=\S*")


def _secrets(argv: list[str]) -> list[str]:
    """Every value given to --api-key in argv, plus $XO_API_KEY."""
    found = [os.environ.get(KEY_ENV, "").strip()]
    for i, arg in enumerate(argv):
        if arg == "--api-key" and i + 1 < len(argv):
            found.append(argv[i + 1])
        elif arg.startswith("--api-key="):
            found.append(arg.partition("=")[2])
    # Too short to be a key, and replacing it would garble the message.
    return [s for s in found if len(s) >= 6]


def _option_name(word: str) -> str:
    """--name=value -> --name; -kVALUE -> -k."""
    if word.startswith("--"):
        return word.partition("=")[0]
    return word[:2]


def _typed_values(argv: list[str]) -> list[str]:
    """Everything in argv that is not an option name: values, joined values, positionals."""
    values = []
    for word in argv:
        if word.startswith("--"):
            values.append(word.partition("=")[2])
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
    message = _UNRECOGNIZED.sub(unrecognized, message)
    message = _ECHOED.sub(lambda m: f"{m.group(1)}: {HIDDEN}", message)
    message = _JOINED.sub(lambda m: f"{m.group(1)}={HIDDEN}", message)
    for value in [*_secrets(argv), *_typed_values(argv)]:
        message = re.sub(r"(?<![\w-])" + re.escape(value) + r"(?![\w-])", HIDDEN, message)
        message = message.replace(value, HIDDEN) if len(value) >= 6 else message
    return message


def _json(data: Any) -> str:
    """JSON for a terminal: json escapes C0 itself; escape DEL and C1 too (only strings hold them)."""
    out = json.dumps(data, indent=2, ensure_ascii=False)
    return re.sub(r"[\x7f-\x9f]", lambda m: f"\\u{ord(m.group()):04x}", out)


class _Parser(argparse.ArgumentParser):
    """argparse echoes what was typed; never let that echo a key. No abbreviations, so a spec flag
    such as --s is never taken for --spec."""

    argv: list[str] = []

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
            raise XoError(f"{flag} wants NAME=VALUE, not {item!r}")
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
                raise XoError(f"--data {args.data}: {e.strerror}") from None
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


def _client(args: argparse.Namespace, *, need_key: bool = True) -> Client:
    """`xo ops` reads a local spec without a key; it only needs one to fetch the spec remotely."""
    if args.api_key == "-" and getattr(args, "data", None) == "-":
        raise XoError("--api-key - and --data - both read stdin; give the body with --data @FILE")
    try:
        key: str | None = api_key(args.api_key)
    except XoError:
        if need_key:
            raise
        key = None
    args.resolved_key = key
    return Client(base_url(args.base_url), key, timeout=args.timeout)


def _print(client: Client, data: Any, text: str) -> None:
    if data is not None:
        out = _json(data)
    else:
        out = text
    if out:
        print(client.scrub(out))


def _send(client: Client, args: argparse.Namespace, method: str, path: str, *, query: dict,
          body: Any, headers: dict | None = None) -> int:
    method = method.upper()
    if args.dry_run:
        plan = {"method": method, "url": client.url(path, query), "headers": headers or {}, "body": body}
        if body is not None:
            client.encode(body)  # the same JSON check a real send makes
        print(client.scrub(_json(plan)))
        print("xo: dry run, nothing sent", file=sys.stderr)
        return 0
    if method == "DELETE" and not args.yes:
        raise XoError(f"DELETE {path} needs --yes (or --dry-run to see the request first)")
    if method != "GET":
        print(f"xo: {method} {spec.clean(client.url(path))}", file=sys.stderr)
    resp = client.request(method, path, query=query, body=body, headers=headers)
    _print(client, resp.data, resp.text)
    return 0


def _ops(args: argparse.Namespace) -> int:
    client = _client(args, need_key=False)
    ops = spec.operations(spec.load(client, args.spec))
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
    client = _client(args, need_key=not args.dry_run)
    # A dry run sends nothing, so it never fetches the spec either: that would send the key.
    op = spec.find(spec.load(client, args.spec, remote=not args.dry_run), args.operation)
    path, query, headers = spec.build(op, _pairs(args.param, "--param"), label=f"call {op.op_id}")
    body = _body(args)
    if op.body_required and body is None:
        raise XoError(f"{op.op_id} needs a JSON body: --data JSON|@FILE|- or --field KEY=VALUE")
    if body is not None and not op.has_body:
        raise XoError(f"{op.op_id} takes no body")
    return _send(client, args, op.method, path, query=query, body=body, headers=headers)


def _api(args: argparse.Namespace) -> int:
    client = _client(args, need_key=not args.dry_run)
    return _send(client, args, args.method, args.path, query=_pairs(args.query, "--query"), body=_body(args))


def _endpoint(args: argparse.Namespace) -> int:
    client = _client(args, need_key=not args.dry_run)
    op = args.op
    label = " ".join([args.command, *([args.action] if getattr(args, "action", None) else [])])
    params, fields = commands.values(args)
    path, query, headers = spec.build(op, params, lambda n: commands.flag_for(args, n), label=label)
    body = _body(args)
    if fields:
        if body is None:
            body = {}
        if not isinstance(body, dict):
            raise XoError(f"{label}: body field flags need --data to be a JSON object")
        for wire, (flag, value) in fields.items():
            # A string field keeps the text as typed: --prompt true sends "true", not true.
            body[wire] = value if flag.type == "string" else _value(value)
    if body is None and op.body_required:
        if not any(bf.required for bf in op.body_fields):
            raise XoError(f"{label} needs a body: its field flags, --data or --field")
        body = {}
    if isinstance(body, dict):
        missing = [bf.name for bf in op.body_fields if bf.required and bf.name not in body]
        if missing:
            raise XoError(f"{label} needs " + ", ".join(commands.flag_for(args, n, body=True) for n in missing))
    return _send(client, args, op.method, path, query=query, body=body, headers=headers)


def _add_send(p: argparse.ArgumentParser) -> None:
    p.add_argument("--data", metavar="JSON|@FILE|-", help="the JSON body, inline, from a file, or stdin")
    p.add_argument("--field", action="append", metavar="KEY=VALUE",
                   help="set one top-level body field; VALUE is JSON when it parses. Repeat it")
    p.add_argument("--dry-run", action="store_true", help="print the request (never the key) and send nothing")
    p.add_argument("--yes", action="store_true", help="needed to send a DELETE")


def _common() -> argparse.ArgumentParser:
    """Options accepted before or after the command, so a misplaced --api-key is still parsed."""
    c = _Parser(add_help=False)
    c.add_argument("--api-key", default=argparse.SUPPRESS, metavar="KEY|-",
                   help="the XO API key; - reads it from stdin (default $XO_API_KEY, which is safer)")
    c.add_argument("--base-url", default=argparse.SUPPRESS,
                   help="the API base (default $XO_BASE_URL or https://app.xo.builders)")
    c.add_argument("--spec", default=argparse.SUPPRESS, metavar="FILE",
                   help="an OpenAPI JSON file (default $XO_SPEC, then bundled, then the API)")
    c.add_argument("--timeout", type=float, default=argparse.SUPPRESS, help="seconds per request (default 30)")
    return c


def _redacted(text: str, argv: list[str], key: str | None = None) -> str:
    """xo's own error text with every key it could hold replaced: the one in use, $XO_API_KEY and
    any --api-key value, wherever the user typed it."""
    for secret in (key, *_secrets(argv)):
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
        if command in ("ops", "call", "api"):
            return  # they say for themselves when they need the spec
        reason = str(e) if isinstance(e, XoError) else f"cannot use spec {where}: {type(e).__name__}: {e}"
        print(f"xo: {spec.clean(_redacted(reason, argv))}", file=sys.stderr)
        if command is not None:
            sys.exit(1)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    _Parser.argv = argv
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
    c.set_defaults(run=_call)

    a = sub.add_parser("api", parents=[common],
                       help="send any request; non-GET changes things on XO, DELETE needs --yes")
    a.add_argument("method", type=str.upper, choices=[m.upper() for m in spec.METHODS])
    a.add_argument("path", help="starting with /, relative to the API base")
    a.add_argument("--query", action="append", metavar="NAME=VALUE", help="a query parameter; repeat it")
    _add_send(a)
    a.set_defaults(run=_api)

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
        print(f"xo: {spec.clean(_redacted(str(e), argv, getattr(args, 'resolved_key', None)))}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("xo: interrupted", file=sys.stderr)
        return 130
    except Exception as e:  # noqa: BLE001 - one line, never a traceback that could carry the key
        text = _redacted(str(e), argv, getattr(args, "resolved_key", None))
        print(f"xo: unexpected {type(e).__name__}: {spec.clean(text)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
