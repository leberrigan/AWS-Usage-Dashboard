# Migrating a tool onto centralized user management

Hand this file to an agent working inside a **gated tool's own repo**
(burst-finder, aws-costs, artifacts, asset-management, or a future one) to
move that tool from its own local allowlist/role store onto the shared
Motus AWS host's central User Management service. The agent won't have
the rest of the Motus AWS ops repo's context, so this file is written to
stand alone — read it in full before changing anything.

## What you're changing, and what you're not

This host (`motusaws.duckdns.org`) already has one outer login gate for
every tool: oauth2-proxy, sitting in front of nginx, doing GitHub OAuth.
**You are not touching that.** By the time a request reaches your app, the
person is already signed in and nginx has set an `X-Auth-Request-Email`
header with their address — trust it, don't re-verify it.

What you *are* changing: the **inner** decision your app currently makes
for itself — "is this email allowed to use *this* tool, and with what
role" — which today probably lives in one of:

- a flat `allowlist.json` your backend reads/writes (`{"email": "role"}`)
- your own Baserow table, queried directly for a role/active field
- something else tool-specific

After this migration, that decision is delegated to a shared service
instead, so granting or revoking someone's access to your tool becomes a
one-line edit in a shared spreadsheet-like table instead of a deploy.

## The contract

The central service runs on the same box, loopback-only, at
`127.0.0.1:5004`. It is **not** reachable from the public internet and has
no nginx location — call it directly from your backend, server-to-server:

```
GET http://127.0.0.1:5004/internal/check?email=<email>&tool=<your-slug>

→ 200 {"allowed": true,  "role": "admin", "name": "Jane Doe"}
→ 200 {"allowed": false, "role": null,    "name": null}
```

`<your-slug>` is your tool's kebab-case slug, matching your repo name and
whatever's already in the main ops repo's `manifest.json` /
`infra/nginx/tools.conf` for you (e.g. `burst-finder`, `aws-costs`).

`role` is whatever string was put in Baserow for this tool — there's no
fixed enum, it's whatever your app already treats as meaningful (`"admin"`
/ `"user"`, or your own set). If your app only ever needed a yes/no (no
role distinction), just check `allowed` and ignore `role`.

There's also `GET /internal/user?email=<email>` → `{"active": bool, "name":
str, "tools": {"<slug>": "<role>", ...}}` if you want the full picture
(every tool this person has access to) rather than a single check — most
tools won't need this.

**No auth is required on the call itself** — it's a loopback call between
trusted backends on the same box, same trust model as everything else
here. If the service is unreachable or errors, **fail closed**: treat it
the same as `allowed: false` (deny / 403), don't silently let requests
through. It has its own `/health` endpoint if you want to sanity-check it's
up during debugging: `curl http://127.0.0.1:5004/health`.

## Where the data actually lives

