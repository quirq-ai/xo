# Agent guide

Read `README.md` first.

- Every change is a pull request against `main`. It lands only with the `presubmit` check green.
- `src/xo/client.py` is the only CLI code that talks to the network, and `Clerk` in
  `src/xo/server.py` the only server code. Keep it that way.
- The server runs on Vercel (`xo.quirq.dev`) through the root `app.py` (Python pinned in
  `.python-version`); keep it a thin wrapper of `xo.server.wsgi_app()`, and keep
  `docs/deploy-vercel.md` and `docs/test-xo-server.sh` true. Once the Vercel project exists, every
  merge to `main` deploys production at `xo.quirq.dev`: a merge is a deploy, so a change to the
  server is ready only when it is safe to run there.
- The status page (`src/xo/status_page.py`, served at `/`) is public: it may show only a request's
  time, method, route template, status and duration, never a key, key id, user id, path or
  address. Its script and style are pinned by hash in its Content-Security-Policy.
- `CLERK_SECRET_KEY` lives only in the `xo serve` deployment's environment. Never print, log or
  return it; tests assert it never reaches output or the audit log.
- The API key is a secret. Never print, log, commit or write it to a file; tests assert it never
  reaches stdout or stderr. Never follow a redirect with it attached.
- Tests mock the API (`tests/conftest.py`, a server on 127.0.0.1). No test reaches XO.
- Standard library only. A new dependency needs a reason in the PR.
- `src/xo/openapi.json` is the bundled spec of the core API, written from xo-swarm's route and Zod
  schemas. Change it with the xo-swarm commit it follows in the PR. Operations marked
  `x-xo-server` are this repo's own server (`xo serve`) and change with `src/xo/server.py`. Test
  fixtures that are not copies say "SYNTHETIC".
- Mark a decision you cannot make with a one-line `TODO(suraj):`.
- This repo is public: no secrets, tokens or internal hostnames.
