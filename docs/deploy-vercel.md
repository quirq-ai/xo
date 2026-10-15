# Deploy the xo server on Vercel and test it

The xo server (`xo serve`, see [server.md](server.md)) runs on Vercel at `https://xo.quirq.dev`.
Vercel runs the repo's root `app.py`, which serves the same routes as a Python function, on
Python 3.12 (pinned by `.python-version`). Nothing in this repo deploys it: you do, with the steps
below, and then run the test script.

Use **development** credentials only (`sk_test_...`) until the test passes and the production gate
at the end is passed. Until then, `xo.quirq.dev` answers 401 to real XO keys, although `xo`
already uses it by default. The Secret Key and the API key you test with must come from the same
Clerk instance: a key from another instance is refused as "Invalid API key".

## What you need

- A Vercel account on the team that owns `quirq.dev`, and the Vercel CLI (`npm i -g vercel`, then
  `vercel login`).
- From XO's Clerk **development** instance: its Secret Key (`sk_test_...`), and an API key
  (`ak_...`) that belongs to a test user of that instance.
- Python 3.11 or newer and `curl`, for the test script.
- **The commit to deploy**: the full 40-character sha Claude gives you with these steps. It is the
  commit that was audited; never deploy a branch head you were not given.

## 1. Create the Vercel project

1. **Add New → Project**, import `quirq-ai/xo`, name it `xo`.
2. **Framework Preset:** **Python** (Vercel should pick it on its own from `pyproject.toml`; choose
   it if not). **Root Directory:** the repo root. Leave the preset's build, output and install
   settings as they are. The Python preset is what runs `app.py`: it routes every path to its
   top-level `app`. With "Other", no function runs and `/healthz` fails.
3. **Environment Variables:** add exactly one, `CLERK_SECRET_KEY` = the `sk_test_...` key, for the
   **Production** environment only, marked **Sensitive**. Add no admin key ids: with none, the
   metadata routes answer 403 to everyone and only `/me` is in use.
4. Let the first deploy run. It builds `main`, which has no `app.py` until this PR is merged, so it
   fails or serves nothing; step 4 deploys the audited commit.

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

1. **Firewall → Rules → New rule**, named `xo per-IP limit`:
   - **If** Request Path **starts with** `/me`, **or** Request Path **starts with** `/user-metadata`;
   - **Then** **Rate Limit**: fixed window, **60 requests per 60 seconds**, keyed by **IP**;
   - past the limit: the default action (`rate_limit`, "too many requests"). Deny also works.
2. **Save**, then **Review Changes → Publish**. A rule is a draft and does nothing until it is
   published. The test script's last check sends 70 requests and fails if the rule is not active.

The same rule from the Vercel CLI, run inside the linked checkout of step 4 (taken from the CLI's
source and not run by Claude; the dashboard steps above are enough):

```bash
vercel firewall rules add "xo per-IP limit" \
  --condition '{"type":"path","op":"pre","value":"/me"}' --or \
  --condition '{"type":"path","op":"pre","value":"/user-metadata"}' \
  --action rate_limit --rate-limit-window 60 --rate-limit-requests 60 \
  --rate-limit-keys ip --rate-limit-action rate_limit --yes
vercel firewall publish
```

If your plan does not offer rate limit rules, stop and tell Claude: without the rule, anyone can
make the server spend Clerk requests (1000 Backend API calls per 10 seconds for the whole instance)
and, past 100,000 a month, paid key checks. On development credentials that is the development
instance only.

## 4. Deploy the audited commit to production (before the merge)

Preview deployments sit behind Vercel's login, so the `xo` command cannot reach them. Deploy the
commit you were given to production from a checkout. Paste its full sha into the first line:

```bash
SHA=paste-the-40-character-sha-here
git clone https://github.com/quirq-ai/xo.git xo-deploy
cd xo-deploy
git checkout --detach "$SHA"
[ "$(git rev-parse HEAD)" = "$SHA" ] && echo "OK: at $SHA" || echo "STOP: not at the audited commit"
```

Go on only if it printed `OK`. Then:

