# xo server mode

One package, two deployments:

- **The command line** runs on each user's machine with their XO API key (`XO_API_KEY`, or the key `xo login` saved).
- **The server**, `xo serve`, runs once, on a machine XO controls, with Clerk's Secret Key. It is
  the only place that key exists.

Every command goes to the server (`https://xo.quirq.dev`, `XO_URL`, or the address `xo login`
saved). It answers `xo me` (and the admin-only `/user-metadata`, which has no xo command) itself, and passes the creates and `xo spaces --space-id ID`
on to XO's core API, so the CLI needs one address.

```
xo me ──────────────┐                      ┌── POST /v1/api_keys/verify
/user-metadata ─────┤                      ├─ CLERK_SECRET_KEY ─► api.clerk.com
                    ├─ XO_API_KEY ─► xo serve └── GET /v1/users/{id}, PATCH /v1/users/{id}/metadata
xo create space ────┘                      └── /api/core/v1/... ─ the caller's key ─► dev.xo.builders
```

## Routes

| Method | Path | Who | Returns |
|---|---|---|---|
| GET | `/healthz` | anyone | `{"status": "ok"}`, with no Clerk call |
| GET | `/` | anyone | the status page: health, counts and the latest requests, refreshed every 5 seconds |
| GET | `/status` | anyone | what the page shows, as JSON; no Clerk call |
| GET | `/config` | anyone | the services list `xo login` reads (`src/xo/services.json`), with `xo.url` set to where this server passes `/api/core/v1` on to (null when that is not a plain https address); addresses only (`me.url` is xo's shipped address, not necessarily this server), no Clerk call; answers even when the server is missing its settings |
| GET | `/login` | anyone | `{"browser": null}`, or where `xo login` signs in: Clerk's `/oauth/authorize` address, the public client id, the scope, the three loopback redirect URLs and how long a new key works; no Clerk call. Set by the two browser login settings below |
| POST | `/login/browser` | anyone (no key) | `{"code", "verifier", "redirect", "name"?}` from a browser sign-in: see "Browser login" below; answers `{"key", "days", "reused"}`. 404 while browser login is off |
| GET | `/me` | any user key | `id`, `username`, `firstName`, `lastName`, `imageUrl`, `createdAt`, `email` (primary), `publicMetadata`, `privateMetadata`: the key's own user only, with secret-looking values masked |
| GET | `/me/projects` | any user key | `projects`: the list in the key's own private metadata (`[]` when there is none) |
| POST | `/me/projects` | any user key | adds `{"name": "..."}` to that list and answers the new list; see "Projects" below |
| POST | `/me/projects/connect` | any user key | `{"name", "repo"}`: a project with no repo gets GitHub's `OWNER/REPO`, which replaces its name in that list; see "Projects" below |
| GET | `/me/spaces` | any user key | `spaces`: the list in the key's own private metadata (`[]` when there is none) |
| POST | `/me/spaces` | any user key | `{"kind", "name", "body"}`: creates the space through XO with the caller's key, then records it; see "Spaces" below |
| POST | `/me/spaces/connect` | any user key | `{"name", "id"?}`: a new space entry with no projects; with an id, only for an XO space the caller's key can read |
| POST | `/me/spaces/{name}/projects` | any user key | `{"project", "ref"}`: sets one project's ref in that space |
| PUT | `/me/spaces/{name}` | any user key | `{"rev", "projects", "base"}`: replaces the space's projects and refs if it is still at `rev` holding `base`, else 409 |
| GET | `/user-metadata/{userId}` | admin keys | `publicMetadata`, `privateMetadata` |
| PATCH | `/user-metadata/{userId}` | admin keys | both buckets after a deep merge; `null` removes a key |
| POST | `/api/core/v1/{create}` | an `ak_` key | XO's answer: `claude-code`, `codex`, `antigravity`, `hermes`, `openclaw`, `xo-cowork`, `nebius-openclaw` |
| GET | `/api/core/v1/spaces/{spaceId}` | an `ak_` key | XO's answer |

Answers use the core API's envelope: `{"success": true, "data": ...}` or
`{"success": false, "error": "..."}`, with 401 (bad key, or a key not tied to a user), 403 (not an
admin key), 404 (no such user), 405, 413 (body over 32KB), 422 (bad body, a key that is not
editable, a bad user id, or over Clerk's 8KB per bucket), 429 (this key's rate limit), 502 (Clerk
did not answer) and 503 (the server is busy, or misconfigured: Clerk refused its Secret Key, which
it also says in one line on stderr). A core API route answers whatever XO answered.

## Passing the core API on

The server sends a core API request on to `XO_CORE_API_URL` (default
`https://dev.xo.builders/api/core/v1`, the `xo` service) with the caller's own key and body, and passes XO's status
and JSON back as they came. XO checks the key; the server does not ask Clerk about it.

**Changed: the default moved from `https://app.xo.builders` to `https://dev.xo.builders`.** Every
create, carrying the caller's key, goes to the new address from the first deploy of this version.
Setting `XO_CORE_API_URL` in the server's environment (in Vercel: Settings → Environment Variables)
overrides it; `https://app.xo.builders/api/core/v1` keeps the old one.

- **Only the routes above.** Nothing else under `/api/core/v1` leaves the server: other paths,
  methods, a path with any `%` escape in it, and a space id beyond 64 letters, digits, `-` and `_`
  are a 404. The query string is dropped. The XO address comes only from the server's environment.
- **Only an API key.** A request without a bearer token that looks like an `ak_` key is a 401 and
  goes nowhere. Each key may send 30 a minute (per instance); the Firewall rule in
  docs/deploy-vercel.md caps each address.
- **Only the key and the body go on.** The request carries `Authorization`, `Accept`,
  `Content-Type` and the server's own `User-Agent`; no cookie, forwarded address or other header of
  the caller's. Redirects are not followed.
- **Nothing is logged** but the request line (method and path, which holds a space id). Keys,
  bodies (with their provider keys and bot tokens) and answers are never logged or kept. The status
  page names the route (`/api/core/v1/codex`, `/api/core/v1/spaces/{spaceId}`), never an id.
- **What it costs.** The keys and provider tokens in a create now pass through this server's
  memory. Every user reaches XO from the server's addresses, not their own. XO's failures do not
  count toward **Degraded**, which is about Clerk.
- **A budget for XO.** All requests passed on share `XO_CORE_PER_SECOND` (default 10 a second per
  process); past it the server answers 503 "The server is busy; try again shortly" without passing
  anything on. Made-up keys step around the per-key limit, so this is what bounds them per instance.
- **Timeouts.** XO gets 25 seconds for the whole answer, however slowly it sends. A create that
  times out answers 504 "XO (dev.xo.builders) did not answer in time; it may have been received,
  so check before you retry", naming the host it passes on to; one XO never answered, 502. An
  answer over 1 MiB is not passed on: the caller gets XO's status and "too large to pass on".
- **No proxy for XO.** Requests passed on to XO go straight to `XO_CORE_API_URL`; `HTTP_PROXY` and
  `HTTPS_PROXY` are not honoured for them (Clerk calls still use them). On a machine whose only way
  out is a proxy, `/me` works but `/api/core` answers 502.

## Browser login

`xo login` can sign you in in a browser instead of asking for a key. Clerk is the sign-in: the
server is the client of a Clerk OAuth application (a public client, so it holds no client secret
for it), and the CLI does the OAuth authorization code flow with PKCE.

1. `GET /login` tells the CLI Clerk's `/oauth/authorize` address, the client id and the redirect
   URLs. The CLI listens on the first free of `http://127.0.0.1:8976/callback`, `:8977` and `:8978`,
   makes a verifier and its S256 challenge and a random state, and opens the sign-in link.
2. You sign in on Clerk's own page. Clerk sends the browser back to the CLI's loopback address with
   a code; the CLI ignores any answer whose state is not the one it sent.
3. The CLI sends `POST /login/browser` `{"code", "verifier", "redirect", "name"}`. The server swaps
   them at Clerk's `/oauth/token` (with the client id, no secret), reads `sub` from `/oauth/userinfo`
   (a `user_...` id or nothing), and drops the access token.
4. With the Secret Key it lists that user's API keys and answers the newest one browser login
   made before (its description is `made by xo login in a browser`) that is theirs, still working
   with a week or more left, and has no claims or scopes (so never an admin key) and is not in
   `XO_ADMIN_KEY_IDS`. A key made any other way is never read back. If there is none, or Clerk will
   not give its secret back, it makes one: named by the CLI (`xo CLI on HOST`), no claims, expiring
   after 90 days; at most 5 a day per account, revoked ones included. If Clerk cannot list the
   keys, it makes none. A key is revoked in the Clerk dashboard (API keys).
5. The key is in that one answer only; it is never stored, logged or put in the audit line, which
   names the key id, the user and whether it was reused. The CLI checks it with XO and saves it like
   a pasted key.

A code works once and only with the verifier the CLI kept, and the redirect is always this
machine's own loopback, so a sign-in link someone else sends you gives them nothing. Swaps are
capped at 2 a second for the whole process, on top of the Clerk budget; on Vercel that is per
instance, so docs/deploy-vercel.md section 6, step 5, also caps `/login` per address. A key
piped in on stdin (`printf KEY | xo login`) is read as before, without a browser. XO's platform
checks keys with the same Clerk instance this server's Secret Key belongs to, so the key works
there; if the two ever differed, XO would refuse it and the CLI would save nothing.

## The status page

`/` shows whether the server answers, how many requests and `/healthz` pings it has served, how
many were refused or failed, and the last 100 requests. Each entry is only the time, method,
route (`/me`, `/user-metadata/{userId}`, `/healthz`, a core API route such as
`/api/core/v1/spaces/{spaceId}`, or `other`), status and duration: never a key,
key id, user id, path or address, so it is public. The page and `/status` are not counted. The
counts live in the process's memory: on Vercel each instance keeps its own and starts empty, so the
page shows what the instance answering you has seen. The audit log stays in the logs only.

The badge says **Operational**, **Degraded** when a request to `/me` or `/user-metadata` (the
routes that call Clerk) failed with a 5xx in the last 5 minutes (a refused Clerk secret, Clerk
down, or the server's own Clerk budget used up), or **Unreachable** when `/status` does not answer.
Other traffic does not count, so it cannot hide a failure, and a failure stops counting 5 minutes
later without any further requests. `recent` in `/status` holds those two counts, and the badge
reads "Degraded: 3 of 11 failed in 5 min": 3 of the 11 requests to those routes in the last 5
minutes. The
page refreshes every 5 seconds while its tab is visible, stops while it is hidden, and slows to
every 30 seconds after three failures in a row. `/`, `/status` and `/healthz` also answer `HEAD`,
for uptime monitors. Public by design: the version, each instance's start time and traffic volume,
and when each request (admin metadata reads and writes included) happened and how long it took.

## Who can do what

- **Any user key** can read its own account through `/me`, including its own public and private
  metadata. The user is the one Clerk ties the key to, never an id from the request. The key is
  verified with Clerk on every request and never cached, so a revoked key stops at once. Under a
  metadata name that reads like a credential (token, secret, password, credential, cookie, key,
  `apiKey`, `accessKey`, `privateKey`, auth, session, jwt and the like, at any depth), `/me` shows
  only the last 4 characters of a string 12 characters or longer, and `(hidden)` for any other
  value but null and the empty string. Under a password-like name (password, passphrase, pin, otp
  and the like) it shows `(hidden)` whatever the length. A string that looks like a credential
  under any name (`sk-`, `ghp_`, `ak_`, `AKIA`, a JWT, a PEM private key, a URL with a password,
  `Bearer ...` and the like) is masked the same way. Admin rights never come from metadata, so a
  user reading their own does not learn anything that grants them. Metadata values are never
  written to the log or the status page. To show users only some of their private metadata, list
  its top-level keys in `XO_ME_PRIVATE_KEYS`; unset, `/me` shows all of it.
- **An admin key** is a Clerk API key that has the claim `xo_admin: true` **and** whose id is in
  `XO_ADMIN_KEY_IDS`. A key with the claim but not on the list is refused, and the refusal is
  logged with `claimedAdmin: true`, since that is what a forged key would look like.
- **Only allow-listed keys change.** `PATCH` may only touch the top-level keys named in
  `XO_EDITABLE_PUBLIC_KEYS` and `XO_EDITABLE_PRIVATE_KEYS`. Both start empty, so nothing is
  editable until you name keys. Reads return both whole buckets.
- **One write reaches another user.** `POST /me/share` lets any user key add one invite to another
  user's `shared` list, and nothing else of theirs is written or answered (see Sharing below).
  Every other write a normal key makes is to its own user's metadata.
- Auth runs first, so a caller without an admin key learns nothing about bodies or users.

## Configuration

Environment only, so the secret never lands in shell history or the process list.

| Variable | Needed | What |
|---|---|---|
| `CLERK_SECRET_KEY` | yes | Clerk's Secret Key (`sk_live_...`); never printed or logged |
| `XO_ADMIN_KEY_IDS` | for metadata | comma-separated Clerk API key ids (`ak_...`) that may act as admin |
| `XO_EDITABLE_PUBLIC_KEYS` | for edits | comma-separated top-level public metadata keys PATCH may change |
| `XO_EDITABLE_PRIVATE_KEYS` | for edits | the same for private metadata |
| `XO_ME_PRIVATE_KEYS` | no | comma-separated top-level private metadata keys `/me` shows its user; unset, all of them |
| `XO_CLERK_PER_SECOND` | no | calls to Clerk per second for the whole process; default 20 |
| `XO_CORE_PER_SECOND` | no | requests passed on to XO per second for the whole process; default 10 |
| `XO_SERVE_MAX_CONNECTIONS` | no | connections served at once; default 64, then 503 |
| `CLERK_API_URL` | no | default `https://api.clerk.com`; tests point it at a fake |
| `XO_CORE_API_URL` | no | where `/api/core/v1` goes; default the `xo` address in `src/xo/services.json` (`https://dev.xo.builders`) plus `/api/core/v1`; https only. `/config` reports it as `xo.url` |
| `XO_OAUTH_CLIENT_ID` | for browser login | the client id of the Clerk OAuth application `xo login` signs in with (public, not a secret) |
| `XO_CLERK_FRONTEND_URL` | for browser login | that Clerk instance's Frontend API URL, https only. Browser login is on only when both are set; a missing or bad one turns it off with a line on stderr, never the server |

```sh
xo serve --host 127.0.0.1 --port 8787
```

It listens on 127.0.0.1 by default. Serve it to anyone else only behind HTTPS (a reverse proxy or
a platform that terminates TLS): the CLI refuses plain `http://` except to localhost.

### The proxy in front

The server trusts its proxy to do what a standard-library server cannot do well:

- **Buffer whole requests** before passing them on, and cap connections, so slow clients cannot
  hold the server's threads (it also caps them itself, at `XO_SERVE_MAX_CONNECTIONS`, and closes
  each connection after 30 seconds).
- **Limit requests per client address.** Behind a proxy the server only sees the proxy's address
  and does not read `X-Forwarded-For`, so per-address limits and per-address forensics belong in
  the proxy and its logs.

### Docker

The `Dockerfile` builds the server image: non-root, `xo serve --host 0.0.0.0` inside the
container, and a `HEALTHCHECK` on `/healthz`. Nothing in this repo deploys it.

```sh
docker build -t xo-serve .
docker run --rm -p 127.0.0.1:8787:8787 -e CLERK_SECRET_KEY -e XO_ADMIN_KEY_IDS xo-serve
```

`-e CLERK_SECRET_KEY` with no value passes it from your shell's environment, so it never appears on
the command line. Never bake it into the image (`ARG` or `ENV`) or a file in it.

### Treat this deployment like the Secret Key itself

- The Secret Key can read and change every user, read every API key's secret and delete users.
  Only the people who could hold that key should be able to reach this deployment's settings,
  shell or logs.
- **Rotating the key:** create a new Secret Key in the Clerk Dashboard, set it on the deployment,
  restart, check `/me` answers, then delete the old key. A 503 "xo server misconfigured" with a
  stderr line naming `CLERK_SECRET_KEY` means the key in use was refused.
- **Keep the audit lines.** They go to stderr, which a container loses on restart: ship stderr to
  the platform's log store and keep it as long as you would keep an access log.

The production server runs on Vercel at `https://xo.quirq.dev`: see
[deploy-vercel.md](deploy-vercel.md). There, Vercel is the HTTPS proxy, and the limits in this
process apply per function instance, so the per-address limit is a Vercel Firewall rule.

## Projects

`xo projects` and `xo projects add NAME` write a normal key's own `projects`, and narrowly:

- **The server builds the change.** The client sends only `{"name": "..."}`; any other field, or
  any other shape, is refused with 422 and nothing is written. There is no route through which a
  normal key sends metadata.
- **Only `projects` is written, only on the key's own user** (sharing is the one write to another
  user, and only to their `shared`). The user is the one Clerk ties the
  key to. The server reads the list, appends the name, and sends Clerk's metadata merge
  (`PATCH /v1/users/{id}/metadata`) with `{"private_metadata": {"projects": [...]}}` and nothing else.
  Clerk merges that into what is stored, so every other public and private key, and every other
  field of the user, stays exactly as it was.
