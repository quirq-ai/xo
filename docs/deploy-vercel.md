# Deploy the xo server on Vercel and test it

The xo server (`xo serve`, see [server.md](server.md)) runs on Vercel at `https://xo.quirq.dev`.
Vercel runs the repo's root `app.py`, which serves the same routes as a Python function, on
Python 3.12 (pinned by `.python-version`). Nothing in this repo deploys it: you do, with the steps
below, and then run the test script.

Use **development** credentials only (`sk_test_...`) until the test passes. The Secret Key and the
API key you test with must come from the same Clerk instance: a key from another instance is
refused as "Invalid API key".

## What you need

- A Vercel account on the team that owns `quirq.dev`, and the Vercel CLI (`npm i -g vercel`, then
  `vercel login`).
- From XO's Clerk **development** instance: its Secret Key (`sk_test_...`), and an API key
  (`ak_...`) that belongs to a test user of that instance.
- Python 3.11 or newer and `curl`, for the test script.

## 1. Create the Vercel project

1. **Add New → Project**, import `quirq-ai/xo`, name it `xo`.
2. **Framework Preset:** Other. **Root Directory:** the repo root. Leave the build command, output
   directory and install command empty: Vercel finds `app.py` and its top-level `app`.
3. **Environment Variables:** add exactly one, `CLERK_SECRET_KEY` = the `sk_test_...` key, for the
   **Production** environment only, marked **Sensitive**. Add no admin key ids: with none, the
   metadata routes answer 403 to everyone and only `/me` is in use.
4. Let the first deploy run. It builds `main`, which has no `app.py` until this PR is merged, so it
   fails or serves nothing; step 3 deploys the branch.

## 2. Add xo.quirq.dev

1. **Settings → Domains**, add `xo.quirq.dev` to Production.
2. If `quirq.dev`'s DNS is on Vercel, the record is added for you. Otherwise add the record Vercel
   shows there (a `CNAME` named `xo` pointing to the target Vercel gives) where `quirq.dev`'s DNS
   is hosted.
3. Wait for **Valid Configuration** and the certificate.

## 3. Add the rate limit rule (required)

The server limits calls to Clerk (`XO_CLERK_PER_SECOND`, default 20 a second) and requests per key,
but on Vercel each function instance keeps its own counts, and Vercel can run many instances at
once. So these limits slow a flood down; they do not stop it. The per-address limit has to be
Vercel's:

- **Firewall → Rules → New rule:** if the request path starts with `/me` or `/user-metadata`,
  **Rate Limit**, keyed by **IP**, 60 requests per 60 seconds, then **Deny** (429).

If your plan does not offer rate limit rules, say so before switching to production credentials:
without the rule, anyone can make the server spend XO's Clerk request budget (1000 Backend API
calls per 10 seconds for the whole instance, shared with app.xo.builders) and, past 100,000 a
month, paid key checks.

## 4. Deploy this branch to production (before the merge)

Preview deployments sit behind Vercel's login, so the `xo` command cannot reach them. Deploy the
branch to production from a checkout instead:

```bash
git clone --branch claude/xo-cli-5ynop4 https://github.com/quirq-ai/xo.git xo-deploy
cd xo-deploy
vercel link
vercel --prod
```

`vercel link` asks for the team and the `xo` project. After the PR is merged, pushes to `main`
deploy production by themselves.

## 5. Test

From the same checkout:

```bash
bash docs/test-xo-server.sh
```

It installs `xo` from the checkout into a throwaway virtualenv, checks `https://xo.quirq.dev/healthz`,
asks for the test user's `ak_...` key without echoing it, and checks:

| Check | Expected |
|---|---|
| `/healthz` | `ok` |
| `xo me` with your key | your account: `id`, `username`, `firstName`, `lastName`, `imageUrl`, `createdAt`, nothing else |
| a wrong `ak_` key | 401 |
| a string that is not a Clerk key | 401, without a call to Clerk |
| `xo user-metadata get` with your key | 403 (no admin keys yet) |

It prints PASS or FAIL per line and ends with "All checks passed." The key is never printed, saved
or put on a command line. A `/healthz` FAIL that says "misconfigured" means `CLERK_SECRET_KEY` is
missing or refused; the project's **Logs** say which.

## What is and is not protected on Vercel

| Protected | How |
|---|---|
| The Secret Key | only in Vercel's environment; never printed, logged or returned |
| Who can read or edit metadata | nobody until an admin key exists (403) |
| Junk tokens | refused without a Clerk call |
| Big bodies | over 32KB refused (413) |
| Requests per address | the Firewall rule in step 3 |
| **Not protected** | a flood from many addresses spread over many instances; only Clerk's own limits stop it |

## Logs and audit lines

Every request is one line in the project's **Logs**, and every `/user-metadata` request adds one
JSON audit line (key id, caller, target user, outcome; never a key or a metadata value). Vercel
keeps runtime logs for a short time that depends on the plan; check **Observability** for yours.
To keep audit lines longer, a log drain is needed, which is a paid feature on some plans, so it is
not part of this setup.

## Limits

- Each request makes at most two Clerk calls, each with a 10-second timeout, well inside Vercel's
  function time limit.
- Bodies are capped at 32KB by the server, far below Vercel's request size limit.

## Production credentials, later

Replace `CLERK_SECRET_KEY` with the production key (`sk_live_...`), redeploy, and run the test
script with a real XO key. A user then needs only the `xo` command: `https://xo.quirq.dev` is its
default server.

If the Secret Key leaks or is rotated: create a new one in the Clerk Dashboard, replace
`CLERK_SECRET_KEY` in Vercel, redeploy, run the test script, then delete the old key.
