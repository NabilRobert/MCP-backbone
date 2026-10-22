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
  var CHART = { width: 640, height: 140, labelSpace: 18, barGap: 4 };
  function svgElement(tag, attributes) {
    const element = document.createElementNS(SVG_NS, tag);
    for (const [name, value] of Object.entries(attributes)) {
      element.setAttribute(name, String(value));
    }
    return element;
  }
  function failedCount(day) {
    return day.rejected + day.errored + day.timed_out;
  }
  function renderChart(days, container) {
    const barWidth = CHART.width / days.length - CHART.barGap;
    const plotHeight = CHART.height - CHART.labelSpace;
    const totals = days.map((day) => day.succeeded + failedCount(day));
    const scale = plotHeight / Math.max(1, ...totals);
    const svg = svgElement("svg", {
      viewBox: `0 0 ${CHART.width} ${CHART.height}`,
      role: "img",
      "aria-label": `Queries per day for the last ${days.length} days: ${totals.reduce((a, b) => a + b, 0)} in total`
    });
    days.forEach((day, i) => {
      const x = i * (barWidth + CHART.barGap);
      const tooltip = `${day.date}: ${day.succeeded} succeeded, ${failedCount(day)} not`;
      const okHeight = day.succeeded * scale;
      const failedHeight = failedCount(day) * scale;
      const addBar = (height, top, className) => {
        if (height === 0) return;
        const rect = svgElement("rect", { x, y: top, width: barWidth, height, class: className });
        const title = svgElement("title", {});
        title.textContent = tooltip;
        rect.appendChild(title);
        svg.appendChild(rect);
      };
      addBar(okHeight, plotHeight - okHeight, "bar-ok");
      addBar(failedHeight, plotHeight - okHeight - failedHeight, "bar-failed");
      if (i % 2 === 0 || i === days.length - 1) {
        const label = svgElement("text", {
          x: x + barWidth / 2,
          y: CHART.height - 4,
          "text-anchor": "middle",
          class: "bar-label"
        });
        label.textContent = day.date.slice(5);
        svg.appendChild(label);
      }
    });
    container.replaceChildren(svg);
  }
  function inputValue(id) {
    const element = document.getElementById(id);
    return element instanceof HTMLInputElement || element instanceof HTMLSelectElement ? element.value.trim() : "";
  }
  function currentFilters() {
    const params = new URLSearchParams();
    const status = inputValue("filter-status");
    const search = inputValue("filter-search");
    if (status) params.set("status", status);
    if (search) params.set("q", search);
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
