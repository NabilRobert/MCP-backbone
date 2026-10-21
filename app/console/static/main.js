"use strict";
(() => {
  // main.ts
  var POLL_INTERVAL_MS = 5e3;
  function renderRows(rows, tbody) {
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
      tbody.appendChild(tr);
    }
  }
  function renderSummary(rows, el) {
    const counts = /* @__PURE__ */ new Map();
    for (const row of rows) {
      counts.set(row.status, (counts.get(row.status) ?? 0) + 1);
    }
    const parts = Array.from(counts.entries()).map(([status, count]) => `${status}: ${count}`);
    el.textContent = parts.length > 0 ? parts.join(" / ") : "No queries yet.";
  }
  async function refreshAuditTable() {
    const tbody = document.getElementById("audit-rows");
    const summary = document.getElementById("summary");
    if (!tbody) return;
    try {
      const response = await fetch("/console/api/audit");
      if (!response.ok) return;
      const rows = await response.json();
      renderRows(rows, tbody);
      if (summary) renderSummary(rows, summary);
    } catch {
    }
  }
  if (document.getElementById("audit-rows")) {
    refreshAuditTable();
    setInterval(refreshAuditTable, POLL_INTERVAL_MS);
  }
})();
