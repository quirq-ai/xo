# xo server mode

One package, two deployments:

- **The command line** runs on each user's machine with their XO API key (`XO_API_KEY`).
- **The server**, `xo serve`, runs once, on a machine XO controls, with Clerk's Secret Key. It is
  the only place that key exists.

`xo me` and `xo user-metadata` go to the server (`https://xo.quirq.dev`, or `XO_SERVER_URL`).
Every other command still goes to the core API at app.xo.builders, as before.

```
xo me ──────────────┐                      ┌── POST /v1/api_keys/verify
xo user-metadata ───┼─ XO_API_KEY ─► xo serve ─ CLERK_SECRET_KEY ─► api.clerk.com
                    │                      └── GET /v1/users/{id}, PATCH /v1/users/{id}/metadata
xo codex, ... ──────┴─ XO_API_KEY ─► app.xo.builders (unchanged)
```

## Routes

| Method | Path | Who | Returns |
|---|---|---|---|
| GET | `/healthz` | anyone | `{"status": "ok"}`, with no Clerk call |
| GET | `/` | anyone | the status page: health, counts and the latest requests, refreshed every 5 seconds |
| GET | `/status` | anyone | what the page shows, as JSON; no Clerk call |
| GET | `/me` | any user key | `id`, `username`, `firstName`, `lastName`, `imageUrl`, `createdAt`; never email or metadata |
| GET | `/user-metadata/{userId}` | admin keys | `publicMetadata`, `privateMetadata` |
| PATCH | `/user-metadata/{userId}` | admin keys | both buckets after a deep merge; `null` removes a key |

Answers use the core API's envelope: `{"success": true, "data": ...}` or
`{"success": false, "error": "..."}`, with 401 (bad key, or a key not tied to a user), 403 (not an
admin key), 404 (no such user), 405, 413 (body over 32KB), 422 (bad body, a key that is not
editable, a bad user id, or over Clerk's 8KB per bucket), 429 (this key's rate limit), 502 (Clerk
did not answer) and 503 (the server is busy, or misconfigured: Clerk refused its Secret Key, which
it also says in one line on stderr).

## The status page

`/` shows whether the server answers, how many requests and `/healthz` pings it has served, how
many were refused or failed, and the last 100 requests. Each entry is only the time, method,
route (`/me`, `/user-metadata/{userId}`, `/healthz` or `other`), status and duration: never a key,
key id, user id, path or address, so it is public. The page and `/status` are not counted. The
counts live in the process's memory: on Vercel each instance keeps its own and starts empty, so the
page shows what the instance answering you has seen. The audit log stays in the logs only.

## Who can do what

- **Any user key** can read its own account through `/me`. The key is verified with Clerk on every
  request and never cached, so a revoked key stops at once.
- **An admin key** is a Clerk API key that has the claim `xo_admin: true` **and** whose id is in
  `XO_ADMIN_KEY_IDS`. A key with the claim but not on the list is refused, and the refusal is
  logged with `claimedAdmin: true`, since that is what a forged key would look like.
- **Only allow-listed keys change.** `PATCH` may only touch the top-level keys named in
  `XO_EDITABLE_PUBLIC_KEYS` and `XO_EDITABLE_PRIVATE_KEYS`. Both start empty, so nothing is
  editable until you name keys. Reads return both whole buckets.
- Auth runs first, so a caller without an admin key learns nothing about bodies or users.

## Configuration

Environment only, so the secret never lands in shell history or the process list.

| Variable | Needed | What |
|---|---|---|
| `CLERK_SECRET_KEY` | yes | Clerk's Secret Key (`sk_live_...`); never printed or logged |
| `XO_ADMIN_KEY_IDS` | for metadata | comma-separated Clerk API key ids (`ak_...`) that may act as admin |
| `XO_EDITABLE_PUBLIC_KEYS` | for edits | comma-separated top-level public metadata keys PATCH may change |
| `XO_EDITABLE_PRIVATE_KEYS` | for edits | the same for private metadata |
| `XO_CLERK_PER_SECOND` | no | calls to Clerk per second for the whole process; default 20 |
| `XO_SERVE_MAX_CONNECTIONS` | no | connections served at once; default 64, then 503 |
| `CLERK_API_URL` | no | default `https://api.clerk.com`; tests point it at a fake |

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

## Guards

- **Only Clerk-shaped keys reach Clerk.** A bearer token that is not `ak_` followed by up to 200
  letters, digits or `_ . - + / =` (the characters the CLI accepts) gets 401 without a Clerk call.
- **A deadline per connection.** `xo serve` closes a connection 30 seconds after it opens, however
  slowly the client sends, so a trickling client cannot hold one of its slots.
- **A budget for Clerk.** All calls to Clerk share `XO_CLERK_PER_SECOND` (default 20); past it the
  server answers 503 without calling. Clerk allows 1000 Backend API calls per 10 seconds for the
  whole instance, shared with XO's core API, and each verification past 100,000 a month is billed,
  so this cap is what keeps made-up keys from spending either.
- **Rate limits.** Per key, 60 requests a minute on `/me` and 20 on `/user-metadata`, counted
  before Clerk is asked; the table remembers the 10,000 most recent keys. Clerk also allows 10
  metadata writes per 10 seconds per user.
- **Cache.** `/me` answers are cached for 60 seconds per user. Key checks are never cached.
- **Audit log.** Every request to `/user-metadata`, refused and failed ones included (a bad or
  missing key, a rate limit, a wrong method, a body too big), is one JSON line on stderr: time,
  method, outcome, the key's id and the caller (null before the key is verified), the target user,
  and for a PATCH the bucket and key names changed. Never values or keys, so a key removed with `null`
  cannot be restored from the log.
- **No secrets in metadata.** Never store API keys, tokens or passwords in private metadata: an
  admin key reads every user's private metadata in full.

## The admin key

No admin key is issued yet, and nothing in this repo makes one. Until a key has the claim
`xo_admin: true` and its id is in `XO_ADMIN_KEY_IDS`, `/user-metadata` answers 403 to everyone,
and only `/me` is in use.

When admin keys come, keep each one apart from the everyday `XO_API_KEY` (every create on
app.xo.builders copies the caller's key into the new space), give it a 90-day expiry, and pass it to
`xo user-metadata` with `--api-key -`.

TODO(suraj): how admin keys are issued, and whether creates on app.xo.builders should refuse them.

Before relying on admin keys, check once on a Clerk development instance that a key a user makes
for themselves, with `claims` added to the request by hand, comes back with `claims: null`.
