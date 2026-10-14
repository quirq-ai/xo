"""xo: the command line for the XO Cloud API (app.xo.builders).

    xo ops [FILTER]                         list the API's operations: id, method, path, summary.
                                            FILTER keeps those whose id, path or tag contains it.
    xo call OPERATION [--param NAME=VALUE ...] [--data JSON|@FILE|-] [--field KEY=VALUE ...]
                                            run one operation from the spec. --param fills path and
                                            query parameters; --data or --field give the JSON body.
    xo api METHOD PATH [--query NAME=VALUE ...] [--data JSON|@FILE|-] [--field KEY=VALUE ...]
                                            send any request, spec or not.

The API key comes from $XO_API_KEY (or --api-key, which other users of this machine can see in the
process list; prefer the variable). xo sends it as a bearer token, never prints it, and never
follows a redirect. The API base is $XO_BASE_URL or https://app.xo.builders. The spec comes from
--spec FILE, $XO_SPEC, the copy bundled with xo, or the API itself.

Prints the response as JSON on stdout. Exit 1 on any problem, with one line on stderr saying why.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from xo import __version__, spec
from xo.client import DEFAULT_TIMEOUT, Client, XoError, api_key, base_url


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
        return json.loads(text)
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
    try:
        key: str | None = api_key(args.api_key)
    except XoError:
        if need_key:
            raise
        key = None
    return Client(base_url(args.base_url), key, timeout=args.timeout)


def _print(resp_data: Any, text: str) -> None:
    if resp_data is not None:
        print(json.dumps(resp_data, indent=2, sort_keys=False))
    elif text:
        print(text)


def _ops(args: argparse.Namespace) -> int:
    client = _client(args, need_key=False)
    ops = spec.operations(spec.load(client, args.spec))
    if args.filter:
        f = args.filter.lower()
        ops = [o for o in ops if f in o.op_id.lower() or f in o.path.lower()
               or any(f in t.lower() for t in o.tags)]
    if args.json:
        print(json.dumps([{"id": o.op_id, "method": o.method, "path": o.path, "summary": o.summary,
                           "tags": o.tags, "params": [p.__dict__ for p in o.params],
                           "body": o.has_body} for o in ops], indent=2))
        return 0
    if not ops:
        print("no operations match" if args.filter else "the spec lists no operations", file=sys.stderr)
        return 1
    for o in ops:
        print(f"{o.op_id}\t{o.method}\t{o.path}\t{o.summary}")
    return 0


def _call(args: argparse.Namespace) -> int:
    client = _client(args)
    op = spec.find(spec.load(client, args.spec), args.operation)
    path, query = spec.build(op, _pairs(args.param, "--param"))
    body = _body(args)
    if op.body_required and body is None:
        raise XoError(f"{op.op_id} needs a JSON body: --data JSON|@FILE|- or --field KEY=VALUE")
    if body is not None and not op.has_body:
        raise XoError(f"{op.op_id} takes no body")
    resp = client.request(op.method, path, query=query, body=body)
    _print(resp.data, resp.text)
    return 0


def _api(args: argparse.Namespace) -> int:
    client = _client(args)
    resp = client.request(args.method, args.path, query=_pairs(args.query, "--query"), body=_body(args))
    _print(resp.data, resp.text)
    return 0


def _add_body(p: argparse.ArgumentParser) -> None:
    p.add_argument("--data", metavar="JSON|@FILE|-", help="the JSON body, inline, from a file, or stdin")
    p.add_argument("--field", action="append", metavar="KEY=VALUE",
                   help="set one top-level body field; VALUE is JSON when it parses. Repeat it")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="xo", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=f"xo {__version__}")
    parser.add_argument("--api-key", help="the XO API key (default $XO_API_KEY; prefer the variable)")
    parser.add_argument("--base-url", help="the API base (default $XO_BASE_URL or https://app.xo.builders)")
    parser.add_argument("--spec", metavar="FILE", help="an OpenAPI JSON file (default $XO_SPEC, then bundled)")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="seconds per request")
    sub = parser.add_subparsers(dest="command", required=True)

    o = sub.add_parser("ops", help="list the API's operations")
    o.add_argument("filter", nargs="?", help="keep operations whose id, path or tag contains this")
    o.add_argument("--json", action="store_true", help="print them as JSON, with their parameters")
    o.set_defaults(run=_ops)

    c = sub.add_parser("call", help="run one operation from the spec")
    c.add_argument("operation", help="its id, as `xo ops` prints it")
    c.add_argument("--param", action="append", metavar="NAME=VALUE", help="a path or query parameter; repeat it")
    _add_body(c)
    c.set_defaults(run=_call)

    a = sub.add_parser("api", help="send any request")
    a.add_argument("method", type=str.upper, choices=[m.upper() for m in spec.METHODS])
    a.add_argument("path", help="starting with /, relative to the API base")
    a.add_argument("--query", action="append", metavar="NAME=VALUE", help="a query parameter; repeat it")
    _add_body(a)
    a.set_defaults(run=_api)

    args = parser.parse_args(argv)
    try:
        return args.run(args)
    except XoError as e:
        print(f"xo: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