- **Refused without a write:** a name that is neither 1 to 64 letters, digits, spaces, `_ . -`
  (starting with a letter or digit) nor a GitHub `OWNER/REPO` (each part within GitHub's own rules,
  not `.`, `..` or ending in `.git`), a name that looks like a credential (`sk-`, `ghp_`, `ak_`,
  `AKIA` and the like, which `/me` would show masked), a name already in the list (ignoring case), a
  list already holding 30 (a user who has more from before this limit keeps them but can add no
  more), a `projects` that exists but is not a list (409: it is never
  overwritten), and a write that would take the user's whole private metadata past 6KB (422).
  Clerk keeps 8KB per bucket and XO's own keys share it, so xo stops at 6KB; a user already past
  6KB cannot add a project until something else is smaller.
- **As atomic as Clerk allows.** Clerk has no transactions or compare-and-swap, and replaces a list
  whole, so an add is read, append, write. On one server instance adds for a user run one at a
  time. Adds on different instances can still race, so after each write the server waits 0.3
  seconds and reads the list again. It answers 200 only when that read holds the new name and every
  name it had read before; anything gone was dropped by a racing write (no route removes a
  project), so it writes the list again with those names put back, up to 3 writes, then answers
  503 asking you to check with `xo projects`. It also stops writing again, with that 503, once a slow
  Clerk has used 10 seconds, so a slow Clerk costs at most one more write and read; at Clerk's
  10-second timeout an add can still take about 40 seconds, and if your client gives up first,
  `xo projects` shows whether the name is there. This relies on Clerk's read returning what was last
  written: if Clerk ever served a stale read, a name added elsewhere in that moment could still be
  dropped. An admin removing a project through `/user-metadata` while an add runs can see it put
  back.
