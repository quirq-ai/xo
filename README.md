# xo

`xo` is the command line for the [XO Cloud](https://app.xo.builders) core API. Every endpoint is a
command, and xo sends your XO API key on each request without ever printing it.

It follows the shape of quirq's own tools (`qq fetch`, `qqsync`): one Python package, one
argparse command, standard library only, Apache-2.0. Unlike them the command is plain `xo`.

## Install

For a step-by-step setup on your own Mac or Linux machine, see [docs/quickstart.md](docs/quickstart.md).

```sh
pip install "quirq-xo @ git+https://github.com/quirq-ai/xo"
```

Python 3.11 or newer. The package is `quirq-xo`, not `xo`: PyPI's `xo` is an unrelated game, and
`pip install -U xo` would replace this tool with it. The command it installs is `xo`.

## Commands

```sh
export XO_API_KEY=ak_...        # an XO API key, from the XO team; never printed

xo --help                       # every command
xo openclaw --help              # the flags one command takes

xo codex --name my-codex --volume-size 20
xo claude-code --name my-cc --volume-size 20 --providers-anthropic-api-key ""
xo openclaw --name my-claw --volume-size 20 \
  --providers-anthropic-api-key "$ANTHROPIC_API_KEY" \
  --notification-channels-telegram-bot-token "$TELEGRAM_BOT_TOKEN"
xo spaces --space-id 6f1c2c1e-...   # connection URLs for a space you own
```

| Command | Request | Body |
|---|---|---|
| `xo claude-code` | `POST /claude-code` | name, volume size; Anthropic provider |
| `xo codex` | `POST /codex` | name, volume size |
| `xo antigravity` | `POST /antigravity` | name, volume size |
| `xo hermes` | `POST /hermes` | name, volume size; providers; Telegram or Slack |
| `xo openclaw` | `POST /openclaw` | name, volume size; at least one provider and one channel |
| `xo xo-cowork` | `POST /xo-cowork` | name, volume size; providers; channels |
| `xo nebius-openclaw` | `POST /nebius-openclaw` | name, Token Factory key, Telegram token (feature flagged) |
| `xo spaces` | `GET /spaces/{spaceId}` | none |
| `xo me` | `GET /me` on your xo server | none |
| `xo user-metadata get` / `update` | `GET` / `PATCH /user-metadata/{userId}` on your xo server | publicMetadata, privateMetadata as JSON objects (admin keys only); try `--dry-run` first: a wrong `--user-id` edits another user |

Each create prints the new space, `data.id` included. Keep that id: the API has no list, delete,
start or stop endpoint, and `xo spaces --space-id ID` is the only way back to a space.

The API is `https://app.xo.builders/api/core/v1`. Its spec is bundled with xo as
`src/xo/openapi.json`, written from the xo-swarm source (`app/api/[[...slugs]]/route.ts` and
`types/*-schema.ts` at 6dddd07aa) because the interactive docs sit behind a browser sign-in. To use
another spec, pass `--spec FILE` or set `XO_SPEC=FILE`.

Things to know:
- A name is 1 to 32 letters, digits or hyphens, starting with a letter or digit. The server
  lowercases it and returns the stored name in `data.name`. A name you already use fails with 500.
- `--volume-size` is in GB, 20 to 100.
- A provider or channel flag sends that object with `"enabled": true`, so
  `--providers-openai-api-key K` sends `{"openai": {"enabled": true, "apiKey": "K"}}`.
- The API checks the body before the key, so a bad body answers 422 with every problem listed,
  even with no key.
- Every create except `nebius-openclaw` stores the XO API key you call with inside the new space,
  as its `xo_api_key`. That key can create spaces and read the connection URLs, session tokens
  included, of every space you own, so anything that can read the new space's environment (an
  agent steered by a message, say) can act as you. xo prints a line saying so before each such
  create, and cannot turn it off. If XO will issue you a second key, use that one for creates.
- `xo spaces` prints `spaceApiUrl` and `openClawRestartUrl`, which carry a session token that
  expires within the hour. Treat them as secrets.
- Provider keys and bot tokens given as flags land in your shell history, and even from a variable
  (`"$ANTHROPIC_API_KEY"`) they show in the process list for as long as the request runs. Give the
  body with `--data @body.json` instead, or one key or token as `-` to type it (or pipe one line).
