// The only client-side logic in this project (per ADR-003: a server-
// rendered console, not a full SPA framework). It polls the audit-trail
// JSON endpoints and refreshes the dashboard: the filtered table, its
// summary, and a 14-day chart.
//
// Every value from the server is inserted with textContent or as an SVG
// attribute, never as HTML: query_text and rejection_reason can contain
// anything an AI client sent, and textContent never interprets markup.

interface AuditRow {
  query_text: string;
  status: string;
  cost_estimate: number | null;
  rejection_reason: string | null;
  created_at: string;
}

interface DailyCounts {
  date: string;
  succeeded: number;
  rejected: number;
  errored: number;
  timed_out: number;
}

const POLL_INTERVAL_MS = 5000;
const SEARCH_DEBOUNCE_MS = 300;
const SVG_NS = "http://www.w3.org/2000/svg";

function renderRows(rows: AuditRow[], tbody: HTMLElement, filtered: boolean): void {
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
      row.rejection_reason ?? "",
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

function renderSummary(rows: AuditRow[], el: HTMLElement): void {
  const counts = new Map<string, number>();
  for (const row of rows) {
    counts.set(row.status, (counts.get(row.status) ?? 0) + 1);
  }
  const parts = Array.from(counts.entries()).map(([status, count]) => `${status}: ${count}`);
  el.textContent = parts.length > 0 ? `Showing ${rows.length}: ${parts.join(" / ")}` : "";
}

// Chart geometry, in SVG user units (the SVG scales to its container).
const CHART = { width: 640, height: 140, labelSpace: 18, barGap: 4 } as const;

function svgElement<K extends keyof SVGElementTagNameMap>(
  tag: K,
  attributes: Record<string, string | number>,
): SVGElementTagNameMap[K] {
  const element = document.createElementNS(SVG_NS, tag);
  for (const [name, value] of Object.entries(attributes)) {
    element.setAttribute(name, String(value));
  }
  return element;
}

function failedCount(day: DailyCounts): number {
  return day.rejected + day.errored + day.timed_out;
}

// A stacked bar per day: succeeded at the bottom, everything that didn't
// succeed (rejected, errored, timed out) on top, labelled every other day.
function renderChart(days: DailyCounts[], container: HTMLElement): void {
  const barWidth = CHART.width / days.length - CHART.barGap;
  const plotHeight = CHART.height - CHART.labelSpace;
  const totals = days.map((day) => day.succeeded + failedCount(day));
  const scale = plotHeight / Math.max(1, ...totals);

  const svg = svgElement("svg", {
    viewBox: `0 0 ${CHART.width} ${CHART.height}`,
    role: "img",
    "aria-label": `Queries per day for the last ${days.length} days: ${totals.reduce((a, b) => a + b, 0)} in total`,
  });

  days.forEach((day, i) => {
    const x = i * (barWidth + CHART.barGap);
    const tooltip = `${day.date}: ${day.succeeded} succeeded, ${failedCount(day)} not`;
    const okHeight = day.succeeded * scale;
    const failedHeight = failedCount(day) * scale;

    const addBar = (height: number, top: number, className: string): void => {
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
        class: "bar-label",
      });
      label.textContent = day.date.slice(5); // MM-DD
      svg.appendChild(label);
    }
  });

  container.replaceChildren(svg);
}

function inputValue(id: string): string {
  const element = document.getElementById(id);
  return element instanceof HTMLInputElement || element instanceof HTMLSelectElement
    ? element.value.trim()
    : "";
}

function currentFilters(): URLSearchParams {
  const params = new URLSearchParams();
  const status = inputValue("filter-status");
  const search = inputValue("filter-search");
  if (status) params.set("status", status);
  if (search) params.set("q", search);
  return params;
}

async function refreshDashboard(): Promise<void> {
  const tbody = document.getElementById("audit-rows");
  if (!tbody) return; // not on the dashboard page
  const summary = document.getElementById("summary");
  const chart = document.getElementById("daily-chart");

  try {
    const params = currentFilters();
    const [auditResponse, dailyResponse] = await Promise.all([
      fetch(`/console/api/audit?${params.toString()}`),
      chart ? fetch("/console/api/audit/daily") : Promise.resolve(null),
    ]);
    if (auditResponse.ok) {
      const rows: AuditRow[] = await auditResponse.json();
      renderRows(rows, tbody, params.toString() !== "");
      if (summary) renderSummary(rows, summary);
    }
    if (chart && dailyResponse?.ok) {
      renderChart(await dailyResponse.json(), chart);
    }
  } catch {
    // A failed poll just leaves the page showing its last-known state;
    // there's always another poll a few seconds later.
  }
}

if (document.getElementById("audit-rows")) {
  let searchTimer: number | undefined;
  document.getElementById("filter-status")?.addEventListener("change", refreshDashboard);
  document.getElementById("filter-search")?.addEventListener("input", () => {
    window.clearTimeout(searchTimer);
    searchTimer = window.setTimeout(refreshDashboard, SEARCH_DEBOUNCE_MS);
  });
  refreshDashboard();
  setInterval(refreshDashboard, POLL_INTERVAL_MS);
}

export {};
