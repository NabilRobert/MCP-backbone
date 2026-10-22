# Database MCP Server

A standalone, read-only database MCP server that lets an AI client explore
a database's schema and run safe, read-only SQL queries against it — with
every attempt validated, cost-checked, and logged. Built to be small enough
to read end to end, not as a piece of a larger platform.

**Try it:** sign up at **https://mcp.my.id** (no invite needed for the
demo), install the Claude Code plugin, and ask Claude about a sample
database. Steps under [How to use](#how-to-use).

A guided tour of the codebase -- what each file does and why, following
how a real request actually flows through it -- is in
[ARCHITECTURE.md](ARCHITECTURE.md). Start there if you're new to this repo.

## Planning

The full phase-by-phase planning trail (problem, requirements, domain model,
flows/failures, architecture/ADRs, risks/skeleton, sliced work plan) lives
alongside this README:

- [phase-1-problem-brief.md](phase-1-problem-brief.md)
- [phase-2-requirements-constraints.md](phase-2-requirements-constraints.md)
- [phase-3-domain-model.md](phase-3-domain-model.md)
- [phase-4-flows-failures.md](phase-4-flows-failures.md)
- [phase-5-architecture-decisions.md](phase-5-architecture-decisions.md)
- [phase-6-risks-skeleton.md](phase-6-risks-skeleton.md)
- [phase-7-slices-plan.md](phase-7-slices-plan.md)

What this server defends against, how, and what it doesn't is in
[THREAT_MODEL.md](THREAT_MODEL.md). The adversarial test suite proving
those defenses, with published pass/fail results, is
[RED_TEAM.md](RED_TEAM.md).

The day-by-day build log — what broke, what surprised me, why decisions
landed where they did — is in [JOURNEY.md](JOURNEY.md).

## Status

**Slices A–E** built. Auth (issuance + revocation), rate limiting, and
all four MCP tools (`list_tables`, `describe_table`, `sample_rows`,
`run_query`) work end to end through the real MCP protocol -- verified
via an in-memory client/server session, not just the SDK's raise-on-error
convenience method, which caught a real bug in how tool errors reached
the model. 114 passing tests against a real Postgres instance, CI
running on every push.

**The MCP server can now actually be reached over a network** --
mounted at `/mcp` on the same FastAPI app as the console (one process,
one port), using the Streamable HTTP transport. Authentication now
also accepts a real `Authorization: Bearer <key>` header, in addition
to (not instead of) the existing `api_key` tool argument, so a real
HTTP client can authenticate at the connection level instead of
needing a custom wrapper. Verified three ways: the pre-existing
in-memory protocol test, a new test against the actual mounted route
using a real HTTP header and no `api_key` argument at all, and a real
`uvicorn` process hit over an actual TCP socket. See JOURNEY.md for
four real bugs (wrong lifespan, a double-mounted path, DNS-rebinding
protection, an unrelated stray process squatting the first port tried)
each found only by actually running it.

**Deployed and live on a real VPS, behind a real domain** -- a real
Claude Code session, running on a laptop, successfully calling this
server's tools over the open internet against the real Pagila
database, over HTTPS (`https://mcp.my.id/mcp/`, a Let's Encrypt
certificate that renews itself). Runs under a systemd unit
(`mcp-backbone`), not a manually tracked background process; the unit,
cert-renewal hook, backup script and cron line all live in `deploy/`
so the server can be rebuilt from git. Getting there surfaced a genuine security
issue worth being upfront about, not just a bug: the bookkeeping
database and the target database were briefly the same database on
first deploy, meaning an AI client could in principle have read API
key hashes and the full audit log through `run_query`. Caught and
fixed the same day, before anyone but the one real workspace ever used
it -- bookkeeping now lives on a separate, local Postgres instance on
the VPS itself. Full account in JOURNEY.md, including the two more
real bugs (a target connection URL silently resolving to an
uninstalled database driver; a duplicate console username crashing
every login attempt) found getting the first real tool call to
actually succeed.

**Open demo signup, invites for your own database.** Anyone can sign
up for a workspace (key, audit log, rate limits) on the demo database.
Connecting your own database needs an invite code, because that's the
part with real risk: it makes the server connect to a host you chose
and store your database password. Letting users register their
own database meant closing the gaps that opens: connection URLs are
encrypted at rest, an address guard stops a URL from pointing the server
at its own internals, `sample_rows` is now audited and rate-limited, and
console logins are throttled. See "How to use" below, ADR-007, and
THREAT_MODEL.md T7-T9.

**Slice E (benchmark) now run for real -- 10/10 (100%)**:
`scripts/run_benchmark.py` runs a PydanticAI agent (`gpt-5-mini` via
Sumopod) against this server as a real MCP client, grading by
execution accuracy against ten hand-written questions over a
synthetic demo dataset (`scripts/load_demo_dataset.py`). Took three
rounds of fixing real bugs the previous attempt surfaced, not three
rounds of guessing: a per-workspace rate limit sized for occasional
use, not an automated eval loop; a Decimal-vs-string type mismatch
between the agent's MCP-serialized answer and the ground truth's raw
DB value that failed every numeric answer regardless of correctness;
and a grading methodology with no tolerance for an agent rounding a
number for readability or adding one genuinely informative extra
column. See JOURNEY.md for the full progression (30% -> 70% -> 100%)
and exactly what each fix changed.

**Slice F (red-team suite) done** -- 16 adversarial tests against the
safety layer, including genuine defense-in-depth against a real
provisioned read-only role, not just the app-level validator. See
[RED_TEAM.md](RED_TEAM.md) for the full catalog and results.

**Slice G (web console) done** -- login (a separate credential from the
API key, so revoking a compromised key never locks you out of the
audit history), a dashboard polling the real audit log every 5s, and a
keys page that can rotate or revoke the workspace's key. Verified
through FastAPI's `TestClient` hitting the actual HTTP layer (login,
redirects, sessions, forms, the real-data audit API) -- see JOURNEY.md
for a real environment limit hit trying to also verify it visually in
a browser, and why the TestClient coverage made that non-blocking.

