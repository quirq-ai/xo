# xo server mode

One package, two deployments:

- **The command line** runs on each user's machine with their XO API key (`XO_API_KEY`).
- **The server**, `xo serve`, runs once, on a machine XO controls, with Clerk's Secret Key. It is
  the only place that key exists.

`xo me` and `xo user-metadata` go to the server (`XO_SERVER_URL`). Every other command still goes
to the core API at app.xo.builders, as before.

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
| GET | `/me` | any user key | `id`, `username`, `firstName`, `lastName`, `imageUrl`, `createdAt`; never email or metadata |
| GET | `/user-metadata/{userId}` | admin keys | `publicMetadata`, `privateMetadata` |
| PATCH | `/user-metadata/{userId}` | admin keys | both buckets after a deep merge; `null` removes a key |

Answers use the core API's envelope: `{"success": true, "data": ...}` or
`{"success": false, "error": "..."}`, with 401 (bad key, or a key not tied to a user), 403 (not an
admin key), 404 (no such user), 405, 413 (body over 32KB), 422 (bad body, a key that is not
editable, a bad user id, or over Clerk's 8KB per bucket), 429 (rate limit) and 502 (Clerk did not
answer).

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
| `CLERK_API_URL` | no | default `https://api.clerk.com`; tests point it at a fake |

```sh
xo serve --host 127.0.0.1 --port 8787
```

It listens on 127.0.0.1 by default. Serve it to anyone else only behind HTTPS (a reverse proxy or
a platform that terminates TLS): the CLI refuses plain `http://` except to localhost.

TODO(suraj): where the server is deployed, and its public URL for `XO_SERVER_URL`.

## Guards

- **Rate limits.** Per key, 60 requests a minute on `/me` and 20 on `/user-metadata`, counted
  before Clerk is asked. Clerk allows 1000 Backend API calls per 10 seconds for the whole instance
  and 10 metadata writes per 10 seconds per user.
- **Cache.** `/me` answers are cached for 60 seconds per user. Key checks are never cached.
- **Audit log.** Every `/user-metadata` read and write, refused and failed ones included, is one
  JSON line on stderr: time, method, outcome, the key's id, the caller, the target user, and for a
  PATCH the bucket and key names changed. Never values or keys, so a key removed with `null`
  cannot be restored from the log.
- **No secrets in metadata.** Never store API keys, tokens or passwords in private metadata: an
  admin key reads every user's private metadata in full.

## The admin key

An admin key is minted by whoever holds the Secret Key, for example with Clerk's backend SDK:

```ts
clerkClient.apiKeys.create({ subject: "<your user id>", name: "xo admin", createdBy: "<your user id>",
  secondsUntilExpiration: 7776000, claims: { issued_by: "xo", xo_admin: true } })
```

Then add its id to `XO_ADMIN_KEY_IDS`. Keep it separate from your everyday `XO_API_KEY` (every
create copies the caller's key into the new space): give it only to `xo user-metadata`, with
`--api-key -`. It expires after 90 days; revoke it at once if it leaks.

TODO(suraj): who mints the admin key, and whether creates on app.xo.builders should refuse it.

Before relying on admin keys, check once on a Clerk development instance that a key a user makes
for themselves, with `claims` added to the request by hand, comes back with `claims: null`.
