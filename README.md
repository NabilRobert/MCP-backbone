# Database MCP Server

An MCP server that lets an AI model explore a Postgres database and query
it, safely. Every query is parsed, cost-checked, run read-only and logged,
and the database itself refuses writes, so a validator bug doesn't become
a data-loss bug.

**Try it:** sign up at **https://mcp.my.id** (no invite needed), install
the Claude Code plugin, and ask Claude about a sample database
([how](#use-it)).

| 1 · Ask a question | 2 · Try to change data |
|---|---|
| ![Claude explores the schema, then answers with one read-only query](app/console/static/demo/step1.gif) | ![The server refuses a DELETE: only SELECT statements are allowed](app/console/static/demo/step2.gif) |

Every query, including the refused one, then shows up in the console's
[audit trail](app/console/static/demo/step3.png).

## How it works

```mermaid
flowchart LR
    C[Claude Code<br/>+ plugin] -->|HTTPS, Bearer key| P[Caddy<br/>mcp.my.id]
    P --> S[MCP server<br/>FastAPI + uvicorn]
    S --> A{auth +<br/>rate limit}
    A --> V[validate SQL<br/>sqlglot AST]
    V --> K[cost check<br/>EXPLAIN]
    K --> T[(target DB<br/>READ ONLY txn,<br/>read-only role)]
    S --> B[(bookkeeping DB<br/>keys, audit log)]
    U[Browser] --> S
```

A query has to pass every layer, and each layer assumes the one before it
might fail:

1. **Auth:** a workspace API key in the `Authorization` header, stored only
   as a hash, revocable from the console, and limited to the tools it needs
   (full access, explore only, or schema only). Per-workspace rate limits.
2. **Validation:** exactly one `SELECT`. The whole syntax tree is checked for
   hidden writes (a write inside a CTE, `SELECT INTO`), and only built-in
   functions are allowed.
3. **Cost check:** `EXPLAIN` estimates the cost before anything runs, and
   expensive queries are rejected.
4. **Execution limits:** a statement timeout, and results capped at 200 rows.
5. **The database itself:** every transaction is `READ ONLY`, and where
   possible the login is a read-only role too. Each covers a gap the other
   has (see [THREAT_MODEL.md](THREAT_MODEL.md)).
6. **Audit:** every attempt, including rejected ones, is logged before a
   result is returned. A web console shows the log live.

Proven by an adversarial suite that sends writes straight to the database,
bypassing the validator: [RED_TEAM.md](RED_TEAM.md).

**How well an AI uses it:** `gpt-5-mini` answered 29 of 30 hand-written
questions over the demo database correctly under strict grading (exact
columns and values), 30 of 30 leniently. Every miss across both runs is
explained in [BENCHMARK.md](BENCHMARK.md), including a tool fix and a
question fix that came out of the first run.

## Use it

1. **Sign up** at https://mcp.my.id with a username and a password of 12+
   characters. You get the demo database (Pagila, a DVD-rental store).
   To query **your own Postgres** instead, open "Connect your own database"
   and enter an invite code and its connection URL (stored encrypted; a
   read-only login is recommended).
2. **Copy your API key.** It's shown once. Issue a new one any time from
   the console's Keys page, optionally with less access (for example, schema
   only, for a client that never needs to read rows).
3. **Install the plugin** in Claude Code, and enter the key when asked:
   ```
   /plugin install mcp-backbone --marketplace NabilRobert/MCP-backbone
   ```
4. **Ask.** For example, "which film category has the most films?". Every
   query shows up on your console's dashboard.

Per workspace, per hour: 5 queries and 30 row samples; listing and
describing tables is free. Demo signups are capped per network and per day.

## Run it yourself

```bash
uv sync
cp .env.example .env            # fill in; APP_CONNECTION_ENCRYPTION_KEY is required
uv run alembic upgrade head
uv run uvicorn app.main:app --reload
```

One process serves the console (`/console`), the MCP endpoint (`/mcp/`) and
`/healthz`. Invite someone with
`python -m scripts.create_invite --note "alice"`. Deploying, monitoring,
restoring and rebuilding the production server:
[deploy/RUNBOOK.md](deploy/RUNBOOK.md).

**Tests** (130, against real Postgres, run in CI on every push) need a
disposable database. The suite drops every table after each test and
refuses to run unless the database name contains `test`. It also needs
`CREATEROLE`, because the red-team tests create real throwaway roles:

```bash
docker run -d --name jarvis-mcp-test-db -e POSTGRES_PASSWORD=test \
  -e POSTGRES_DB=test -p 55432:5432 postgres:16-alpine
# .env: APP_TEST_DATABASE_URL=postgresql+asyncpg://postgres:test@localhost:55432/test
uv run pytest
```

The console's compiled JavaScript is committed (the server needs no Node);
CI fails if it drifts from `static-src/main.ts`.

## Layout

```
app/
  auth/       workspaces, API keys, console logins, signup, rate limits
  query/      validation, cost check, execution, address guard, audit log
  console/    server-rendered web console (Jinja2 + a little TypeScript)
  mcp/        the MCP tools: a thin adapter, no business logic
  benchmark/  text-to-SQL eval cases and grading
plugin/       the Claude Code plugin (MCP registration + query skill)
deploy/       systemd unit, Caddy block, firewall, backups, runbook
scripts/      operator CLIs: invites, seeding, datasets, benchmark
```

## More

- [ARCHITECTURE.md](ARCHITECTURE.md): a guided tour of the code, following a
  request through it. Start here.
- [THREAT_MODEL.md](THREAT_MODEL.md): what it defends against, how, and
  what it doesn't.
- [RED_TEAM.md](RED_TEAM.md): the adversarial suite and its results,
  including the limits it demonstrates.
- [JOURNEY.md](JOURNEY.md): the build log. What broke, what was assumed
  and turned out wrong, and why each decision landed where it did.
- [phase-5-architecture-decisions.md](phase-5-architecture-decisions.md):
  the ADRs. Phases [1](phase-1-problem-brief.md)–[7](phase-7-slices-plan.md)
  are the full planning trail.

## License

[MIT](LICENSE).
