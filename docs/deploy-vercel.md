# Deploy the xo server on Vercel and test it

The xo server (`xo serve`, see [server.md](server.md)) runs on Vercel at `https://xo.quirq.dev`.
Vercel runs the repo's root `app.py`, which serves the same routes as a Python function, on
Python 3.12 (pinned by `.python-version`). Vercel's GitHub integration deploys it: every commit
on `main` goes to production by itself, so every merge to `main` ships `xo.quirq.dev`. Nothing in
this repo deploys it, and the steps below are yours, followed by the test script.

Use **development** credentials only (`sk_test_...`) until the test passes and the production gate
at the end is passed. Until then, `xo.quirq.dev` answers `xo me` with 401 to real XO keys, although
`xo` already uses it by default; the creates and `xo spaces --space-id ID` work, since XO checks those keys
itself. The Secret Key and the API key you test with must come from the same
Clerk instance: a key from another instance is refused as "Invalid API key".

## What you need

- A Vercel account on the team that owns `quirq.dev`, and the Vercel CLI (`npm i -g vercel`, then
  `vercel login`).
- From XO's Clerk **development** instance: its Secret Key (`sk_test_...`), and an API key
  (`ak_...`) that belongs to a test user of that instance.
- Python 3.11 or newer and `curl`, for the test script.

## 1. Create the Vercel project

1. **Add New → Project**, import `quirq-ai/xo`, name it `xo`.
2. **Framework Preset:** **Python** (Vercel should pick it on its own from `pyproject.toml`; choose
   it if not). **Root Directory:** the repo root. Leave the preset's build, output and install
   settings as they are. The Python preset is what runs `app.py`: it routes every path to its
   top-level `app`. With "Other", no function runs and `/healthz` fails.
3. **Environment Variables:** add exactly one, `CLERK_SECRET_KEY` = the `sk_test_...` key, for the
   **Production** environment only, marked **Sensitive**. Add no admin key ids: with none, the
   metadata routes answer 403 to everyone and only `/me` is in use. Without `XO_CORE_API_URL`,
   creates go to XO's platform at `https://dev.xo.builders` (it was `https://app.xo.builders` before
   this version); add `XO_CORE_API_URL` only to send them somewhere else.
4. Leave the **Production Branch** at `main` (**Settings → Git**) and the GitHub integration on.
   The first deploy builds `main`.

## 2. Add xo.quirq.dev

1. **Settings → Domains**, add `xo.quirq.dev` to Production.
2. If `quirq.dev`'s DNS is on Vercel, the record is added for you. Otherwise add the record Vercel
   shows there (a `CNAME` named `xo` pointing to the target Vercel gives) where `quirq.dev`'s DNS
   is hosted.
3. Wait for **Valid Configuration** and the certificate.

## 3. Add the rate limit rule (recommended, where your plan has it)

The server limits calls to Clerk (`XO_CLERK_PER_SECOND`, default 20 a second), requests passed on
to XO (`XO_CORE_PER_SECOND`, default 10 a second) and requests per key, but on Vercel each function
instance keeps its own counts, and Vercel can run many instances at once. So these limits slow a
flood down; they do not stop it. A per-address limit can only be Vercel's. Add it if your plan
offers rate limit rules under **Firewall**; without it the server's own limits are all there is,
and the test script's last check fails:

1. **Firewall → Rules → New rule**, named `xo per-IP limit`:
   - **If** Request Path **starts with** `/me`, **or** Request Path **starts with** `/user-metadata`,
     **or** Request Path **starts with** `/api/core`;
   - **Then** **Rate Limit**: fixed window, **60 requests per 60 seconds**, keyed by **IP**;
   - past the limit: the default action (`rate_limit`, "too many requests"). Deny also works.
2. **Save**, then **Review Changes → Publish**. A rule is a draft and does nothing until it is
   published. The test script's last check sends up to 130 requests and fails if the rule is not
   active.

The same rule from the Vercel CLI, run inside a checkout linked with `vercel link` (taken from the CLI's
source and not run by Claude; the dashboard steps above are enough):