- **Audited** like `/user-metadata`: one JSON line per add with the key id, the caller, the outcome
  and the list's length; never a name or any other value.
- **Limited** to 30 requests a minute per key, and covered by the Firewall rule, since the path
  starts with `/me`. Past Clerk's own limit of 10 metadata writes per 10 seconds per user the
  answer is 429. It clears that user's 60-second `/me` cache.
- **Not narrowed by `XO_ME_PRIVATE_KEYS`.** `/me/projects` always shows the `projects` list, even
  when that variable leaves `projects` out of what `/me` shows.
- **Giving a project its repo.** A project's name is its repo: `OWNER/REPO` for a GitHub project,
  a plain name for one with no repo yet. `POST /me/projects/connect` (`xo connect project:P --repo
  URL`) takes `{"name": "P", "repo": "OWNER/REPO"}` for a plain P and replaces P with `OWNER/REPO`
  in place, through the same engine as an add, writing `projects` and nothing else. Refused without
  a write: P not in the list (404), P already a repo or the repo not `OWNER/REPO` (422), the repo
  already in the list (409), and P in any space (409), since the space would still name it P.
  `spaces` is checked on every read, not only the first: if a racing `xo connect project:P space:S`
  puts P in a space after the rename landed, the rename is put back (P listed again) and the answer
  is 409; if another space came to name `OWNER/REPO` meanwhile, both stay listed. The rename stays
  when the put-back does not settle within the tries or the time limit (503, naming the space), or
  when the racing connect lands after the last read (200). The repo is listed once whatever a
  racing add wrote.
