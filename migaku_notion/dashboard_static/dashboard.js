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
let gapsData = null;
let selectedLevel = 1;

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

function hskEstimateLine(report) {
  if (!report) return "—";
  const est = report.estimated_level;
  if (est == null) return `Below L1 (<${report.threshold_pct}%)`;
  const nxt = report.next_level;
  if (nxt) return `Level ${est} · L${nxt.level} at ${nxt.pct}%`;
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

function switchTab(name) {
  document.querySelectorAll(".tab").forEach((el) => {
    el.classList.toggle("active", el.dataset.tab === name);
  });
  document.querySelectorAll(".tab-panel").forEach((el) => {
    el.classList.toggle("active", el.id === `tab-${name}`);
  });
  if (name === "missing" && !gapsData) {
    loadGaps(document.getElementById("lang").value.trim() || "zh");
  }
}

function renderProgressCharts(points) {
  const labels = points.map((p) => p.date);
  const common = {
    ...chartCommon,
    scales: {
      x: chartCommon.scales.x,
      y: { ...chartCommon.scales.y, max: undefined },
    },
  };

  if (cumulativeChart) cumulativeChart.destroy();
  cumulativeChart = new Chart(document.getElementById("chart-cumulative"), {
    type: "line",
    data: {
      labels,
      datasets: [
        { label: "Known words", data: points.map((p) => p.known_words), borderColor: "#6ecf8e", tension: 0.2 },
        { label: "Known Hanzi", data: points.map((p) => p.known_chars), borderColor: "#e8b86d", tension: 0.2 },
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
        { label: "Δ words", data: points.map((p) => p.delta_words ?? 0), backgroundColor: "#6ecf8e88" },
        { label: "Δ chars", data: points.map((p) => p.delta_chars ?? 0), backgroundColor: "#e8b86d88" },
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
    tr.innerHTML = `<td>L${inc.level}</td><td>${cellLine(inc)}</td><td>${cellLine(exc)}</td>`;
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
      <td class="${deltaClass(p.delta_chars)}">${fmtDelta(p.delta_chars)}</td>`;
    tbody.appendChild(tr);
  }
}

function renderWordGrid(containerId, words, listKind) {
  const el = document.getElementById(containerId);
  el.replaceChildren();
  if (!words.length) {
    el.innerHTML = '<p class="empty">None</p>';
    return;
  }
  for (const w of words) {
    const details = document.createElement("details");
    details.className = "word-chip-details";

    const summary = document.createElement("summary");
    summary.className = "word-chip";
    summary.textContent = w;
    details.appendChild(summary);

    const menu = document.createElement("div");
    menu.className = "word-menu";

    const mkBtn = (label, status) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "word-menu-btn";
      btn.textContent = label;
      btn.addEventListener("click", async (ev) => {
        ev.preventDefault();
        await markWordStatus(w, status, details, listKind);
      });
      return btn;
    };

    menu.appendChild(mkBtn("Mark as KNOWN", "KNOWN"));
    if (listKind === "missing") {
      menu.appendChild(mkBtn("Add as LEARNING", "LEARNING"));
    }
    details.appendChild(menu);
    el.appendChild(details);
  }
}

function applyLocalGapUpdate(word, newStatus) {
  if (!gapsData?.levels) return;
  const row = gapsData.levels.find((r) => r.level === selectedLevel);
  if (!row) return;

  row.missing = row.missing.filter((w) => w !== word);
  row.learning = row.learning.filter((w) => w !== word);
  row.known = row.known.filter((w) => w !== word);

  if (newStatus === "KNOWN") {
    row.known.push(word);
    row.known.sort();
  } else if (newStatus === "LEARNING") {
    row.learning.push(word);
    row.learning.sort();
  } else {
    row.missing.push(word);
    row.missing.sort();
  }

  row.known_count = row.known.length;
  row.learning_count = row.learning.length;
  row.missing_count = row.missing.length;
}

async function markWordStatus(word, status, detailsEl, listKind) {
  const lang = document.getElementById("lang").value.trim() || "zh";
  detailsEl.classList.add("pending");
  try {
    const res = await fetch("/api/word/status", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ word, lang, status }),
    });
    const data = await res.json();
    if (!res.ok || data.error) {
      throw new Error(data.error || `HTTP ${res.status}`);
    }
    applyLocalGapUpdate(word, status);
    renderLevelPills();
    renderGapsDetail();
  } catch (err) {
    alert(`Could not update Migaku: ${err.message}`);
    detailsEl.classList.remove("pending");
    detailsEl.open = true;
  }
}

async function copyText(text, btn) {
  try {
    await navigator.clipboard.writeText(text);
    if (btn) {
      const prev = btn.textContent;
      btn.textContent = "Copied!";
      setTimeout(() => { btn.textContent = prev; }, 800);
    }
  } catch {
    /* clipboard blocked */
  }
}

function renderLevelPills() {
  const container = document.getElementById("level-pills");
  container.replaceChildren();
  if (!gapsData?.levels) return;
  for (const row of gapsData.levels) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "level-pill" + (row.level === selectedLevel ? " active" : "");
    btn.innerHTML = `L${row.level}<span class="pill-sub">${row.missing_count} missing</span>`;
    btn.addEventListener("click", () => {
      selectedLevel = row.level;
      renderLevelPills();
      renderGapsDetail();
    });
    container.appendChild(btn);
  }
}

function renderGapsDetail() {
  if (!gapsData?.levels) return;
  const row = gapsData.levels.find((r) => r.level === selectedLevel) || gapsData.levels[0];
  if (!row) return;
  selectedLevel = row.level;

  const modeLabel = gapsData.mode === "exclusive" ? "exclusive" : "inclusive";
  document.getElementById("gaps-title").textContent =
    `${gapsData.label} Level ${row.level} (${modeLabel})`;
  document.getElementById("gaps-stats").textContent =
    `${row.known_count} known · ${row.learning_count} learning · ${row.missing_count} missing · ${row.total} total`;

  document.getElementById("missing-count").textContent = row.missing_count;
  document.getElementById("learning-count").textContent = row.learning_count;

  const copyMissing = document.getElementById("copy-missing");
  const copyLearning = document.getElementById("copy-learning");
  copyMissing.disabled = !row.missing.length;
  copyLearning.disabled = !row.learning.length;
  copyMissing.onclick = () => copyText(row.missing.join(", "), copyMissing);
  copyLearning.onclick = () => copyText(row.learning.join(", "), copyLearning);

  renderWordGrid("missing-words", row.missing, "missing");
  renderWordGrid("learning-words", row.learning, "learning");
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

async function loadGaps(lang) {
  const err = document.getElementById("gaps-error");
  err.classList.add("hidden");
  const standard = document.getElementById("gaps-standard").value;
  const mode = document.getElementById("gaps-mode").value;
  const url = `/api/hsk/gaps?lang=${encodeURIComponent(lang)}&standard=${standard}&mode=${mode}`;
  const res = await fetch(url);
  gapsData = await res.json();
  if (gapsData.error) {
    err.textContent = gapsData.error;
    err.classList.remove("hidden");
    return;
  }
  if (!gapsData.levels.some((r) => r.level === selectedLevel)) {
    selectedLevel = gapsData.levels[0]?.level ?? 1;
  }
  renderLevelPills();
  renderGapsDetail();
}

async function load() {
  const lang = document.getElementById("lang").value.trim() || "zh";
  gapsData = null;
  await Promise.all([loadProgress(lang), loadHsk(lang)]);
  const missingTab = document.getElementById("tab-missing");
  if (missingTab.classList.contains("active")) {
    await loadGaps(lang);
  }
}

document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    switchTab(tab.dataset.tab);
    if (tab.dataset.tab === "missing") {
      loadGaps(document.getElementById("lang").value.trim() || "zh");
    }
  });
});

document.getElementById("gaps-standard").addEventListener("change", () => {
  gapsData = null;
  loadGaps(document.getElementById("lang").value.trim() || "zh");
});
document.getElementById("gaps-mode").addEventListener("change", () => {
  gapsData = null;
  loadGaps(document.getElementById("lang").value.trim() || "zh");
});
document.getElementById("refresh").addEventListener("click", load);
document.getElementById("lang").addEventListener("change", load);

const params = new URLSearchParams(location.search);
if (params.get("lang")) document.getElementById("lang").value = params.get("lang");
if (params.get("tab") === "missing") switchTab("missing");
load();
