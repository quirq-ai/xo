# xo architecture

One package, two halves: the `xo` command line on your machine, and the xo server (`xo serve`, on
Vercel at `xo.quirq.dev` through `app.py`), which holds the Clerk Secret Key so the CLI never needs
it. Standard library only. Setup is in [quickstart.md](quickstart.md); the server's settings in
[server.md](server.md).

This describes main at 4027932 (through #33) plus the unified `xo create` change. Every command has its own sequence diagram in
section 3. [architecture.html](architecture.html) is the same doc as a page with drawn diagrams;
GitHub shows it as source, so open it locally or from a static host.

| Mode | Runs on | Holds | Entry point |
|---|---|---|---|
| CLI | your Mac or Linux machine, Python 3.11+ | your XO API key; Coder and gh keep their own logins | `xo` = `xo.cli:main` |
| Server | Vercel, Docker, or `xo serve` locally | `CLERK_SECRET_KEY`, in its environment only | `app.py` = `wsgi_app()` |

## 1. System context

The CLI sends your XO key to one address, the xo server. The server answers `/me` and
`/user-metadata` through Clerk, writes your `projects` and `spaces` lists there, and passes creates
and the space read on to XO's core API with your key as sent. Projects are cloned with your own git.

```mermaid
flowchart LR
  subgraph machine[Your machine]
    cli[xo CLI]
    cred[~/.config/xo/credentials]
    coder[coder CLI]
    gh[gh CLI]
    git[git]
    cli -- save --> cred
    cli -- runs --> coder
    cli -- runs --> gh
    cli -- runs --> git
  end
  server[xo server<br/>xo.quirq.dev]
  clerk[Clerk<br/>api.clerk.com]
  core[XO core API<br/>dev.xo.builders/api/core/v1]
  coderdep[Coder<br/>dev-coder.xo.builders]
  github[GitHub]
  cli == XO API key ==> server
  server -- key, user --> clerk
  server == your key ==> core
  coder -- Coder session token --> coderdep
  gh -- gh token --> github
  git -- clone, fetch, push over https --> github
```

## 2. Commands

Most commands are generated: `commands.register()` reads the bundled spec (`src/xo/openapi.json`,
or `--spec`) and makes one command per operation, with every parameter and body field as a flag.
XO's seven space creates are the exception: they are the kinds of `xo create space NAME KIND`.
`xo coder VERB` is caught before argparse and handed to the coder CLI.

```mermaid
flowchart LR
  argv[xo ARGS] -- argv --> main["cli.main()"]
  main -- coder VERB --> pass["_coder_pass()"] -- runs --> coderc[coder CLI]
  main -- built-in --> builtin[login, status, projects, create, connect, list, install, update, sync, ops, call, api, serve]
  main -- endpoint --> gen["commands.register()"]
  spec[openapi.json or --spec] -- reads --> gen
  builtin -- send --> client["Client.request()<br/>Bearer key, no redirects"]
  gen -- send --> client
```

| Command | Goes to | What it does |
|---|---|---|
| `xo create space NAME claude-code --volume-size GB` | server, then XO | a Claude Code space named NAME on XO and in your spaces; add `--providers-anthropic-api-key -` for the Anthropic provider |
| `xo create space NAME codex --volume-size GB` | server, then XO | a Codex space |
| `xo create space NAME antigravity --volume-size GB` | server, then XO | an Antigravity space |
| `xo create space NAME hermes --volume-size GB` | server, then XO | plus a provider key and a Telegram or Slack bot token |
| `xo create space NAME openclaw --volume-size GB` | server, then XO | plus at least one provider key and one channel (Telegram, WhatsApp or Slack) |
| `xo create space NAME xo-cowork --volume-size GB` | server, then XO | providers and channels optional |
| `xo create space NAME nebius-openclaw --token-factory-api-key K --telegram-bot-token T` | server, then XO | feature flagged, 404 when off |
| `xo spaces --space-id ID` | server, then XO | `GET /spaces/{spaceId}`: connection URLs with a session token |
| `xo me` | server only | your profile, primary email and both metadata buckets, as `{"xo": {..., "public_data", "private_data"}}`; credential-looking values masked |
| `xo projects [add NAME]` | server only | read or append to the `projects` list in your private metadata |
| `xo create project NAME [URL]` | server, then git | add the project, then clone `OWNER/REPO` here or `git init` a plain name; a NAME given with a URL must match it |
| `xo connect project:P space:S [--ref R]` | server only | set P in space S at R (default `main`) |
| `xo connect project:P --repo URL` | server, then git | a project made with a name only gets its repo and is renamed `OWNER/REPO`; `./P` gets that origin and moves to `./OWNER/REPO` |
| `xo connect space:S [--id ID]` | server, then XO | record a space entry; `--id` ties it to an XO space your key can read |
| `xo list projects\|spaces` | server only | show either list |
| `xo install project:P\|space:S` | server, git | clone, or fetch and fast-forward, P; for a space, each project into `./S/` at its ref |
| `xo update project:P\|space:S` | git, server | push P (never forced); for a space, push each branch, then record the refs under `./S/` |
| `xo sync project:P\|space:S` | git, server | `pull --rebase` and push P; for a space, rebase and push each branch, merge refs, record |
| `xo user-metadata get --user-id ID` | server only | admin keys only: both metadata buckets |
| `xo user-metadata update --user-id ID --private-metadata JSON` | server only | admin keys only; deep merges into allow-listed keys (also `--public-metadata`) |
| `xo login [xo\|me\|coder\|github]` | server, Coder, gh | reads `/config`, checks and saves the key, runs Coder's and gh's logins |
| `xo logout`, `xo status` | local, Coder, gh | delete saved login; show each service |
| `xo ops`, `xo call`, `xo api` | local / server | list operations; run one by id; send any request |
| `xo coder github URL` | Coder | create an `xo-github` workspace |
| `xo coder login`, `logout`, `list`, `ssh`, ... | Coder | Coder's own commands after the login check; no delete |
| `xo serve` | local | run the server on 127.0.0.1:8787 |

## 3. Every command, step by step

One swimlane per command: who does each step, in order, and what moves between them. Highlighted is the step the command exists for; dashed is a step that happens only sometimes.

### Log in and check

Setup commands. Only xo login sends your key anywhere, and only to the xo server.

#### `xo login`

```mermaid
sequenceDiagram
  participant Cli as You · xo CLI
  participant Srv as xo server
  participant Xo as XO core API
  participant Git as git · GitHub
  participant Coder as Coder
  Note over Cli: xo login: or: xo login coder
  Cli->>Srv: fetch / GET /config: addresses, no key
  Srv->>Cli: the list / ask for your key: hidden, must start ak_
  Cli->>Srv: your key / GET spaces/0000...: a space no one has
  Srv->>Xo: forwarded / Space not found: 404 means the key works
  Xo->>Cli: 404 / save key + URL: ~/.config/xo/credentials
  Cli->>Coder: then / coder login: in your terminal
  Coder->>Git: then / gh auth login: only if not logged in
```

A key is saved only after XO accepts it. A 401, or the server's own bare 404, saves nothing. Each service prints one line; xo login xo, coder or github does just that one.

#### `xo logout`

```mermaid
sequenceDiagram
  participant Cli as You · xo CLI
  participant Git as git · GitHub
  participant Coder as Coder
  Note over Cli: xo logout
  Note over Cli: delete key + addresses: the files login saved
  Cli-->>Coder: if logged in there / coder logout: asks, xo's deployment only
  Coder-->>Git: if you say yes / gh auth logout: asks first
```

Logout is local first. Coder and gh are logged out only where they point at xo's deployment and github.com, and only after asking.

#### `xo status`

```mermaid
sequenceDiagram
  participant Cli as You · xo CLI
  participant Git as git · GitHub
  participant Coder as Coder
  Note over Cli: xo status
  Note over Cli: read saved login: never prints the key
  Cli->>Coder: ask / where is Coder?: asked without a URL
  Coder->>Git: ask / gh auth status: who, on github.com
  Git->>Cli: answers / four rows: me, xo, coder, github
```

Status sends no key. It only asks the Coder and GitHub CLIs who they are logged in as.

### Your account

Answered by the xo server itself, with the Clerk Secret Key only it holds.

#### `xo me`

```mermaid
sequenceDiagram
  participant Cli as You · xo CLI
  participant Srv as xo server
  participant Clerk as Clerk
  Note over Cli: xo me
  Cli->>Srv: key / GET /me: 60 a minute per key
  Srv->>Clerk: verify / verify key: every call, never cached
  Note over Clerk: read user: profile cached 60 s
  Clerk->>Srv: user / mask secrets: email + both buckets
  Srv->>Cli: profile / print profile: ("xo": (...))
```

You see your own metadata, masked. Values under credential-like names, or that look like keys, show only their last 4 characters or (hidden). XO_ME_PRIVATE_KEYS can narrow the private keys shown.

#### `xo user-metadata get | update`

```mermaid
sequenceDiagram
  participant Cli as You · xo CLI
  participant Srv as xo server
  participant Clerk as Clerk
  Note over Cli: xo user-metadata: --api-key - (admin key)
  Cli->>Srv: key / /user-metadata/(id): 20 a minute per key
  Srv->>Clerk: verify / verify key: has claim xo_admin?
  Clerk->>Srv: claims / admin check: id in XO_ADMIN_KEY_IDS
  Srv->>Clerk: if admin / GET or PATCH user: allow-listed keys only
  Clerk->>Srv: audit line: ids and key names only
  Srv->>Cli: answer / both buckets
```

Admin needs two locks, and none is issued yet. Until a key has the claim and its id is listed, every caller gets 403. PATCH changes only top-level keys named in XO_EDITABLE_*_KEYS.

### Spaces on XO

Every space is created one way, xo create space NAME KIND, and recorded in your spaces. The old xo claude-code, xo codex, ... commands are gone and say so.

#### `xo create space NAME KIND`

```mermaid
sequenceDiagram
  participant Cli as You · xo CLI
  participant Srv as xo server
  participant Clerk as Clerk
  participant Xo as XO core API
  Note over Cli: xo create space dev codex: KIND's flags, no --name
  Cli->>Srv: kind + body / POST /me/spaces: name, room, 6KB first
  Srv->>Xo: POST + key / create space: named dev on XO too
  Xo->>Srv: new id / got the id
  Srv->>Clerk: write / merge (spaces): name, kind, id, rev 1
  Clerk->>Cli: 200 / space recorded
```

One name, on XO and in your spaces. KIND is claude-code, codex, antigravity, hermes, openclaw, xo-cowork or nebius-openclaw; NAME is 1 to 32 letters, digits or hyphens. Everything recording needs is checked before XO is called. Every kind but nebius-openclaw stores your key inside the new space, and xo says so first.

#### `xo claude-code | codex | ... (removed)`

```mermaid
sequenceDiagram
  participant Cli as You · xo CLI
  Note over Cli: xo codex --name a: an old create
  Note over Cli: nothing sent: says: xo create space
```

The old creates only point to the new one. xo stops before parsing, so nothing is sent and no flag value is echoed: xo codex is now xo create space NAME codex [flags].

#### `xo spaces --space-id ID`

```mermaid
sequenceDiagram
  participant Cli as You · xo CLI
  participant Srv as xo server
  participant Xo as XO core API
  Note over Cli: xo spaces --space-id
  Cli->>Srv: GET + key / /api/core/v1/spaces/ID: id: 64 safe characters
  Srv->>Xo: same key / read space: owner only
  Xo->>Cli: answer / connection URLs: carry a session token
```

The only way back to a space XO made. The API has no list, delete, start or stop. Treat the URLs as secrets: their token lasts up to an hour.

### Projects and spaces in your metadata

Each write goes through the server's one engine: a per-user lock, a write that names one list, a read-back after 0.3 s, up to 3 writes.

#### `xo projects [add NAME]`

```mermaid
sequenceDiagram
  participant Cli as You · xo CLI
  participant Srv as xo server
  participant Clerk as Clerk
  Note over Cli: xo projects add N: or just: xo projects
  Cli->>Srv: name / POST /me/projects: ("name") only
  Srv->>Clerk: verify / verify, read user
  Clerk->>Srv: list / append name: rules, 30 max, 6KB
  Srv->>Clerk: write / merge (projects): nothing else written
  Note over Clerk: read back: dropped? write again
  Clerk->>Cli: 200 / the new list
```

Only the projects list changes. A duplicate (ignoring case), a credential-like name, or a full list is refused with nothing written.

#### `xo create project NAME [URL]`

```mermaid
sequenceDiagram
  participant Cli as You · xo CLI
  participant Srv as xo server
  participant Clerk as Clerk
  participant Git as git · GitHub
  Note over Cli: xo create project N: or a URL, or both
  Note over Cli: name the project: a repo: OWNER/REPO
  Cli->>Srv: name / POST /me/projects: the same add as above
  Srv->>Clerk: write / merge (projects)
  Clerk->>Cli: 200 / listed
  Cli->>Git: make ./NAME / clone or git init: OWNER/REPO: clone
```

A project is named after its repo. Give a name (git init ./notes), a GitHub URL (cloned as OWNER/REPO), or both when they name the same repository; a different name with a URL is refused before anything is sent. Record first, then make the folder.

#### `xo connect project:P --repo URL`

```mermaid
sequenceDiagram
  participant Cli as You · xo CLI
  participant Srv as xo server
  participant Clerk as Clerk
  participant Git as git · GitHub
  Note over Cli: xo connect project:notes: --repo URL
  Cli->>Srv: name, repo / POST /me/projects/connect: ("name", "repo")
  Srv->>Clerk: read / read user: notes in no space?
  Note over Clerk: merge (projects): notes becomes OWNER/REPO
  Clerk->>Cli: 200 / the new list
  Cli-->>Git: if ./notes is here / set origin: ./notes to ./OWNER/REPO
```

A project made with a name gets its repo later. The name is replaced in place, through the same engine as an add. Refused while the project is in a space, since the space would still name it the old way. An origin the folder already has is left as it is.

#### `xo connect space:S [--id ID]`

```mermaid
sequenceDiagram
  participant Cli as You · xo CLI
  participant Srv as xo server
  participant Clerk as Clerk
  participant Xo as XO core API
  Note over Cli: xo connect space:S: --id: an XO space
  Cli->>Srv: name / POST .../connect: ("name", "id"?)
  Srv-->>Xo: only with --id / GET spaces/ID: can your key read it?
  Xo->>Clerk: write / merge (spaces): new entry, rev 1
  Clerk->>Cli: 200 / the entry
```

An entry without creating anything. With an id, XO must confirm your key can read that space (403 or 404 there is a 404 here). A name or id already recorded is refused.

#### `xo connect project:P space:S [--ref R]`

```mermaid
sequenceDiagram
  participant Cli as You · xo CLI
  participant Srv as xo server
  participant Clerk as Clerk
  Note over Cli: xo connect project:P: space:S --ref R
  Cli->>Srv: P, ref / POST spaces/S/projects: ("project", "ref")
  Srv->>Clerk: read / read user: P must be in projects
  Note over Clerk: merge (spaces): (P: R), rev + 1
  Clerk->>Cli: 200 / the space
```

One project, one ref, one more rev. A missing ref is main. Connecting P again replaces its ref in place; a space holds at most 10 projects.

#### `xo list projects | spaces`

```mermaid
sequenceDiagram
  participant Cli as You · xo CLI
  participant Srv as xo server
  participant Clerk as Clerk
  Note over Cli: xo list spaces: or projects
  Cli->>Srv: key / GET /me/spaces
  Srv->>Clerk: verify / verify, read user
  Clerk->>Srv: list / mask: refs shown as stored
  Srv->>Cli: answer / print the list
```

A read, masked as /me is. Project names and refs are not masked: a repository called auth-service is not a secret.

### Install, update and sync

Your own git, over https only, with no submodules and none of xo's tokens. The metadata changes only through the server's PUT /me/spaces/{name}.

#### `xo install space:S`

```mermaid
sequenceDiagram
  participant Cli as You · xo CLI
  participant Srv as xo server
  participant Clerk as Clerk
  participant Git as git · GitHub
  Note over Cli: xo install space:S
  Cli->>Srv: key / GET /me/spaces
  Srv->>Clerk: read / read user
  Clerk->>Cli: space S / each project: dirty checkout: refuse
  Cli->>Git: https / clone or fetch: ./S/OWNER/REPO
  Note over Git: checkout ref: fast-forward only
  Git->>Cli: done / save state.json: rev N and refs
```

The installed rev and refs are the base for later updates. A project with no GitHub remote is left as it is. A branch that has moved on from its remote is refused, never merged.

#### `xo install project:P`

```mermaid
sequenceDiagram
  participant Cli as You · xo CLI
  participant Srv as xo server
  participant Git as git · GitHub
  Note over Cli: xo install project:P
  Cli->>Srv: key / GET /me/projects: P must be listed
  Srv->>Cli: yes / ./P here?
  Cli->>Git: https / clone, or fetch: over https
  Note over Git: fast-forward: never a merge
```

Clone once, then only fast-forward. If your branch and its upstream have both moved on, xo stops and points you to xo sync project:P.

#### `xo update space:S`

```mermaid
sequenceDiagram
  participant Cli as You · xo CLI
  participant Srv as xo server
  participant Clerk as Clerk
  participant Git as git · GitHub
  Note over Cli: xo update space:S: reads state.json
  Note over Cli: check checkouts: own, clean
  Cli->>Git: push / git push: each tracked branch
  Git->>Cli: pushed / read refs here: branch, or commit
  Cli->>Git: check / ref on remote?: else refuse
  Git->>Srv: refs / PUT /me/spaces/S: rev N + base
  Srv->>Clerk: write / merge (spaces): rev N + 1, or 409
  Clerk->>Cli: 200 / save state.json
```

Push, then record, only if nobody moved the space. The server refuses with 409 "run xo sync" unless the space is still at rev N and still holds base, the refs you installed. With nothing changed, nothing is recorded.

#### `xo update project:P`

```mermaid
sequenceDiagram
  participant Cli as You · xo CLI
  participant Git as git · GitHub
  Note over Cli: xo update project:P
  Note over Cli: check upstream: own, on a branch, clean
  Cli->>Git: push / push to upstream: never forced
  Git->>Cli: pushed: remote ahead: xo sync
```

A plain push, never forced. It goes to the branch's upstream remote, not a pushRemote, and needs a branch, not a detached HEAD.

#### `xo sync space:S`

```mermaid
sequenceDiagram
  participant Cli as You · xo CLI
  participant Srv as xo server
  participant Clerk as Clerk
  participant Git as git · GitHub
  Note over Cli: xo sync space:S: reads state.json
  Cli->>Git: rebase / pull --rebase, push: each tracked branch
  Git->>Srv: read space / GET /me/spaces
  Srv->>Clerk: read / read user
  Clerk->>Cli: space S / merge refs: installed, yours, space
  Cli->>Git: no clash / checkout merged
  Git->>Srv: refs / PUT /me/spaces/S: only if refs changed
  Srv->>Clerk: write / merge (spaces)
  Clerk->>Cli: 200 / save state.json
```

A three-way merge of refs, per project. If you and the space both changed one project, or the space's rev went back, it stops, names the projects and records nothing. A rebase that conflicts is undone and left for you.

#### `xo sync project:P`

```mermaid
sequenceDiagram
  participant Cli as You · xo CLI
  participant Git as git · GitHub
  Note over Cli: xo sync project:P
  Cli->>Git: rebase / pull --rebase: undone on conflict
  Note over Git: push: never forced
  Git->>Cli: done / rebased, pushed
```

Rebase on the upstream, then push. On a conflict xo runs git rebase --abort, so the checkout is as it was.

### Lower level

For operations the generated commands don't cover, or to see what a command would send.

#### `xo ops [FILTER]`

```mermaid
sequenceDiagram
  participant Cli as You · xo CLI
  Note over Cli: xo ops codex
  Note over Cli: read openapi.json: or --spec, XO_SPEC
  Note over Cli: print operations: id, method, path
```

Offline. The spec is read from disk, never fetched, so ops and every --help work without a network.

#### `xo call OPERATION · xo api METHOD PATH`

```mermaid
sequenceDiagram
  participant Cli as You · xo CLI
  participant Srv as xo server
  participant Xo as XO core API
  Note over Cli: xo call create_codex: --param, --data, --field
  Cli->>Srv: request + key / /api/core/v1/...: creates + space read only
  Srv->>Xo: forwarded / XO's answer
  Xo->>Cli: answer / print JSON: secrets redacted
```

Anything else gets the server's 404. To reach XO directly, add --url https://dev.xo.builders with XO_API_KEY or --api-key -. --dry-run sends nothing and hides provider keys.

### Coder and the server

Coder keeps its own login; xo checks where it points before every call.

#### `xo coder github URL`

```mermaid
sequenceDiagram
  participant Cli as You · xo CLI
  participant Coder as Coder
  Note over Cli: xo coder github URL: --name, --ref, --start-command
  Cli->>Coder: check / where logged in?: must be xo's deployment
  Note over Coder: create workspace: template xo-github
  Note over Coder: wait for the build: up to 600 s
  Coder->>Cli: done / print URLs: workspace + public app
```

The app on port 3000 is public. --dry-run prints the plan without Coder. Provisioner logs are never printed, since they can hold credentials.

#### `xo coder login | logout | list | ssh | ...`

```mermaid
sequenceDiagram
  participant Cli as You · xo CLI
  participant Coder as Coder
  Note over Cli: xo coder ssh my-app: caught before argparse
  Cli->>Coder: check / where logged in?: asked without a URL
  Note over Coder: coder --url DEP ssh: in your terminal
```

No Coder command runs against another deployment. Options that change the deployment, token or login store are refused anywhere after the verb. xo has no delete.

#### `xo serve`

```mermaid
sequenceDiagram
  participant Cli as You · xo CLI
  participant Srv as xo server
  Note over Cli: xo serve: --host, --port
  Cli->>Srv: start / ThreadingHTTPServer: 127.0.0.1:8787
```

The same App Vercel runs. It needs CLERK_SECRET_KEY in its environment and listens on localhost by default; anything else should sit behind HTTPS.

## 4. The server

```mermaid
flowchart LR
  wsgi["wsgi_app() (Vercel)"] -- WSGI --> app["App.handle()"]
  http["ThreadingHTTPServer (xo serve)"] -- HTTP --> app
  app -- public --> pub["/, /status, /healthz, /config"]
  app -- key --> acct["/me, /me/projects, /me/spaces/**, /user-metadata"] -- verify --> clerk[Clerk]
  app == forward ==> fwd["/api/core/v1/..."] == your key ==> core[XO core API]
```

## 5. Projects and spaces in your metadata

Two lists in your own Clerk private metadata are the only things a normal key can change. The
client sends a name, kind, id, or project and ref, never metadata; the server builds every change
and writes through one engine, `App._change()`.

```
projects: ["quirq-ai/xo", "notes"]            OWNER/REPO is cloned; a plain name gets git init (max 30)
spaces:   [{"name": "dev", "kind": "claude-code", "id": "<XO space id>", "rev": 2,
            "projects": [{"quirq-ai/xo": "main"}, {"notes": "v1.2"}]}]   (max 10, 10 projects each)
./S/.xo/state.json (on your machine): {"space", "rev", "projects"} as installed
```

Each space's project keys must be in `projects`. Both lists share one 8KB Clerk bucket with XO's own
keys, so xo stops writing at 6KB.

```mermaid
sequenceDiagram
  participant CLI as xo projects add / create / connect / update
  participant S as xo server
  participant C as Clerk
  CLI->>S: POST or PUT /me/projects, /me/spaces/** (names and refs only)
  S->>S: verify key, 30/min, validate with names.py and the 6KB budget
  S->>S: one change per user (lock, this instance)
  loop up to 3 writes, stop after 10 s
    S->>C: GET /users/{id}
    S->>C: PATCH metadata naming only projects or spaces
    S->>S: wait 0.3 s
    S->>C: read back: anything dropped by a racing write?
  end
  S-->>CLI: 200 with the new list, 409 if someone else changed the space, or 503
```

| Route | Command | Change |
|---|---|---|
| `POST /me/projects` | `xo projects add`, `xo create project` | append a name |
| `POST /me/projects/connect` | `xo connect project:P --repo URL` | replace a plain name with its `OWNER/REPO`, in place; refused while P is in a space |
| `POST /me/spaces` | `xo create space` | check name and budget, create on XO with your key, record `{name, kind, id, rev: 1}` |
| `POST /me/spaces/connect` | `xo connect space:S [--id]` | new entry; with an id, only after XO confirms your key can read it |
| `POST /me/spaces/{name}/projects` | `xo connect project:P space:S` | set one project's ref |
| `PUT /me/spaces/{name}` | `xo update`, `xo sync` | replace projects and refs if the space is still at `rev` holding `base`, else 409 "run xo sync" |
| `GET /me/projects`, `GET /me/spaces` | `xo projects`, `xo list` | read, masked as `/me` is (refs shown as stored) |

## 6. Install, update and sync

A new `workspace.py` drives `gitops.py`. The metadata is only changed by the server, through
`PUT /me/spaces/{name}`.

```mermaid
flowchart LR
  meta[space S in metadata<br/>rev N, project: ref]
  local[./S/OWNER/REPO<br/>./S/.xo/state.json]
  meta -- read --> install[xo install space:S] -- clone, checkout, save rev N --> local
  local -- push, read refs --> update[xo update space:S] -- PUT rev N + base, 409 if moved --> meta
  meta <-- read, then PUT --> sync[xo sync space:S] <-- rebase, push, checkout --> local
```

- `xo update` pushes each tracked branch first; `xo sync` rebases each on its upstream and pushes it,
  undoing a rebase that conflicts. Tags and commits stay where they are.
- `xo sync` keeps the space's change, or yours, per project, and stops on a project both changed
  (or one the space dropped while you changed it).
- A ref is recorded only if it is on the project's remote; a checkout with uncommitted changes is never touched.
- Single projects use git only: install clones, or fetches and fast-forwards; update pushes (never
  forced); sync runs `pull --rebase` then pushes.
- git runs with `GIT_ALLOW_PROTOCOL=https`, no submodules, `--end-of-options` before names and refs, and without XO, Clerk, Coder or gh tokens in its environment.
- Two updates on different instances that both read rev N within the 0.3 s settle can both get 200;
  the later write stands and the other client's next update is refused, since the space no longer
  holds its `base`. If a stale write sends the rev backwards, `xo sync` stops and names the projects
  that differ.

## 7. Access model

- `client.py` is the only CLI code that talks to XO; `gitops.py` runs git and `github.py` runs gh. `Clerk` and `Core` in `server.py` are the only server code that talks out.
- The key is never printed, and is redacted in escaped forms too. Plain `http://` is refused except to this machine.
- `/me` returns your own email and both metadata buckets, with credential-like values masked;
  `XO_ME_PRIVATE_KEYS` can narrow the private keys it shows.
- Only `projects` and `spaces` are writable by a normal key, each written alone on the key's own user, with one audit line per change (a count, never a value).
- An admin key needs the claim `xo_admin: true` and an id in `XO_ADMIN_KEY_IDS`. None is issued yet.
- Before each Coder command, xo checks Coder is logged in to xo's deployment.

| Secret | Lives in | Sent to |
|---|---|---|
| XO API key | `~/.config/xo/credentials` or `XO_API_KEY` | the server it was saved with, which forwards it (also for `xo create space` and `connect --id`); copied into each new space except `nebius-openclaw` |
| `CLERK_SECRET_KEY` | the server's environment | `api.clerk.com` |
| Coder session token | Coder's own store | xo's Coder deployment |
| GitHub token | gh's store or your git credential helper | github.com over https; xo never reads it |

## 8. Where state lives

No database. Clerk: your `projects` and `spaces` in private metadata. CLI: `credentials` and
`services` under `$XDG_CONFIG_HOME/xo/`, and `./S/.xo/state.json` per installed space.
Package: `services.json` (served at `/config`) and `openapi.json`. Server: process memory only
(last 100 requests, rate limits for up to 10,000 keys, `/me` cache for 60 seconds, one change lock
per user), plus audit lines on stderr.

## 9. Folder map

42 tracked files.

```
src/xo/
  cli.py            CLI    argparse; built-ins: login, status, projects, create, connect, list,
                           install, update, sync, coder
  workspace.py      CLI    install, update, sync
  gitops.py         CLI    the user's git: https only, own checkouts, no submodules, no xo tokens
  client.py         CLI    the only XO network code, saved key, redaction
  commands.py       CLI    spec operations to commands and flags
  spec.py           CLI    load and check the OpenAPI spec
  coder.py          CLI    xo-github template inputs
  github.py         CLI    gh login, status, logout
  names.py          shared project, space and ref rules; the only module both sides import
  services.py/.json shared services list and saved addresses
  openapi.json      shared bundled core API spec
  server.py         server App, Clerk, Core forward, /me masking, list write engine, limits
  status_page.py    server status page, CSP pinned by hash
app.py              server Vercel entry
Dockerfile          server non-root image running xo serve
tests/              shared pytest on loopback: fake XO, fake Clerk, local git remotes
                           (test_workspace.py drives install, update and sync)
docs/               shared guides and the server test script
```
