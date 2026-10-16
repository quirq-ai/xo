# xo

`xo` is the command line for the [XO Cloud](https://dev.xo.builders) core API. Every endpoint is a
command, and xo sends your XO API key on each request without ever printing it.

It follows the shape of quirq's own tools (`qq fetch`, `qqsync`): one Python package, one
argparse command, standard library only, Apache-2.0. Unlike them the command is plain `xo`.

## Install

For a step-by-step setup on your own Mac or Linux machine, see [docs/quickstart.md](docs/quickstart.md); for every command and how to test them, the [user guide](docs/guide.md).

```sh
pip install "quirq-xo @ git+https://github.com/quirq-ai/xo"
```

Python 3.11 or newer. The package is `quirq-xo`, not `xo`: PyPI's `xo` is an unrelated game, and
`pip install -U xo` would replace this tool with it. The command it installs is `xo`.

## Commands

```sh
xo login                        # every service the xo server lists: your XO API key (from the
                                # XO team) for me and xo, and Coder's own login
                                # (or: export XO_API_KEY=ak_..., which wins over the saved key)
xo status                       # each service, its address, and whether you are logged in

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
| `xo login [xo\|coder]` / `logout` | every listed service (or the one named): checks the XO key with XO (a space that doesn't exist) and saves it with the address, and logs Coder in / deletes the key and logs Coder out of xo's deployment | none |
| `xo status` | each service's address and whether you are logged in; never a key | none |
| `xo me` | `GET /me` on your xo server; prints `{"xo": {username, imageUrl, ..., "public_data": {...}, "private_data": {...}}}` | none |
| `xo projects` / `xo projects add NAME` | `GET` / `POST /me/projects` on your xo server: the `projects` list in your own private metadata; the server makes the change and writes only that list | the name only |
| `xo create project NAME\|URL` | `POST /me/projects`, then clones `https://github.com/OWNER/REPO` here (or `git init ./NAME` for a plain name) with your own git | the name only |
| `xo create space KIND NAME` | `POST /me/spaces` on your xo server: it creates the space through XO as `xo KIND` does, then records it in your `spaces` as NAME | the flags of `xo KIND` |
| `xo connect project:P space:S` / `xo connect space:ID --name S` | `POST /me/spaces/S/projects` / `POST /me/spaces/connect`: a project's ref in a space, or a space you already have | `--ref` (default main) / the space id |
| `xo list projects\|spaces` | `GET /me/projects` / `GET /me/spaces` | none |
| `xo user-metadata get` / `update` | `GET` / `PATCH /user-metadata/{userId}` on your xo server | publicMetadata, privateMetadata as JSON objects (admin keys only); try `--dry-run` first: a wrong `--user-id` edits another user |
| `xo coder github URL` | Coder CLI, using your Coder login | GitHub repository and `xo-github` template parameters; see below |

Each XO core API create prints the new space, `data.id` included. Keep that id: the API has no
list, delete, start or stop endpoint, and `xo spaces --space-id ID` is the only way back to a space.
`xo coder github` creates a Coder workspace separately; manage its lifecycle with Coder.

The API is XO's platform, `https://dev.xo.builders/api/core/v1` (the `xo` service); xo
reaches it through the xo server (see Server mode and Services). Its spec is bundled with xo as
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

## Services

One `xo login` covers three services, plus GitHub through the GitHub CLI (`gh`). The xo server lists the three at `/config` (no key needed); xo
ships the same list (`src/xo/services.json`) and uses its own copy when the server cannot give a
good one. The names follow the commands: `xo` for the creates, `coder` inside it because `xo coder`
is a subcommand, and `me` for the xo server that answers `xo me`:

```json
{
  "schema": "xo-services/2",
  "xo": { "url": "https://dev.xo.builders", "about": "...",
          "coder": { "url": "https://dev-coder.xo.builders", "about": "..." } },
  "me": { "url": "https://xo.quirq.dev", "about": "..." }
}
```

| Service | Address | How you log in | Commands |
|---|---|---|---|
| `me` | the xo server: `--url`, `XO_URL`, the one `xo login` saved, else `https://xo.quirq.dev` (the `me` address in xo's own list) | your XO API key | `xo me`, `xo user-metadata` |
| `xo` | `https://dev.xo.builders`, reached through the xo server | the same XO API key | `xo claude-code`, `xo codex`, `xo openclaw`, ... and `xo spaces` |
| `coder` | `https://dev-coder.xo.builders` | Coder's own login, in your terminal | `xo coder github`, `xo coder list`, `ssh`, ... |
| `github` | `https://github.com` (not in the list; always github.com) | the GitHub CLI's own login (`gh auth login`), in your browser | `gh`, and git over https if you let gh set it up |

Every XO API command, creates included, goes to the xo server you logged in to: xo sends your key
to that one address, and the server passes it on to the `xo` address with each create. xo does not
use a served list's `me` address; the key's address is always `--url`, `XO_URL`, the one `xo login`
saved, else `https://xo.quirq.dev`.

**Changed: XO's platform is now `https://dev.xo.builders`, not `https://app.xo.builders`.** Every
create carries your XO API key, and it now goes to dev.xo.builders. The xo server takes the new
address on its next deploy unless `XO_CORE_API_URL` is set in its environment (in Vercel), which
overrides it; set `XO_CORE_API_URL=https://app.xo.builders/api/core/v1` to keep the old one.

The list holds addresses only, never keys. Keys stay on your machine: the XO key in
`~/.config/xo/credentials`, Coder's token wherever Coder keeps it. xo checks a served list: no key is
sent for it, no redirect is followed, every address must be plain https, and unknown services are
skipped with a line. If the list names a Coder address other than the one you last logged in to (or,
on a first login, the one shipped with xo), `xo login` says so and asks once; without a terminal the
answer is no. `xo login xo` (or `me`) or `xo login coder` does one service; `xo-server` and
`xo-platform`, the names before `xo-services/2`, still work. `xo logout` deletes the key and
logs Coder out only if it is logged in to xo's deployment.

## Server mode

XO API commands go to one address, the xo server (`https://xo.quirq.dev`, or `XO_URL`, or the one
`xo login --url` saved). `xo me` and `xo user-metadata` read your Clerk account, which needs Clerk's
Secret Key. That key never goes on a user's machine: the same package runs as a server, `xo serve`,
on one deployment that holds it. The server passes every other command (the creates and
`xo spaces`) on to XO's core API under its `/api/core/v1`, with your key, as sent. Until that
deployment holds XO's production Clerk key, `xo me` answers 401 to real XO keys; the creates do not
need it. See
[docs/server.md](docs/server.md) and, to deploy it, [docs/deploy-vercel.md](docs/deploy-vercel.md).

## Deploy a GitHub repository with Coder

Install the [Coder CLI](https://coder.com/docs/install/cli) separately, then run `xo coder login`:
it logs Coder in to the deployment that has the `xo-github` template, in your terminal. Coder keeps
its own login; xo needs no `XO_API_KEY` for it and never hands that key to Coder. Tested with Coder
2.36.4. `xo login` and `--url`/`XO_URL` configure XO API calls; Coder uses
`--coder-url`/`XO_CODER_URL`. Coder's own `CODER_URL` does not pick the deployment. In CI, set
`CODER_SESSION_TOKEN` together with `CODER_URL` naming the same deployment; with the token set and
`CODER_URL` missing or naming another one, xo stops with nothing sent.

On Linux, Coder keeps one login for whichever deployment you last logged in to, and sends it to any
deployment it is told to use. So before every Coder command, xo asks Coder (without naming a
deployment) where it is logged in, and stops with nothing sent unless it is xo's deployment. Coder
options that change the deployment, token or login store (`--url`, `--token`, `--global-config`,
`--use-keyring`, `--header`, `--header-command`, `--client-tls-*`) are refused anywhere in the
arguments after the command, even after `--`.

```sh
xo coder login
xo coder github https://github.com/OWNER/REPO --dry-run
xo coder github https://github.com/OWNER/REPO

# A branch and an explicit startup command; the app must listen on port 3000.
xo coder github https://github.com/OWNER/REPO --name my-preview --ref main \
  --start-command 'npm ci && npm run dev -- --hostname 0.0.0.0 --port 3000'

# Another deployment with the same template; flags also work before `coder` or `github`.
XO_CODER_URL=https://coder.example.com xo coder github https://github.com/OWNER/REPO
xo --coder-url https://coder.example.com coder github https://github.com/OWNER/REPO

# Any of list, show, start, stop, restart, ssh, open, ping, port-forward, update runs as
# `coder --url DEPLOYMENT ...` in your terminal, after the same check. xo has no delete; use the dashboard.
xo coder list
xo coder ssh my-preview
xo coder logout   # only if Coder is logged in to xo's deployment; asks first
```

The default workspace name is the repository name without `.git`, lowercased, with punctuation
changed to hyphens and shortened to 32 characters. An existing name fails; choose `--name` for
another preview. The command never updates, restarts or deletes an existing workspace.

The defaults are `--capacity spot` (shared/free), `--workspace-size xo.small`,
`--home-disk-size 5`, the repository's default branch and `--start-command auto`. `auto` detects
Node apps; `manual` only clones the repository and shows the template's status page. A custom
command runs inside `~/repo`. `--ref` selects a branch or tag at creation. Disk size is fixed at
creation. `--capacity ondemand` explicitly selects dedicated/paid capacity. `--org` selects a
Coder organization; otherwise Coder uses its configured organization.

The command waits up to `--wait-timeout 600` seconds for Coder to create/start the workspace.
The repository may still be installing or building afterward: the public status page switches to
the app when it listens on port 3000. It prints JSON with `name`, `repoUrl`, `workspaceUrl` and
`appUrl`. The app URL comes from Coder's configured routing, without a hardcoded preview hostname
or a session token. **The app is public.** The workspace dashboard and shell still use Coder login.

`--dry-run` prints the deployment URL, name and template parameters without running Coder or
requiring a login. Provisioner logs are not printed, because they can contain credentials. If
creation fails, times out or is interrupted, inspect the workspace in Coder before retrying: it
may already exist. Use Coder's dashboard or `coder show NAME` to diagnose a failed build.

Keep secrets out of `--start-command` and template parameters. Use
[Coder secrets](https://coder.com/docs/user-guides/user-secrets) or a private workspace env file.
See [docs/coder.md](docs/coder.md) for the command's setup and output example.

## How commands are named

The command is the path without a leading `/api` or `/vN` and without its `{placeholders}`, joined
with dashes. When several methods share a path, each is an action: `list` and `get` (GET on the
collection or one item), `create` (POST), `update` (PATCH), `replace` (PUT), `delete` (DELETE).
Path, query and header parameters and the fields of a JSON body are the flags; a field nested in an
object is named by its path (`providers.anthropic.apiKey` is `--providers-anthropic-api-key`). A
body field the spec types as a string keeps the text you type; other values are JSON when they
parse. Required ones are checked before anything is sent. A spec that cannot be read leaves `ops`,
`call` and `api` working and makes generated endpoint commands stop with one line saying why.
The built-in `login`, `logout`, `serve` and `coder` commands also work independently of the spec.

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

Through the xo server only the creates and `GET /spaces/{spaceId}` pass; any other `xo api`
request, or an extra operation from your own `--spec`, gets the server's `HTTP 404: Not found`. To
send one straight to XO, add `--url https://dev.xo.builders` and give the key with `XO_API_KEY` or
`--api-key -` (the key `xo login` saved is only sent to the address it was saved with, so a one-run
`--url` always needs one of those).

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
| API key | `XO_API_KEY` | `--api-key -` (asks without echo on a terminal, else reads one line from stdin) | the key `xo login` saved in `$XDG_CONFIG_HOME/xo/credentials` (`~/.config/xo/credentials`); required for XO API calls except a dry run; Coder uses its own login |
| Address | `XO_URL` | `--url` | the one `xo login` saved, then `https://xo.quirq.dev`; the core API is under its `/api/core/v1`. The flag wins over the variable. `XO_BASE_URL` and `XO_SERVER_URL` are no longer read: xo stops with a line saying so while either is set, unless `--url` or `XO_URL` is given |
| Coder deployment | `XO_CODER_URL` | `--coder-url` | `https://dev-coder.xo.builders`; used only by `xo coder` |
| Spec | `XO_SPEC` | `--spec` | the copy bundled with xo |

`--api-key KEY` also works, but the key then lands in your shell history and in the process list
other users of the machine can read. Prefer `xo login`, the variable, or `--api-key -`. The flag wins
over the variable, and the variable over the saved key. The saved key is only sent to the address
it was saved with. xo refuses a saved key file that other users could read, or that is a symlink. The options go before or after the command.

## Safety

- The key goes only in the `Authorization: Bearer` header. xo reads the spec only from a file
  (the bundled one, `--spec` or `XO_SPEC`); it never fetches one from the API with your key. It is redacted from any response or
  error that echoes it, and tests check it never reaches stdout or stderr, including on argument
  errors and unexpected failures. An argument error never repeats a value you typed, so a
  mistyped key flag (`--api_key KEY`, `--token=KEY`, `--api-keyKEY`) stays off the screen. A key
  with anything but letters, digits and `_ . - + / =` is refused without being shown, and the key
  is also redacted where escaping changes any of its characters, in any mix: URL escaping (`%2F`,
  in either case, or double-encoded as `%252F`, and `+` read as a space), JSON (`\/`, `\u002f`)
  and HTML (`&#x2F;`, `&#47;`, zero-padded or not, and `&sol;`).
- xo never follows a redirect, so a redirect cannot carry the key to another host. A redirect to
  XO's sign-in page is reported as "this route does not accept an API key".
- Plain `http://` is refused except to this machine.

## Develop

```sh
python -m pip install -e ".[test]"
python -m pytest -q
```

Tests run against a fake API on 127.0.0.1 (`tests/conftest.py`) and a synthetic spec
(`tests/fixtures/openapi.json`); they never reach XO. Coder tests use a fake executable/runner;
they require neither Coder nor a deployment and create no workspaces.

## License

Apache-2.0.