- **A rename can bring the old name back.** Every other write puts back names a racing write
  dropped, and this is the one route that removes a name. A `xo projects add` or `xo create
  project` on another instance that read the list before the rename can therefore put P back next
  to `OWNER/REPO`. Nothing is lost, but P stays listed (xo has no delete); the window is the 0.3
  second settle.

## Spaces

`xo create space`, `xo connect` and `xo list spaces` keep a `spaces` list in the key's own private
metadata, the same way projects are kept:

```
spaces: [{"name": "dev", "kind": "claude-code", "id": "<XO space id>", "rev": 2,
          "projects": [{"quirq-ai/xo": "main"}, {"notes": "v1.2"}]}]
```

- **The server builds every change, and writes only `spaces`.** The client sends a name, a kind,
  an id or a project and ref; never metadata. The merge sent to Clerk names `spaces` and nothing
  else.
- **`POST /me/spaces`** first checks everything recording will need: the name, that the user has
  no space by that name (ignoring case) and fewer than 10, that `spaces` is readable, and that the
  new entry fits in 6KB. Only then does it pass the create's own body to XO's core API with the
  caller's key (`POST /api/core/v1/{kind}`, as `/api/core` does). An XO refusal is passed back and
  nothing is recorded. Once XO answers with the new space's id, the server records `{"name",
  "kind", "id", "rev": 1, "projects": []}`. If recording then fails, the answer says XO created the
  space and gives its id. When the failure was a race, or Clerk being busy, failing or out of
  reach (409, 429, 502 or 503), it also says to run the `xo connect space:NAME --id ID` that
  records it if `xo list spaces` does not show it.
- **`POST /me/spaces/connect`** makes a new space entry, `{"name", "rev": 1, "projects": []}`,
  with nothing created on XO. With an `id` it stands for an XO space the caller already has: the
  server first asks XO for it with the caller's key (`GET /api/core/v1/spaces/{id}`); 403 or 404
  there is a 404 here, and nothing is recorded. An id given for an entry made without one is
  added to that entry (its `rev` goes up by 1); an entry that already has an id keeps it. Any other
  name already taken (ignoring case), or an id already recorded under another name, is refused
  (409).
- **`POST /me/spaces/{name}/projects`** sets `{project: ref}` in that space. The project must be in
  the user's `projects`. One entry per project: connecting it again replaces its ref in place. A
  missing ref is `main`. A space holds at most 10 projects. Each change adds 1 to the space's
  `rev`, the counter `xo update` will use to tell when the remote is ahead.
- **`PUT /me/spaces/{name}`** is what `xo update` and `xo sync` send: `{"rev": N, "projects":
  [{"PROJECT": "REF"}, ...], "base": [...]}`, where `base` is the projects the change starts from.
  It is refused (409, "run xo sync") unless the space is still at `rev` N and still holds `base`.
  Every project must be in the caller's `projects`, each listed once, at most 10, each ref valid;
  the space's other fields stay. The answer is the space at rev N + 1, or unchanged at N when the
  projects equal `base`. Clerk has no compare-and-swap, so this narrows a race without closing it:
  two updates on different instances that both read N within the 0.3-second settle can both write.
  The re-read gives 409 to one whose write did not stick, but if both re-reads land before the
  second write, both get 200 and the later write stands. The other client's next update is then
  refused, since the space no longer holds the `base` it sends. A space's `rev` can also go back
  when a stale write lands outside the settle; `xo sync` then stops, names the projects whose refs
  differ, records nothing and leaves the choice to the caller (`xo install` takes the space's).
- **Names and refs.** A space name is 1 to 64 letters, digits, `_ . -`, starting with a letter or
  digit (it becomes a folder). A ref is a branch, tag or commit sha: letters, digits and `. _ / -`,
  starting with a letter or digit, never holding `..`, `//`, `/.` or `@{`, never ending in `/`, `.`
  or `.lock`. A ref or XO id that looks like a credential (`sk-`, `ghp_`, `ak_` and the like) is
  refused. A ref may start like a token when a branch can (`sk-`, `npm_`, `hf_`, `ghp_` and the
  like) and is refused then only with a token's body: 16 or more letters and digits in a row, or,
  separators left out, 20 or more mixing upper case, lower case and digits. So `npm_bump` and
  `sk-rewrite` are fine; `glpat-`, `AIza`, `ya29.` and `eyJ` are refused on the prefix alone. The CLI checks the same rules before sending, and again before git sees a value.
