const fmt = (n) => (n == null ? "—" : n.toLocaleString());
const fmtDelta = (n) => {
  if (n == null) return "—";
  if (n === 0) return "0";
  return (n > 0 ? "+" : "") + n.toLocaleString();
};

let cumulativeChart = null;
let deltaChart = null;
let hsk30Chart = null;
let hsk20Chart = null;

const chartCommon = {
  responsive: true,
  maintainAspectRatio: false,
  plugins: { legend: { labels: { color: "#8b98a8" } } },
  scales: {
    x: { ticks: { color: "#8b98a8" }, grid: { color: "#2a3441" } },
    y: {
      ticks: { color: "#8b98a8" },
      grid: { color: "#2a3441" },
      max: 100,
    },
  },
};

function paceLine(pace) {
  if (!pace) return "Need more snapshots";
  const w = pace.words_per_day;
  const c = pace.chars_per_day;
  const signW = w >= 0 ? "+" : "";
  const signC = c >= 0 ? "+" : "";
  return `${signW}${w.toFixed(1)} words/day · ${signC}${c.toFixed(2)} chars/day (${pace.days}d)`;
}

function hskEstimateLine(report) {
  if (!report) return "—";
  const est = report.estimated_level;
  if (est == null) return `Below L1 (<${report.threshold_pct}%)`;
  const nxt = report.next_level;
  if (nxt) {
    return `Level ${est} · L${nxt.level} at ${nxt.pct}%`;
  }
  return `Level ${est}+`;
}

function deltaClass(n) {
  if (n == null) return "";
  if (n > 0) return "delta-pos";
  if (n < 0) return "delta-neg";
  return "delta-zero";
}

function cellLine(row) {
  return `${row.known}/${row.total} (${row.pct}%)`;
}

function renderProgressCharts(points) {
  const labels = points.map((p) => p.date);
  const words = points.map((p) => p.known_words);
  const chars = points.map((p) => p.known_chars);
  const dWords = points.map((p) => p.delta_words ?? 0);
  const dChars = points.map((p) => p.delta_chars ?? 0);

  const common = { ...chartCommon, scales: {
    x: chartCommon.scales.x,
    y: { ...chartCommon.scales.y, max: undefined },
  }};

  if (cumulativeChart) cumulativeChart.destroy();
  cumulativeChart = new Chart(document.getElementById("chart-cumulative"), {
    type: "line",
    data: {
      labels,
      datasets: [
        { label: "Known words", data: words, borderColor: "#6ecf8e", tension: 0.2 },
        { label: "Known Hanzi", data: chars, borderColor: "#e8b86d", tension: 0.2 },
      ],
    },
    options: common,
  });

  if (deltaChart) deltaChart.destroy();
  deltaChart = new Chart(document.getElementById("chart-delta"), {
    type: "bar",
    data: {
      labels,
      datasets: [
        { label: "Δ words", data: dWords, backgroundColor: "#6ecf8e88" },
        { label: "Δ chars", data: dChars, backgroundColor: "#e8b86d88" },
      ],
    },
    options: common,
  });
}

function renderHskChart(canvasId, report, existingChart) {
  const labels = report.inclusive.map((r) => `L${r.level}`);
  const pcts = report.inclusive.map((r) => r.pct);
  if (existingChart) existingChart.destroy();
  return new Chart(document.getElementById(canvasId), {
    type: "bar",
    data: {
      labels,
      datasets: [{ label: "% known (inclusive)", data: pcts, backgroundColor: "#5b9fd488" }],
    },
    options: chartCommon,
  });
}

function renderHskTable(tbodyId, report) {
  const tbody = document.getElementById(tbodyId);
  tbody.replaceChildren();
  for (let i = 0; i < report.inclusive.length; i++) {
    const inc = report.inclusive[i];
    const exc = report.exclusive[i];
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>L${inc.level}</td>
      <td>${cellLine(inc)}</td>
      <td>${cellLine(exc)}</td>
    `;
    tbody.appendChild(tr);
  }
}

function renderHistoryTable(points) {
  const tbody = document.getElementById("history");
  tbody.replaceChildren();
  for (const p of [...points].reverse()) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${p.date}</td>
      <td>${fmt(p.known_words)}</td>
      <td>${fmt(p.known_chars)}</td>
      <td class="${deltaClass(p.delta_words)}">${fmtDelta(p.delta_words)}</td>
      <td class="${deltaClass(p.delta_chars)}">${fmtDelta(p.delta_chars)}</td>
    `;
    tbody.appendChild(tr);
  }
}

async function loadProgress(lang) {
  const err = document.getElementById("error");
  err.classList.add("hidden");

  const res = await fetch(`/api/progress?lang=${encodeURIComponent(lang)}`);
  const data = await res.json();

  if (data.error) {
    err.textContent = data.error;
    err.classList.remove("hidden");
    return;
  }

  const latest = data.latest;
  document.getElementById("v-words").textContent = latest ? fmt(latest.known_words) : "—";
  document.getElementById("v-chars").textContent = latest ? fmt(latest.known_chars) : "—";

  if (!data.points?.length) {
    err.textContent = "No snapshots yet. Run sync or: python -m migaku_notion progress --record";
    err.classList.remove("hidden");
    return;
  }

  renderProgressCharts(data.points);
  renderHistoryTable(data.points);
}

async function loadHsk(lang) {
  const err = document.getElementById("hsk-error");
  err.classList.add("hidden");

  const res = await fetch(`/api/hsk?lang=${encodeURIComponent(lang)}`);
  const data = await res.json();

  if (data.error) {
    err.textContent = data.error;
    err.classList.remove("hidden");
    return;
  }

  document.getElementById("v-hsk30").textContent = hskEstimateLine(data.hsk30);
  document.getElementById("v-hsk20").textContent = hskEstimateLine(data.hsk20);

  hsk30Chart = renderHskChart("chart-hsk30", data.hsk30, hsk30Chart);
  hsk20Chart = renderHskChart("chart-hsk20", data.hsk20, hsk20Chart);
  renderHskTable("hsk30-table", data.hsk30);
  renderHskTable("hsk20-table", data.hsk20);
}

async function load() {
  const lang = document.getElementById("lang").value.trim() || "zh";
  await Promise.all([loadProgress(lang), loadHsk(lang)]);
}

document.getElementById("refresh").addEventListener("click", load);
document.getElementById("lang").addEventListener("change", load);

const params = new URLSearchParams(location.search);
if (params.get("lang")) document.getElementById("lang").value = params.get("lang");
load();