```bash
vercel firewall rules add "xo per-IP limit" \
  --condition '{"type":"path","op":"pre","value":"/me"}' --or \
  --condition '{"type":"path","op":"pre","value":"/user-metadata"}' --or \
  --condition '{"type":"path","op":"pre","value":"/api/core"}' \
  --action rate_limit --rate-limit-window 60 --rate-limit-requests 60 \
  --rate-limit-keys ip --rate-limit-action rate_limit --yes
vercel firewall publish
```

If the rule already exists without `/api/core`, edit it to add that condition and publish again:
the server passes anything under `/api/core` that looks like an API key on to XO, so without it one
address can send XO as many requests as the per-key limit allows for each made-up key.

If your plan does not offer rate limit rules, tell Claude before you go on: without the rule,
anyone can make the server spend Clerk requests (1000 Backend API calls per 10 seconds for the whole instance)
and, past 100,000 a month, paid key checks. Through `/me` that is the server's own Clerk instance,
the development one on development credentials. Through `/api/core` it is XO's own
instance whatever credentials the server holds: a request there goes on to XO's platform
(`XO_CORE_API_URL`, dev.xo.builders by default), where XO checks the key with its own Clerk. So the `/api/core` condition matters from the day the
forwarding is deployed, not only once the server holds production credentials.

## 4. How it deploys

- **Production:** every commit on `main` (each merged pull request) deploys to production and
  `xo.quirq.dev` by itself. A merge is a release: merge only what was reviewed.
- **Previews:** every other branch and pull request gets a preview deployment. Previews have no
  `CLERK_SECRET_KEY` (it is set for Production only), so they answer 503 "xo server
  misconfigured". That is expected, and it keeps the Secret Key out of any branch's build.
- **Rolling back:** in **Deployments**, pick the last good production deployment and choose
  **Instant Rollback**, then fix `main`. Vercel's documentation says a rollback turns off the
  automatic assignment of production domains: later merges to `main` still build, but they do not
  reach `xo.quirq.dev`. Once the fix is merged and its deployment is **Ready**, **Promote** it to
  production to turn automatic deploys back on, then run the test script. Not **Undo Rollback**:
  that puts back the deployment you rolled back from, the broken one. Also from the
  documentation: on the Hobby plan, Instant Rollback can go back only to the previous production
  deployment.
- `.vercelignore` keeps `.env` files and the tests out of every upload.

## 5. Test

From a checkout at the commit that is deployed: its sha is on the production deployment in
**Deployments**. Paste it into the first line:

```bash
SHA=paste-the-deployed-commit-sha-here
git clone https://github.com/quirq-ai/xo.git && cd xo && git checkout --detach "$SHA" &&
  bash docs/test-xo-server.sh
```

The script runs only if the checkout worked, so a sha left as the placeholder stops it.

It runs `xo` straight from the checkout (nothing is installed or downloaded, and it calls only
`xo.quirq.dev`, which passes one request with a made-up key on to XO), checks `/healthz`, asks for the test user's `ak_...` key without echoing it, and
checks:

| Check | Expected |
|---|---|
| `/healthz` | `ok` |
| your key's format | `ak_`, then letters, digits and `_ . - + / =` only (checked without showing it; anything else stops the script before the key is sent) |
| `xo me` with your key | your profile under `xo`: `id`, `username`, `firstName`, `lastName`, `imageUrl`, `createdAt`, `email`, `public_data`, `private_data`, nothing else |
| `xo spaces --space-id ID` with a made-up key | XO's 401 (or its 404 "Space not found"), passed back by the server; your key is not used. A bare "Not found" means the deployment is older than the forwarding |
| a wrong `ak_` key | 401 |
| a string that is not a Clerk key | 401, without a call to Clerk |
| `/user-metadata/user_test` with your key | 403 (no admin keys yet) |
| up to 130 requests to `/me` with no key | refused (429, or 403 for a Deny rule) after about 60, or up to 120 if the rule's minute ends mid-burst; any other answer is a FAIL. This holds your address back for up to a minute |
| then one request to `/api/core/v1/spaces/0000…` with no key | 429 or 403: the same rule covers `/api/core`. A 401 means the `/api/core` condition is missing or not published. This assumes Vercel counts one rule's paths together for an address, which has not been checked against Vercel |

