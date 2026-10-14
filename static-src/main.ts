// The only client-side logic in this project (per ADR-003: a server-
// rendered console, not a full SPA framework). Its one job: poll the
// audit-trail JSON endpoint and refresh the dashboard's table -- no
// other interactivity needed anywhere else in the console.

interface AuditRow {
  query_text: string;
  status: string;
  cost_estimate: number | null;
  rejection_reason: string | null;
  created_at: string;
}

const POLL_INTERVAL_MS = 5000;

function renderRows(rows: AuditRow[], tbody: HTMLElement): void {
  tbody.replaceChildren();

  if (rows.length === 0) {
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = 5;
    cell.textContent = "No queries yet.";
    row.appendChild(cell);
    tbody.appendChild(row);
    return;
  }

  for (const row of rows) {
    const tr = document.createElement("tr");

    const time = document.createElement("td");
    time.textContent = new Date(row.created_at).toLocaleString();
    tr.appendChild(time);

    const status = document.createElement("td");
    status.textContent = row.status;
    status.className = `status-${row.status}`;
    tr.appendChild(status);

    const query = document.createElement("td");
    query.textContent = row.query_text;
    tr.appendChild(query);

    const cost = document.createElement("td");
    cost.textContent = row.cost_estimate === null ? "" : row.cost_estimate.toFixed(1);
    tr.appendChild(cost);

    const reason = document.createElement("td");
    reason.textContent = row.rejection_reason ?? "";
    tr.appendChild(reason);

    // Using textContent (not innerHTML) for every cell above is
    // deliberate: query_text and rejection_reason can contain anything
    // an AI client sent, including characters that would be dangerous
    // to insert as raw HTML. textContent never interprets its input as
    // markup, so this is safe regardless of what's in the data.
    tbody.appendChild(tr);
  }
}

function renderSummary(rows: AuditRow[], el: HTMLElement): void {
  const counts = new Map<string, number>();
  for (const row of rows) {
    counts.set(row.status, (counts.get(row.status) ?? 0) + 1);
  }
  const parts = Array.from(counts.entries()).map(([status, count]) => `${status}: ${count}`);
  el.textContent = parts.length > 0 ? parts.join(" / ") : "No queries yet.";
}

async function refreshAuditTable(): Promise<void> {
  const tbody = document.getElementById("audit-rows");
  const summary = document.getElementById("summary");
  if (!tbody) return; // not on the dashboard page

  try {
    const response = await fetch("/console/api/audit");
    if (!response.ok) return;
    const rows: AuditRow[] = await response.json();
    renderRows(rows, tbody);
    if (summary) renderSummary(rows, summary);
  } catch {
    // A failed poll just leaves the table showing its last-known
    // state -- there's always another poll a few seconds later.
  }
}

if (document.getElementById("audit-rows")) {
  refreshAuditTable();
  setInterval(refreshAuditTable, POLL_INTERVAL_MS);
}

export {};
