"use strict";
(() => {
  // main.ts
  var POLL_INTERVAL_MS = 5e3;
  var SEARCH_DEBOUNCE_MS = 300;
  var SVG_NS = "http://www.w3.org/2000/svg";
  function renderRows(rows, tbody, filtered) {
    tbody.replaceChildren();
    if (rows.length === 0) {
      const row = document.createElement("tr");
      const cell = document.createElement("td");
      cell.colSpan = 5;
      cell.textContent = filtered ? "No queries match these filters." : "No queries yet.";
      row.appendChild(cell);
      tbody.appendChild(row);
      return;
    }
    for (const row of rows) {
      const tr = document.createElement("tr");
      const cells = [
        new Date(row.created_at).toLocaleString(),
        row.status,
        row.query_text,
        row.cost_estimate === null ? "" : row.cost_estimate.toFixed(1),
        row.rejection_reason ?? ""
      ];
      cells.forEach((text, i) => {
        const td = document.createElement("td");
        td.textContent = text;
        if (i === 1) td.className = `status-${row.status}`;
        tr.appendChild(td);
      });
      tbody.appendChild(tr);
    }
  }
  function renderSummary(rows, el) {
    const counts = /* @__PURE__ */ new Map();
    for (const row of rows) {
      counts.set(row.status, (counts.get(row.status) ?? 0) + 1);
    }
    const parts = Array.from(counts.entries()).map(([status, count]) => `${status}: ${count}`);
    el.textContent = parts.length > 0 ? `Showing ${rows.length}: ${parts.join(" / ")}` : "";
  }
  function renderChart(days, container) {
    const width = 640;
    const height = 140;
    const labelSpace = 18;
    const barGap = 4;
    const barWidth = width / days.length - barGap;
    const totals = days.map((d) => d.succeeded + d.rejected + d.errored + d.timed_out);
    const max = Math.max(1, ...totals);
    const svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
    svg.setAttribute("role", "img");
    svg.setAttribute(
      "aria-label",
      `Queries per day for the last ${days.length} days: ${totals.reduce((a, b) => a + b, 0)} in total`
    );
    days.forEach((day, i) => {
      const x = i * (barWidth + barGap);
      const failed = day.rejected + day.errored + day.timed_out;
      const plot = height - labelSpace;
      const okHeight = day.succeeded / max * plot;
      const failedHeight = failed / max * plot;
      for (const [value, barHeight, y, cls] of [
        [day.succeeded, okHeight, plot - okHeight, "bar-ok"],
        [failed, failedHeight, plot - okHeight - failedHeight, "bar-failed"]
      ]) {
        if (value === 0) continue;
        const rect = document.createElementNS(SVG_NS, "rect");
        rect.setAttribute("x", String(x));
        rect.setAttribute("y", String(y));
        rect.setAttribute("width", String(barWidth));
        rect.setAttribute("height", String(barHeight));
        rect.setAttribute("class", cls);
        const title = document.createElementNS(SVG_NS, "title");
        title.textContent = `${day.date}: ${day.succeeded} succeeded, ${failed} not`;
        rect.appendChild(title);
        svg.appendChild(rect);
      }
      if (i % 2 === 0 || i === days.length - 1) {
        const label = document.createElementNS(SVG_NS, "text");
        label.setAttribute("x", String(x + barWidth / 2));
        label.setAttribute("y", String(height - 4));
        label.setAttribute("text-anchor", "middle");
        label.setAttribute("class", "bar-label");
        label.textContent = day.date.slice(5);
        svg.appendChild(label);
      }
    });
    container.replaceChildren(svg);
  }
  function currentFilters() {
    const params = new URLSearchParams();
    const status = document.getElementById("filter-status")?.value;
    const search = document.getElementById("filter-search")?.value;
    if (status) params.set("status", status);
    if (search?.trim()) params.set("q", search.trim());
    return params;
  }
  async function refreshDashboard() {
    const tbody = document.getElementById("audit-rows");
    if (!tbody) return;
    const summary = document.getElementById("summary");
    const chart = document.getElementById("daily-chart");
    try {
      const params = currentFilters();
      const [auditResponse, dailyResponse] = await Promise.all([
        fetch(`/console/api/audit?${params.toString()}`),
        chart ? fetch("/console/api/audit/daily") : Promise.resolve(null)
      ]);
      if (auditResponse.ok) {
        const rows = await auditResponse.json();
        renderRows(rows, tbody, params.toString() !== "");
        if (summary) renderSummary(rows, summary);
      }
      if (chart && dailyResponse?.ok) {
        renderChart(await dailyResponse.json(), chart);
      }
    } catch {
    }
  }
  if (document.getElementById("audit-rows")) {
    let searchTimer;
    document.getElementById("filter-status")?.addEventListener("change", refreshDashboard);
    document.getElementById("filter-search")?.addEventListener("input", () => {
      window.clearTimeout(searchTimer);
      searchTimer = window.setTimeout(refreshDashboard, SEARCH_DEBOUNCE_MS);
    });
    refreshDashboard();
    setInterval(refreshDashboard, POLL_INTERVAL_MS);
  }
})();