It prints PASS or FAIL per line and ends with "All checks passed." The key is never printed, saved,
put on a command line or exported; only the `xo` calls that need it get it. If the format check
fails, tell Claude which kinds of characters the key has (never the key): xo and the server would
need to accept them. A `/healthz` FAIL that says "misconfigured" means `CLERK_SECRET_KEY` is
missing or refused; the project's **Logs** say which.

## 6. Browser login (optional; only once keys from this instance work with XO)

Until both settings below are set, `xo login` asks for a key as it always has, and `GET /login`
answers `{"browser": null}`. To let users sign in in a browser instead (see docs/server.md,
"Browser login"), first check this:

**Browser login gives each user an API key from the Clerk instance this deployment's
`CLERK_SECRET_KEY` belongs to.** XO's platform checks keys with its own Clerk (step 3), so such a
key works with XO only if that is the same instance. While the server holds development
credentials it is not: XO checks keys with its production instance, so every browser login would
end with XO refusing the key (401) and nothing saved. So do this section only after the production
gate at the end, and only once XO has confirmed that its platform checks API keys with that same
production instance. TODO(suraj): confirm this with XO before step 5.

In the Clerk Dashboard (dashboard.clerk.com), with that instance chosen at the top:

1. **API keys must be on.** Open <https://dashboard.clerk.com/~/platform/api-keys>. If it offers
   **Enable API keys**, select it, choose **Enable User API keys**, then **Enable**. If it already
   lists keys, they are on. Clerk bills each new key past 1,000 a month ($0.001 each), and the
   Hobby plan stops at 1,000; browser login makes an account at most 5 a day.
2. **Add the OAuth application.** Open <https://dashboard.clerk.com/~/oauth-applications> and
   select **Add application** → **Create OAuth application**. **Name:** `xo CLI`. **Scopes:**
   `openid`, `profile` and `email`. Select **Add**. If a **Client Secret** is shown, xo does not use
   it: close it without copying it anywhere.
3. **On the application's page that opens:**
   - turn on **Public** (xo has no client secret; it proves itself with PKCE instead), and turn on
     **Require PKCE**. Without **Public**, every browser login fails, and the project's **Logs** say
     `Clerk refused the OAuth application`;
   - leave the consent screen on (it is on by default): without it, someone signed in to XO who
     opens a sign-in link someone else made is not asked first;
   - in **Redirect URIs**, add these three, one at a time, exactly as written (Clerk compares them
     exactly: `http`, `127.0.0.1` and not `localhost`, the port, and no `/` at the end):
     `http://127.0.0.1:8976/callback`, `http://127.0.0.1:8977/callback`,
     `http://127.0.0.1:8978/callback`;
   - under **Application credentials**, copy the **Client ID**.
4. **Copy the Frontend API URL** from <https://dashboard.clerk.com/~/api-keys> (the page with the
   Publishable Key, not the one in step 1). It is an address only, such as
   `https://clerk.example.com` for a production instance (a development one ends in
   `.clerk.accounts.dev`): copy nothing after the host.

Then in Vercel:

5. **Settings → Environment Variables**, for **Production**: add `XO_OAUTH_CLIENT_ID` (the Client
   ID) and `XO_CLERK_FRONTEND_URL` (the Frontend API URL). Neither is a secret. Then
   **Deployments**, the latest production deployment, **⋯ → Redeploy**, so the new values are used.
6. **Cap it per address.** `/login/browser` needs no key and each request makes a call to Clerk,
   and the server's own cap (2 a second) is per instance. In **Firewall → Rules**, open
   `xo per-IP limit` from step 3, add **or** Request Path **starts with** `/login`, **Save**, then
   **Review Changes → Publish**. A login sends four requests that this rule counts.
