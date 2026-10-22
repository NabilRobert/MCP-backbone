---
name: query-database
description: Answer questions about data in the database connected through the mcp-backbone plugin's "database" MCP server, by exploring its schema and running read-only SQL. Use when the user asks what's in the database, asks a question its data could answer, or wants a query written and run against it.
---

# Querying the connected database

The `database` MCP server gives you four tools. The server, not you, enforces
safety: every `run_query` is parsed, checked for being a single read-only
`SELECT`, cost-estimated, and logged before anything executes. Calls that
return real data (`run_query`, `sample_rows`) are logged and visible to the
workspace owner.

| Tool and arguments | Use it for | Rate-limited |
|---|---|---|
| `list_tables()` | See what tables exist | No |
| `describe_table(table_name)` | Column names and types for one table | No |
| `sample_rows(table_name, limit)` | A few real rows (`limit` defaults to 10, max 100) to see what values look like | Yes, larger allowance (30/hour by default) |
| `run_query(sql)` | Run one read-only SQL statement and get the results | **Yes, tight** (5/hour by default) |

Every tool also has an optional `api_key` argument. Leave it out: the plugin
already sends the key as a header, and the server ignores the argument over HTTP.

## Workflow

1. **Explore first.** Call `list_tables`, then `describe_table` on every
   table the question touches; both are free. Use `sample_rows` when you need
   to know how values are actually stored (codes vs. names, date formats,
   nulls) before filtering on them. It has its own, larger allowance, so
   prefer it over `run_query` for looking around, but don't sample tables you
   don't need.
2. **Then write one good query.** `run_query` is limited to a small number of
   calls per hour per workspace, so don't use it to explore and don't guess
   at column names. Get the schema right first and answer with one query
   where you can.
3. **Answer in plain language** first, then show the SQL you ran.

`run_query` returns at most 200 rows by default. If a result was cut off, the
response has `truncated: true` and a note. Don't answer a "how many" or
"total" question from a truncated list: aggregate in SQL instead.

## When `run_query` is rejected

The error message says why. Fix the cause before you retry, and don't resend
the same query:

- **Not a single read-only SELECT:** the server only accepts one `SELECT`
  statement. No writes, DDL, multiple statements, or data-modifying CTEs.
  Tell the user if they asked for a change: this server is read-only by design.
- **Function not allowed:** rewrite without that function.
- **Estimated cost too high:** narrow it down. Filter earlier, aggregate
  instead of returning raw rows, avoid accidental cross joins, add a `LIMIT`.
- **Timed out:** same fixes as cost. The query ran but was too slow.
- **Could not parse SQL:** fix the syntax (Postgres dialect).
- **Rate limit exceeded:** stop calling that tool (`run_query` and
  `sample_rows` have separate limits). Tell the user the limit resets when
  the server's window ends (an hour by default), and answer from what you
  already have.
- **Authentication failed / missing API key:** the plugin's API key is
  wrong, revoked, or not set. Tell the user to update it with `/plugin`
  (configure the mcp-backbone plugin), or to issue a new key from the
  server's console Keys page. Don't retry, and never put a key in a tool
  argument: the server refuses keys passed that way over HTTP.

## Don'ts

- Don't try to work around a rejection by rephrasing the same operation. The
  validator, cost check and (where provisioned) a read-only database role are
  deliberate guardrails.
- Don't present a sampled or `LIMIT`ed result as a total. Say when results
  are partial.
