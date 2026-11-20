// Прототип фронта для HR. Без сборки и библиотек: данные из файлов отчётов.
const COLORS = {
  green: { label: "Подходит", order: 0 },
  yellow: { label: "Нужно уточнить", order: 1 },
  red: { label: "Не подходит", order: 2 },
};
const STATUS = {
  sent: "Созвонились, отправлен в департамент",
  not_sent: "Созвонились, не отправлен",
};

const app = document.getElementById("app");
const state = { vacancies: [], filter: "all" };

const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const dirOf = (path) => path.slice(0, path.lastIndexOf("/") + 1);
const colorOf = (c) => (COLORS[c] ? c : "grey");

function getStatus(v, id) {
  try { return localStorage.getItem(`hr:${v.title}:${id}`); } catch { return null; }
}
function setStatus(v, id, value) {
  try { localStorage.setItem(`hr:${v.title}:${id}`, value); } catch {}
}

async function loadJson(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`${res.status} ${url}`);
  return res.json();
}

async function loadVacancy(cfg) {
  const ranking = await loadJson(cfg.ranking);
  const items = ranking.applications.map((a) => ({
    id: String(a.application_id),
    color: a.color,
    rank: a.rank,
    pdf: dirOf(cfg.ranking) + a.report,
    title: "",
  }));
  items.sort((a, b) => (COLORS[a.color]?.order ?? 9) - (COLORS[b.color]?.order ?? 9) || a.rank - b.rank);
  const v = { title: ranking.vacancy_title || "Вакансия", items };
  if (cfg.applications) {
    // Должность из резюме — необязательно, ошибки не мешают показу.
    await Promise.all(items.map(async (it) => {
      try {
        const d = await loadJson(`${cfg.applications}/${encodeURIComponent("отклик " + it.id)}.json`);
        it.title = d.resume_title || "";
        it.name = d.full_name || d.name || "";
      } catch {}
    }));
  }
  return v;
}

function route() {
  const m = location.hash.match(/^#\/v\/(\d+)(?:\/r\/([^/]+))?/);
  const vi = m ? Math.min(+m[1], state.vacancies.length - 1) : 0;
  if (m && m[2]) renderReport(vi, decodeURIComponent(m[2]));
  else renderList(vi);
  window.scrollTo(0, 0);
}

function renderList(vi) {
  const v = state.vacancies[vi];
  const counts = { all: v.items.length };
  for (const it of v.items) counts[it.color] = (counts[it.color] || 0) + 1;
  const shown = v.items.filter((it) => state.filter === "all" || it.color === state.filter);

  const tabs = state.vacancies.map((x, i) =>
    `<button class="tab ${i === vi ? "on" : ""}" data-v="${i}">${esc(x.title)} · ${x.items.length}</button>`).join("");
  const chips = [["all", "Все"], ...Object.entries(COLORS).map(([k, c]) => [k, c.label])].map(([k, label]) =>
    `<button class="chip ${state.filter === k ? "on" : ""}" data-f="${k}">${k === "all" ? "" : `<span class="dot ${k}"></span> `}${label} ${counts[k] || 0}</button>`).join("");
  const cards = shown.map((it) => {
    const st = getStatus(v, it.id);
    return `<div class="card" data-r="${esc(it.id)}">
      <span class="dot ${colorOf(it.color)}"></span>
      <div class="name">${esc(it.name || "Отклик " + it.id)}</div>
      <div class="sub">${esc(it.title || "—")}</div>
      ${st ? `<div class="mark">${esc(STATUS[st])}</div>` : ""}
    </div>`;
  }).join("");

  app.innerHTML = `<div class="tabs">${tabs}</div><div class="chips">${chips}</div>
    <div class="grid">${cards || "<p>Нет откликов</p>"}</div>`;
  app.querySelectorAll("[data-v]").forEach((b) => b.onclick = () => { state.filter = "all"; location.hash = `#/v/${b.dataset.v}`; });
  app.querySelectorAll("[data-f]").forEach((b) => b.onclick = () => { state.filter = b.dataset.f; renderList(vi); });
  app.querySelectorAll("[data-r]").forEach((c) => c.onclick = () => { location.hash = `#/v/${vi}/r/${encodeURIComponent(c.dataset.r)}`; });
}

function renderReport(vi, id) {
  const v = state.vacancies[vi];
  const it = v.items.find((x) => x.id === id);
  if (!it) { location.hash = `#/v/${vi}`; return; }
  const st = getStatus(v, id);
  app.innerHTML = `<div class="bar">
      <a href="#/v/${vi}">← ${esc(v.title)}</a>
      <span class="dot ${colorOf(it.color)}"></span>
      <b>${esc(it.name || "Отклик " + it.id)}</b>
      <span>${esc(COLORS[it.color]?.label || "Цвет не определён")}</span>
      <span class="grow"></span>
      ${st ? `<span>${esc(STATUS[st])}</span>` : ""}
      <button class="act main" data-s="sent">Созвонились, отправляю в департамент</button>
      <button class="act" data-s="not_sent">Созвонились, не отправляю</button>
    </div>
    <iframe src="${esc(it.pdf)}" title="Отчёт"></iframe>
    <p><a href="${esc(it.pdf)}" target="_blank">Открыть отчёт в новой вкладке</a></p>`;
  app.querySelectorAll("[data-s]").forEach((b) => b.onclick = () => {
    setStatus(v, id, b.dataset.s);
    location.hash = `#/v/${vi}`;
  });
}

async function start() {
  const list = window.HR_CONFIG?.vacancies || [];
  const results = await Promise.allSettled(list.map(loadVacancy));
  state.vacancies = results.filter((r) => r.status === "fulfilled").map((r) => r.value);
  const errors = results.filter((r) => r.status === "rejected").map((r) => r.reason.message);
  if (!state.vacancies.length) {
    app.innerHTML = `<div class="msg">Не удалось загрузить данные.
${esc(errors.join("\n"))}

Запускайте через сервер из корня репозитория:
python -m http.server 8080
и открывайте http://localhost:8080/frontend/</div>`;
    return;
  }
  if (errors.length) console.warn("Не загрузились вакансии:", errors);
  window.onhashchange = route;
  route();
}

start();