Baserow, database `473779`, table `1041434` (the "Users" table — Asset
Management's existing roster). A **"Tool Access"** multi-select field on
that table carries every tool's grants in one place: each selected option
is shaped `<tool-slug>:<role>` (e.g. `burst-finder:admin`,
`aws-costs:user`). A row with no option for your slug has no access to
your tool, full stop. The existing `Active` field is a global kill switch
that applies before Tool Access is even checked — someone can hold a
`your-slug:admin` grant and still be locked out everywhere if `Active` is
unchecked.

**You will very likely need to ask whoever runs this migration to confirm
your users already have `<your-slug>:<role>` options set in Tool Access**
before you cut over — that data-population step happens once, centrally,
separately from this code change, and isn't something you do from inside
your tool's repo. If you're not sure it's done, ask rather than assuming;
cutting your app over before the grants exist will lock everyone out of
your tool.

## Making the change

1. **Find your current inner-gate code.** Look for wherever your backend
   reads `X-Auth-Request-Email` and checks it against something —
   commonly a `require_auth`/`get_current_user` dependency, an
   `auth.py`/`auth_config.py`, or similar.

2. **Replace the local lookup with a call to `/internal/check`.** Keep the
   same function signature/shape your app already depends on (same
   dependency-injection pattern, same exception type on failure) so
   nothing else in your codebase has to change — only the body of that one
   function. Roughly:

   ```python
   import httpx

   USER_MGMT_URL = "http://127.0.0.1:5004"

   async def get_current_user(x_auth_request_email: str | None = Header(default=None)):
       if not x_auth_request_email:
           raise HTTPException(401, "Not signed in")
       try:
           async with httpx.AsyncClient(timeout=5) as client:
               r = await client.get(f"{USER_MGMT_URL}/internal/check",
                                     params={"email": x_auth_request_email, "tool": "<your-slug>"})
               r.raise_for_status()
               data = r.json()
       except httpx.HTTPError:
           raise HTTPException(503, "User management service unavailable")
       if not data["allowed"]:
           raise HTTPException(403, "Not authorized for this tool")
       return {"email": x_auth_request_email, "role": data["role"], "name": data["name"]}
   ```

   If your app already has a short-TTL in-memory cache around its old
   lookup (Asset Management's `auth.py` does, 30s), keep the same caching
   shape around this call instead — no need to hit the service on every
   single request, but don't cache for so long that a revoked grant stays
   live for minutes.

3. **Remove any in-app "manage users" CRUD.** If your tool has its own
   admin panel/API for adding or removing allowlist entries (burst-finder
   does), remove it — Baserow's grid *is* the admin UI now, there's no
   reason to maintain a second one that only half-works (this service is
   read-only; it never writes to Baserow). Point whatever admin-facing UI
   you had at a short note like "manage access in Baserow" rather than
   leaving a dead button.

4. **Leave your old allowlist file/table alone for now, unused.** Don't
   delete `allowlist.json` or drop old Baserow columns in the same change
   — comment out or delete just the code path that reads them, keep the
   data as a fallback until the new path is confirmed working in
   production. Clean up the leftover data in a follow-up once you're sure.

5. **Don't touch:** `infra/oauth2-proxy/`, `infra/nginx/tools.conf`,
   `manifest.json`'s `gated` flag, or anything about the outer login gate.
   None of that changes — you're only swapping what happens *after*
   someone's already authenticated.
   
## Deploying your change to the box

Don't `git pull` or bare `pip install` on the box itself — it's not a git
checkout (no `git` binary is even installed there), and it only ever
receives files pushed to it. Two things trip people up every time:

- **Push files, don't pull a repo.** From your own machine, `scp` the
  changed backend file(s) to `/opt/tools/<your-slug>/` (or `/backend/` or
  `/web/` underneath it, whichever your tool already uses — check what's
  there with `ssh ... "ls /opt/tools/<your-slug>/"` if unsure). There's no
  CI/CD wired up yet for most tools, so this manual push is the actual
  deploy step, not a placeholder for one.
- **Install into the tool's own venv, not system Python.** Every tool has
  its own venv at `/opt/tools/<your-slug>/venv/` (or a `web/venv` alongside
  its app code — again, check). Running bare `pip install` as `ec2-user`
  silently falls back to a user-level install that the systemd service
  (which runs as the `nginx` user) can never see — `httpx` (or whatever new
  dependency your change adds) will import-error at runtime even though
  `pip install` appeared to succeed. Always install with the venv's own
  pip binary explicitly:

  ```
  sudo /opt/tools/<your-slug>/venv/bin/pip install -r /opt/tools/<your-slug>/<web-or-backend>/requirements.txt
  ```

- **Restart your tool's actual unit name**, not a guessed one — it's
  `tool-<your-slug>` (e.g. `tool-burst-finder`, `tool-asset-management`),
  matching the `.service` file in this ops repo's `infra/systemd/`:

  ```
  sudo systemctl restart tool-<your-slug>
  sudo systemctl status tool-<your-slug> --no-pager -l | head -10
  ```

If any of this doesn't match what you find on the box for your specific
tool, that's a sign to stop and ask rather than improvise — the deploy
mechanism is intentionally uniform across tools, so a mismatch usually
means you're looking at the wrong path/unit name, not that your tool is
special.

## Verifying it worked

- `curl http://127.0.0.1:5004/health` on the box → `{"status": "ok"}`.
- `curl "http://127.0.0.1:5004/internal/check?email=<your own email>&tool=<your-slug>"`
  on the box → confirm `allowed: true` and the role you expect, *before*
  relying on it from your app.
- Deploy your app's change, then sign in fresh through the normal browser
  flow (`https://motusaws.duckdns.org/<your-slug>/`) as yourself and as
  (if possible) a second test account, and confirm both the allow and deny
  paths behave the same as they did before the migration.
- Check your app's logs for `503`s from the auth dependency, which would
  mean it couldn't reach `127.0.0.1:5004` — if you see that, the central
  service (`tool-user-management` on the box) may not be running; that's
  outside your repo, flag it rather than working around it.