- A body field the command does not know is refused, since the server would drop it and create
  the space without that setting; `xo api` sends any body as given.
- If a create times out or is interrupted, the server may still have made the space: check before
  you retry.

## Server mode

`xo me` and `xo user-metadata` read your Clerk account, which needs Clerk's Secret Key. That key
never goes on a user's machine: the same package runs as a server, `xo serve`, on one deployment
that holds it, and the commands go there (`https://xo.quirq.dev`, or `XO_SERVER_URL`). See
[docs/server.md](docs/server.md) and, to deploy it, [docs/deploy-vercel.md](docs/deploy-vercel.md).

## How commands are named

The command is the path without a leading `/api` or `/vN` and without its `{placeholders}`, joined
with dashes. When several methods share a path, each is an action: `list` and `get` (GET on the
collection or one item), `create` (POST), `update` (PATCH), `replace` (PUT), `delete` (DELETE).
Path, query and header parameters and the fields of a JSON body are the flags; a field nested in an
object is named by its path (`providers.anthropic.apiKey` is `--providers-anthropic-api-key`). A
body field the spec types as a string keeps the text you type; other values are JSON when they
parse. Required ones are checked before anything is sent. A spec that cannot be read leaves `ops`,
`call` and `api` working and makes any other command stop with one line saying why.

## Lower level

```sh
xo ops                          # every operation: id, method, path, summary
xo ops openclaw                 # only those whose id, path or tag mentions "openclaw"
xo ops spaces --json            # the same with their parameters

xo call create_codex --data '{"name": "my-codex", "volumeSize": 20}'
xo call create_codex --field name=my-codex --field volumeSize=20   # the body field by field
xo call create_openclaw --data @body.json                           # or from a file, or - for stdin
xo call get_space --param spaceId=ID
xo call create_codex --field name=x --field volumeSize=20 --dry-run # print the request, send nothing

xo api GET /spaces/ID                                               # any request, spec or not
```

`--param` fills path, query and header parameters, never `Authorization`, `Host`, `Cookie`,
`Proxy-*` or the other headers xo and HTTP own.
`--field` values are JSON when they parse (`false`, `5`, `["a"]`) and plain strings otherwise.
A request that is not a GET changes things on XO, so it prints one line on stderr before it goes.
`--dry-run` needs no key and sends nothing. It prints the request with the provider keys and bot
tokens the spec marks as secrets shown as `<hidden>`; add `--show-secrets` to see them. Those keys
and tokens are redacted from responses and errors too, like the API key.
Responses print as JSON on stdout. Any problem exits 1 with one line on stderr.

| Setting | Variable | Flag | Default |
|---|---|---|---|
| API key | `XO_API_KEY` | `--api-key -` (asks without echo on a terminal, else reads one line from stdin) | none; required for every call but a dry run |
| API base | `XO_BASE_URL` | `--base-url` | `https://app.xo.builders/api/core/v1` |
| xo server | `XO_SERVER_URL` | `--server-url` | `https://xo.quirq.dev`; used only by `xo me` and `xo user-metadata` |
| Spec | `XO_SPEC` | `--spec` | the copy bundled with xo |

`--api-key KEY` also works, but the key then lands in your shell history and in the process list
other users of the machine can read. Prefer the variable, or `--api-key -`. The options go before
or after the command.

## Safety

- The key goes only in the `Authorization: Bearer` header. xo reads the spec only from a file
  (the bundled one, `--spec` or `XO_SPEC`); it never fetches one from the API with your key. It is redacted from any response or
  error that echoes it, and tests check it never reaches stdout or stderr, including on argument
  errors and unexpected failures. An argument error never repeats a value you typed, so a
  mistyped key flag (`--api_key KEY`, `--token=KEY`, `--api-keyKEY`) stays off the screen. A key
  with anything but letters, digits, `_`, `.` and `-` is refused without being shown, so no URL or
  JSON escaping can hide the key from redaction.
- xo never follows a redirect, so a redirect cannot carry the key to another host. A redirect to
  XO's sign-in page is reported as "this route does not accept an API key".
- Plain `http://` is refused except to this machine.

## Develop

```sh
python -m pip install -e ".[test]"
python -m pytest -q
```

Tests run against a fake API on 127.0.0.1 (`tests/conftest.py`) and a synthetic spec
(`tests/fixtures/openapi.json`); they never reach XO.

## License

Apache-2.0.