```bash
vercel link
[ "$(git rev-parse HEAD)" = "$SHA" ] && vercel --prod
```

`vercel link` asks for the team and the `xo` project. The second line deploys only if the checkout
is still at the audited commit. `.vercelignore` keeps `.env` files and the tests out of the upload.

After the PR is merged, every merge to `main` deploys production by itself.

## 5. Test

From the same checkout (the one at the audited commit):

```bash
bash docs/test-xo-server.sh
```

It runs `xo` straight from the checkout (nothing is installed or downloaded, and it calls only
`xo.quirq.dev`), checks `/healthz`, asks for the test user's `ak_...` key without echoing it, and
checks:

| Check | Expected |
|---|---|
| `/healthz` | `ok` |
| your key's format | `ak_`, then letters, digits and `_ . - + / =` only (checked without showing it) |
| `xo me` with your key | your account: `id`, `username`, `firstName`, `lastName`, `imageUrl`, `createdAt`, nothing else |
| a wrong `ak_` key | 401 |
| a string that is not a Clerk key | 401, without a call to Clerk |
| `xo user-metadata get` with your key | 403 (no admin keys yet) |
| 70 requests to `/me` with no key | refused by the Firewall rule after about 60 (this holds your address back for up to a minute) |

It prints PASS or FAIL per line and ends with "All checks passed." The key is never printed, saved,
put on a command line or exported; only the `xo` calls that need it get it. If the format check
fails, tell Claude which kinds of characters the key has (never the key): xo and the server would
need to accept them. A `/healthz` FAIL that says "misconfigured" means `CLERK_SECRET_KEY` is
missing or refused; the project's **Logs** say which.

## What is and is not protected on Vercel

| Protected | How |
|---|---|
| The Secret Key | only in Vercel's environment; never printed, logged or returned |
| Who can read or edit metadata | nobody until an admin key exists (403) |
| Junk tokens | refused without a Clerk call |
| Big bodies | over 32KB refused (413) |
| Requests per address | the Firewall rule in step 3, once published |
| **Not protected** | a flood from many addresses spread over many instances; only Clerk's own limits stop it |
| **Not rate limited** | `/` (the status page), `/status` and `/healthz`: public, one function invocation each, never a Clerk call. An open status page tab polls `/status` every 5 seconds while it is visible (about 17,000 invocations a day per tab left open) and stops while hidden. To cap them too, add `/status` to the Firewall rule in step 3; the page stays well under 60 a minute. |

## Logs and audit lines

Every request is one line in the project's **Logs**, and every `/user-metadata` request adds one
JSON audit line (key id, caller, target user, outcome; never a key or a metadata value). Vercel
keeps runtime logs for a short time that depends on the plan; check **Observability** for yours.
To keep audit lines longer, a log drain is needed, which is a paid feature on some plans, so it is
not part of this setup.

## Limits

- Each request makes at most two Clerk calls, each with a 10-second timeout. Vercel's function time
  and request size limits are not checked here; 20 seconds and 32KB (the server's body cap) are
  small, but confirm them for your plan.

## Before production credentials (a gate)

Do not switch to production credentials until all of these hold:

1. The test script passed on development credentials, including the Firewall check.
2. **A global limit, or the risk accepted in writing.** The per-IP rule does not stop a flood
   spread over many addresses, and the server's own Clerk budget is per instance. Either add a
   limit that counts all requests together (one option is a second rate limit rule on the same
   paths keyed by the `host` header, since every request has the same host; whether Vercel counts
   it globally is not verified), or write down that XO accepts the risk: such a flood could use up
   XO's production Clerk instance's Backend API limit, which app.xo.builders shares, and pay for key
   checks past 100,000 a month. TODO(suraj): choose one.

Then replace `CLERK_SECRET_KEY` with the production key (`sk_live_...`), redeploy, and run the test
script with a real XO key. A user then needs only the `xo` command: `https://xo.quirq.dev` is its
default server.

## Rotating the Secret Key

If the Secret Key leaks or is rotated: create a new one in the Clerk Dashboard, replace
`CLERK_SECRET_KEY` in Vercel, redeploy, run the test script, then delete the old key.