7. **Check.** `curl -s https://xo.quirq.dev/login` shows the sign-in address and client id under
   `browser`. If it still says `null`, the project's **Logs** have a line starting `xo serve:
   browser login is off:` that says why. Then run `xo login` on a computer with a browser: sign in,
   allow `xo CLI` on the consent screen, and it ends with `logged in as ...; a new key that works
   for 90 days and URL saved to ...`.

To turn it off, delete either variable and redeploy. Keys made by browser login are named
`xo CLI on HOST`, described `made by xo login in a browser`, and expire after 90 days. A later
browser login for the same account hands back the newest of those while it has a week or more
left (when Clerk will read its secret back), and never a key made any other way; otherwise it makes
a new one. Revoke a key in the Clerk Dashboard.

## What is and is not protected on Vercel

| Protected | How |
|---|---|
| The Secret Key | only in Vercel's environment; never printed, logged or returned |
| Who can read or edit metadata | nobody until an admin key exists (403) |
| Tokens that are not `ak_` keys | refused without a Clerk call, and never passed on to XO. A made-up `ak_` token is passed on: XO refuses it, but only the limits below bound how many reach XO |
| What reaches XO | only the creates and `GET /spaces/{id}` under `/api/core/v1`, with an `ak_` key; anything else there is a 404 from the server |
| Big bodies | over 32KB refused (413) |
| Requests per address | the Firewall rule in step 3, once published |
| Requests to XO per instance | `XO_CORE_PER_SECOND` (default 10 a second), then 503 |
| **Not protected** | a flood from many addresses spread over many instances; only Clerk's and XO's own limits stop it |
| **Changed for XO** | every xo user now reaches XO's platform from Vercel's addresses, not their own, so a per-address limit on XO's side would count all of them together |
| **Not rate limited** | `/login` and `/login/browser` until step 6 adds `/login` to the rule in step 3 (the server caps swaps at 2 a second per instance), `/config` (the services list `xo login` reads; static, never a Clerk call; do not add it to the rule in step 3, for the same reason as `/status`), `/` (the status page), `/status` and `/healthz`: public, one function invocation each, never a Clerk call. An open status page tab polls `/status` every 5 seconds while it is visible (about 17,000 invocations a day per tab left open) and stops while hidden. To cap them too, add a second rule of its own (Request Path **starts with** `/status`, 60 requests per 60 seconds per IP, so `/status/` is capped too); the page stays well under that. Do not add `/status` to the rule in step 3: it would share one per-address budget with `/me`, and an open status tab could then make the test script's Firewall check fail. |

## Logs and audit lines

Every request is one line in the project's **Logs**, and every `/user-metadata` request adds one
JSON audit line (key id, caller, target user, outcome; never a key or a metadata value). Vercel
keeps runtime logs for a short time that depends on the plan; check **Observability** for yours.
To keep audit lines longer, a log drain is needed, which is a paid feature on some plans, so it is
not part of this setup.

## Limits

- Each request makes at most two Clerk calls, each with a 10-second timeout, except these: a share
  makes up to ten (the key, the sharer, the lookup, and up to four reads and three writes of the
  recipient), an accept or reject up to nine, `GET /me/shared` three, a `/me/projects` or
  `/me/spaces` write up to eight (the key, then up to four reads and three writes), and a browser
  login five (the code swap and who signed in, at Clerk's Frontend API, then the user's keys, one
  key's secret and a new key); or one call to XO
  that gets 25 seconds in all, however slowly XO sends (a create that takes longer answers 504 and
  may still have happened). Vercel's
  function time and request size limits are not checked here; 25 seconds and 32KB (the server's
  body cap) are small, but confirm them for your plan.

## Before production credentials (a gate)

Do not switch to production credentials until all of these hold:

1. The test script passed on development credentials, including the Firewall check where your plan
   has the rule.
2. **A global limit, or the risk accepted in writing.** The per-IP rule does not stop a flood
   spread over many addresses, and the server's own budgets (Clerk's, and `XO_CORE_PER_SECOND` for
   requests passed on to XO) are per instance. This is not only about this switch: requests to
   `/api/core` already reach XO's production Clerk on development credentials. Either add a
   limit that counts all requests together (one option is a second rate limit rule on the same
   paths keyed by the `host` header, since every request has the same host; whether Vercel counts
   it globally is not verified), or write down that XO accepts the risk: such a flood could use up
   the Backend API limit of XO's Clerk instance, which XO's platform shares, and pay for key
   checks past 100,000 a month. TODO(suraj): choose one.

Then replace `CLERK_SECRET_KEY` with the production key (`sk_live_...`), redeploy, and run the test
script with a real XO key. A user then needs only the `xo` command: `https://xo.quirq.dev` is its
default server.

## Rotating the Secret Key

If the Secret Key leaks or is rotated: create a new one in the Clerk Dashboard, replace
`CLERK_SECRET_KEY` in Vercel, redeploy, run the test script, then delete the old key.
