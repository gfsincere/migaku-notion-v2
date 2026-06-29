const fmt = (n) => (n == null ? "—" : n.toLocaleString());
const fmtDelta = (n) => {
  if (n == null) return "—";
  if (n === 0) return "0";
  return (n > 0 ? "+" : "") + n.toLocaleString();
};

let cumulativeChart = null;
let deltaChart = null;

function paceLine(pace) {
  if (!pace) return "Need more snapshots";
  const w = pace.words_per_day;
  const c = pace.chars_per_day;
  const signW = w >= 0 ? "+" : "";
  const signC = c >= 0 ? "+" : "";
  return `${signW}${w.toFixed(1)} words/day · ${signC}${c.toFixed(2)} chars/day (${pace.days}d)`;
}

function deltaClass(n) {
  if (n == null) return "";
  if (n > 0) return "delta-pos";
  if (n < 0) return "delta-neg";
  return "delta-zero";
}

function renderCharts(points) {
  const labels = points.map((p) => p.date);
  const words = points.map((p) => p.known_words);
  const chars = points.map((p) => p.known_chars);
  const dWords = points.map((p) => p.delta_words ?? 0);
  const dChars = points.map((p) => p.delta_chars ?? 0);

  const common = {
    responsive: true,
    maintainAspectRatio: false,
    plugins: { legend: { labels: { color: "#8b98a8" } } },
    scales: {
      x: { ticks: { color: "#8b98a8" }, grid: { color: "#2a3441" } },
      y: { ticks: { color: "#8b98a8" }, grid: { color: "#2a3441" } },
    },
  };

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

function renderTable(points) {
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

async function load() {
  const lang = document.getElementById("lang").value.trim() || "zh";
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
  document.getElementById("v-pace7").textContent = paceLine(data.pace?.["7d"]);
  document.getElementById("v-pace30").textContent = paceLine(data.pace?.["30d"]);

  if (!data.points?.length) {
    err.textContent = "No snapshots yet. Run sync or: python -m migaku_notion progress --record";
    err.classList.remove("hidden");
    return;
  }

  renderCharts(data.points);
  renderTable(data.points);
}

document.getElementById("refresh").addEventListener("click", load);
document.getElementById("lang").addEventListener("change", load);

const params = new URLSearchParams(location.search);
if (params.get("lang")) document.getElementById("lang").value = params.get("lang");
load();
