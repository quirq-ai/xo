# Agent guide

Read `README.md` first.

- Every change is a pull request against `main`. It lands only with the `presubmit` check green.
- `src/xo/client.py` is the only code that talks to the network. Keep it that way.
- The API key is a secret. Never print, log, commit or write it to a file; tests assert it never
  reaches stdout or stderr. Never follow a redirect with it attached.
- Tests mock the API (`tests/conftest.py`, a server on 127.0.0.1). No test reaches XO.
- Standard library only. A new dependency needs a reason in the PR.
- A spec copied from XO goes in `src/xo/openapi.json`, with the date and source in the PR. Test
  fixtures that are not copies say "SYNTHETIC".
- Mark a decision you cannot make with a one-line `TODO(suraj):`.
- This repo is public: no secrets, tokens or internal hostnames.