All seven slices from `phase-7-slices-plan.md` are now built.

**Pagila, a richer dataset.** `scripts/load_demo_dataset.py`'s
hand-written rows are enough to exercise the code paths but too thin
to demo convincingly or to calibrate `APP_COST_THRESHOLD` against.
`scripts/load_pagila_dataset.py` loads [Pagila](https://github.com/devrimgunduz/pagila)
instead -- a real, widely-known public Postgres sample database (a
DVD-rental store, 16+ tables, ~1,000 films, ~50,000 rentals) -- as an
optional, more credible alternative target. See JOURNEY.md for the
version-compatibility bugs real runs against a live Postgres 16
instance turned up getting it working.

## Layout

```
app/
  auth/       workspace, api keys, console credentials, rate limiting
  query/      data source connections, validation+execution, audit records
  benchmark/  text-to-SQL eval cases and results
  console/    server-rendered web console (Jinja2 + TS)
  mcp/        the MCP protocol layer (thin adapter, no business logic)
plugin/       the Claude Code plugin (MCP registration + query-database skill)
static-src/   TypeScript source for the console, compiled to app/console/static
```

## Running (once dependencies are installed)

```
uv sync
cp .env.example .env   # fill in real values
uv run alembic upgrade head   # creates this project's own bookkeeping tables
uv run uvicorn app.main:app --reload
```

This single process now serves both the console (`/console/...`) and
the MCP server (`/mcp`) -- see the Status section above. `--reload` is
for local development only (single process, restarts on every file
change); a real deployment needs a proper process manager and should
drop that flag.

The console's compiled JavaScript (`app/console/static/main.js`) is
committed, so running or deploying the server needs no Node at all.
Only if you edit `static-src/main.ts`, rebuild and commit both files:

```
cd static-src && npm ci && npm run build
```

CI rebuilds it on every push and fails if the committed copy doesn't
match, so the two can't drift apart.

## How to use

### As a user: ask Claude about a database

1. **Sign up** at `https://<server>` (this project's own server is
   https://mcp.my.id). Pick a username and a password of at least 12
   characters. That's all the demo needs: you get the server's sample
   database (Pagila, a DVD-rental store).
   - **Your own database instead (invite only):** open "Connect your own
     database", enter the invite code from whoever runs the server, and
     paste your Postgres connection URL
     (`postgresql://user:password@host:5432/dbname`). It must be reachable
     from the internet, and a read-only login is strongly recommended:
     signup warns you if the login can write. The URL is stored encrypted.
2. **Copy your API key.** The page after signup shows it once. Lost it?
   Log into the console and issue a new one on the **Keys** page; the old
   one stops working immediately.
3. **Install the plugin** in Claude Code:

   ```
   /plugin install mcp-backbone --marketplace NabilRobert/MCP-backbone
   ```

   When asked, enter the server URL (`https://<server>/mcp/`; the default is
   this project's own server) and your API key. The key goes to your OS
   credential store, not a settings file.
4. **Ask.** For example: "which film category has the most films?" Claude
   looks at the schema, writes one read-only query and answers. The
   plugin's `query-database` skill tells it how to use the tools well, such
   as exploring before spending a query and how to react to a rejection.
5. **Check what ran.** The console's **Dashboard** lists every query and
   data sample against your workspace, including rejected ones.

Limits per workspace, per hour by default: 5 `run_query` calls and 30
`sample_rows` calls. Demo signups are capped at 3 per network and 50 in
total per day. Listing and describing tables is unlimited. The
server only ever runs a single read-only `SELECT`, and rejects anything
estimated to be too expensive.

### As the operator: run the server and invite people

1. **Deploy and configure.** Copy `.env.example` to `.env` and fill it in.
   `APP_CONNECTION_ENCRYPTION_KEY` is required: the server won't start
   without it. Generate it with the command in `.env.example` and keep a copy
   off the server. `APP_TARGET_DATABASE_URL` is the default database new
   workspaces query. Then run `alembic upgrade head` and start the server
   (see Running below, and `deploy/` for the systemd unit).
2. **Invite someone who wants their own database.** Demo accounts need no
   invite (turn them off with `APP_DEMO_SIGNUPS_ENABLED=false`). Each code is
   single use and expires after 14 days by default:

   ```
   python -m scripts.create_invite --note "alice"
   ```

   Send them the code and the signup link.
3. **Host the demo database yourself (recommended).** Anyone can sign up for
   the demo, so its database should refuse writes on its own, not only through
   the app's checks. `deploy/setup_demo_database.sh` builds Pagila on the
   server's own Postgres with a read-only login, proves that login can't
   write, and switches the app over.
4. **Manage access.** Revoke a key from the console, or with
   `scripts/revoke_key.py`. `scripts/seed_workspace.py` still creates a
   workspace directly, without an invite.

## Running tests: the `APP_DATABASE_URL` privilege requirement

This isn't a local-vs-remote thing -- it's about privilege level.
`APP_DATABASE_URL` (this project's own bookkeeping database) needs an
account with `CREATEROLE`, wherever that database actually lives.
Several tests (`tests/test_red_team.py`'s defense-in-depth suite in
particular) provision a real, throwaway Postgres role via
`scripts/provision_readonly_role.py` to prove the database itself
blocks a write, not just the app's own validator -- deliberately, so
that proof isn't faked.

A *shared, restricted-tenant* host -- even one you otherwise have full
read/write access to -- will fail those tests with `permission denied
to drop role`, not because anything's broken, but because withholding
`CREATEROLE` from ordinary accounts is exactly what that kind of
hosting tier is supposed to do. A self-hosted remote Postgres (your
own VPS, your own superuser) works just as well as a local one --
what matters is control over the account, not where the machine is.
CI's own Postgres service container (`.github/workflows/ci.yml`) is
technically remote too (a fresh container on GitHub's runner), and it
passes 100% because it connects as the default `postgres` superuser.

For local dev, a disposable container is the easiest way to get that:

```
docker run -d --name jarvis-mcp-app-db \
  -e POSTGRES_USER=jarvis -e POSTGRES_PASSWORD=jarvis -e POSTGRES_DB=jarvis_mcp \
  -p 5432:5432 postgres:16-alpine
```

`APP_TARGET_DATABASE_URL` (the database being *queried*, not this
project's own) has no such requirement, and realistically *shouldn't*
have it -- it's meant to model exactly the kind of restricted external
database this project doesn't control, so it can keep pointing at a
managed, multi-tenant host indefinitely.