- **As atomic as Clerk allows.** As for projects: one change per user at a time on an instance, a
  0.3-second settle and a re-read after each write, at most 3 writes, the same 10-second stop, and
  the same 6KB limit. Writing `spaces` replaces the whole list, so a write racing this one can drop
  or revert a space. Every other space is put back at the highest `rev` read, and this one is
  written again if it is missing or back as it was first read. If it holds anything else, someone
  changed it meanwhile: 409, and nothing more is written. A `spaces` that is not a list of objects
  with a `name`, holds two names that differ only in case, or a project entry that is not one
  `{project: ref}`, is never overwritten (409). An admin removing a space through `/user-metadata`
  while a change runs can see it put back.
- **Sharing** is the one write to another user's metadata. `POST /me/share` with `{"item":
  "space:S"|"project:OWNER/REPO", "to": USERNAME}` adds one invite to that user's `shared` list:
  `{"from": <sharer's user id>, "fromName": <sharer's username then>, "item", "projects" (a space's
  GitHub {project: ref} entries; local ones stay the sharer's), "at"}`. Never the space's id, kind
  or rev. The merge sent to Clerk names `shared` only, with the same lock, settle and re-read as
  every other write; each writer re-adds or re-removes only its own entry.
  - **Room.** All of `shared` is at most 1.5KB and one invite at most 1KB, and a share also needs
    the recipient's whole private metadata to stay within 6KB. Invites count toward that 6KB like
    everything else, so the whole never passes it; others can take at most 1.5KB of anyone's
    room, and accepting or rejecting gives it back.
  - **Limits.** 20 invites waiting, 5 from one sender, one per sender and item. Sending the same
    invite again writes nothing; a changed one replaces it only a minute after the last. A reject
    keeps nothing, so the sender can share the same item again straight away.
  - **The same answer.** The caller is told plainly about what is theirs: no username on their
    account (409), an item they do not have (404), a local project or an invite over 1KB (422).
    Everything about the recipient (no such username, the caller's own username, a full list, no
    room, a `shared` xo cannot read, their Clerk write limit) is the same 404 "Could not
    share ITEM with USERNAME", and a Clerk failure is a 503. Once the key is verified, every answer
    takes at least 1.5 seconds. A 200 does confirm the username exists, and a 503 only comes once
    it was found. The audit line names the recipient only by a hash.
  - `GET /me/shared` lists the caller's invites. Each has an `id`: 16 characters of a hash of its
    sender's user id and its exact content, so an invite sent again, or one from another sender
    under the same name, has a different id. `from` is the sender's username now, looked up with
    one Clerk call; `sharedAs` is the name they shared under when it differs, and `senderGone`
    marks an account that no longer exists. If that lookup fails, each shows the name it was
    shared under, marked `senderUnchecked`. `/me` shows `shared` by the names shared under, never
    by user id, and does not look them up: `/me/shared` (`xo list shared`) is the one to trust
    about who sent an invite. Should two invites ever share an id, accept refuses both (409) and reject takes
    both off.
  - `POST /me/shared/accept` takes `{"id", "as"?}` for a shared space and `{"id", "space",
    "ref"?}` for a shared project. It checks every refusal first (an id that matches one invite, a
    usable name, refs and GitHub projects, for a project a space of the caller's with room, at most
    10 spaces, 10 projects a space and 30 projects, 6KB), then makes one write of the caller's own
    `spaces`, `projects` and `shared` (the invite taken off). A shared space becomes a new space
    `{"name", "rev": 1, "projects"}` (`as` when the name is taken). A shared project goes into the
    space named `space` at `ref` (default `main`), added or its ref replaced in place, and that
    space's rev goes up by one. Either way the projects are added to `projects` too, since
    `xo update` and `sync` need every project of a space there. A refused accept writes nothing.
    `xo accept` shows the name and every `project@ref` (for a project, the space it goes in) and
    asks first (`--yes` skips that). `POST /me/shared/reject` with `{"id"}` takes it off and keeps
    nothing else. Neither looks anyone up.
  - **Races.** A share racing the recipient's own write is written again if it was lost. A share
    that read before a reject and writes after it can bring the invite back, once; the reject's
    own re-read then takes it off again, unless both re-reads land first (the same narrowed race as
    `PUT /me/spaces/{name}`). An accept whose new space, or project entry, is lost to a racing
    write of `spaces` puts it back (the invite is already spent), and refuses only when that
    cannot be done: the name was taken meanwhile, or the space is gone or full.
  - **The 6KB check is per instance.** Writes on different instances can each pass it and
    together go over by about one write (the same no compare-and-swap class as `projects`). A
    share adds at most 1.5KB, so the whole stays under Clerk's 8KB. A 422 for 6KB names how much
    the waiting invites take; `xo reject` always works and frees it.
  - **Two Clerk answers to confirm once on a development instance:** `GET /v1/users?username=...`
    and `GET /v1/users?user_id=...&limit=N` are expected to return a bare JSON list. If they do
    not, every share answers 503 and `xo list shared` marks each sender `senderUnchecked`.
- **Masked as `/me` is**, by setting name and by value, except each space's `projects`: its keys
  are project names (a repository called `auth-service` is not a secret), so its refs are shown as
  stored. The `xo` part of a `POST /me/spaces` answer, XO's own answer, is masked too, where
  `xo call` and `xo api` show XO's answer as it came.
- **Audited and limited** like `/me/projects`: one JSON line per change with the key id, the caller,
  the outcome and the list's length (never a name, id, ref or other value), 30 requests a minute
  per key, under the Firewall rule's `/me` prefix.

## Guards

- **Only Clerk-shaped keys reach Clerk.** A bearer token that is not `ak_` followed by up to 200
  letters, digits or `_ . - + / =` (the characters the CLI accepts) gets 401 without a Clerk call.
- **A deadline per connection.** `xo serve` closes a connection 30 seconds after it opens, however
  slowly the client sends, so a trickling client cannot hold one of its slots.
- **A budget for Clerk.** All calls to Clerk share `XO_CLERK_PER_SECOND` (default 20); past it the
  server answers 503 without calling. Clerk allows 1000 Backend API calls per 10 seconds for the
  whole instance, shared with XO's core API, and each verification past 100,000 a month is billed,
  so this cap is what keeps made-up keys sent to `/me` from spending either. It does not cover
  `/api/core`: a request passed on makes no call to this server's Clerk, and goes to XO's platform
  (`XO_CORE_API_URL`, dev.xo.builders by default), where XO checks the key itself, with the same
  Clerk instance as this server. Made-up keys sent there spend that instance outside this cap, from
  the day the forwarding is deployed. What bounds them is `XO_CORE_PER_SECOND` per instance and the Firewall rule per
  address.
- **Rate limits.** Per key and per instance, 60 requests a minute on `/me`, 30 on `/me/projects`,
  30 on `/me/spaces` and its routes (and `/me/shared/accept` and `reject`), 10 on `/me/share`, 30 on
  `/me/shared`, and 20 on
  `/user-metadata`, counted before Clerk is asked; the table remembers the 10,000 most recent keys.
  Clerk also allows 10 metadata writes per 10 seconds per user.
- **Cache.** `/me` answers are cached for 60 seconds per user. Key checks are never cached.
- **Audit log.** Every request to `/user-metadata`, refused and failed ones included (a bad or
  missing key, a rate limit, a wrong method, a body too big), is one JSON line on stderr: time,
  method, outcome, the key's id and the caller (null before the key is verified), the target user,
  and for a PATCH the bucket and key names changed. When Clerk is why a request failed, during the
  key check or after it, `outcome` is the status the caller got (502 or 503 during the key check;
  after it also 404 or 422 when Clerk refused the user or the change) and `clerk` is what Clerk
  said: the status it answered, or `unreachable`, `redirect`, `not JSON`, `not an object`, `not a list` or
  `xo budget` (the server's own Clerk budget). Never values or keys, so a key removed with `null`
  cannot be restored from the log.
- **No secrets in metadata.** Never store API keys, tokens or passwords in private metadata: an
  admin key reads every user's private metadata in full, and each user reads their own through
  `/me` (masked where it looks like a credential, and only the keys in `XO_ME_PRIVATE_KEYS` when
  that is set). Anything kept there on the assumption that the user cannot see it can be seen.

## The admin key

No admin key is issued yet, and nothing in this repo makes one. Until a key has the claim
`xo_admin: true` and its id is in `XO_ADMIN_KEY_IDS`, `/user-metadata` answers 403 to everyone,
and only `/me` is in use.

When admin keys come, keep each one apart from the everyday `XO_API_KEY` (every create on
XO's platform copies the caller's key into the new space), give it a 90-day expiry. xo has no user-metadata command, so send it to `/user-metadata` yourself
(`Authorization: Bearer`), never on a command line.

TODO(suraj): how admin keys are issued, and whether creates on XO's platform should refuse them.

Before relying on admin keys, check once on a Clerk development instance that a key a user makes
for themselves, with `claims` added to the request by hand, comes back with `claims: null`.
