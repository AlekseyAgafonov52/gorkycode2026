import { createApi } from "./api.js";

const COLORS = {
  green: { label: "Подходит" },
  yellow: { label: "Частично подходит" },
  red: { label: "Навык не указан" },
};
const STATUS = {
  new: "Новый",
  forwarded: "Созвонились, передаю",
  not_forwarded: "Созвонились, не передаю",
};
const LEVELS = { advanced: "Продвинутый", intermediate: "Средний", basic: "Базовый" };
const api = createApi(window.HR_CONFIG);
const app = document.getElementById("app");
const state = { vacancies: [], filter: "all", vacancyId: null, report: null,
  viewFilter: "all", navigation: 0, controller: null, saving: new Set(),
  reviewing: new Set(), reviewNotices: new Map(), reviewWrites: new Map() };

const esc = (value) => String(value ?? "").replace(/[&<>"']/g,
  (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));
const colorOf = (value) => Object.hasOwn(COLORS, value) ? value : "grey";
const share = (value) => new Intl.NumberFormat("ru-RU", { style: "percent", maximumFractionDigits: 1 }).format(value);
const vacancyLink = (id) => `#/v/${encodeURIComponent(id)}`;
const reportLink = (vacancyId, id) => `${vacancyLink(vacancyId)}/r/${encodeURIComponent(id)}`;
const dot = (color) => `<span class="dot ${colorOf(color)}" role="img" aria-label="${esc(COLORS[color]?.label || "Цвет не определён")}"></span>`;

function applyRecentViewed(item, requestStarted) {
  const saved = state.reviewWrites.get(item.id);
  if (saved && saved.at >= requestStarted) item.is_viewed = saved.value;
}

function tabs(selectedId) {
  return `<nav class="tabs" aria-label="Вакансии">${state.vacancies.map((vacancy) =>
    `<a class="tab ${vacancy.id === selectedId ? "on" : ""}" ${vacancy.id === selectedId ? 'aria-current="page"' : ""}
      href="${vacancyLink(vacancy.id)}">${esc(vacancy.title)} · ${vacancy.responses_count}</a>`).join("")}</nav>`;
}

function message(title, text, action = "refresh") {
  app.setAttribute("aria-busy", "false");
  app.innerHTML = `${tabs(state.vacancyId)}<section class="msg" role="status"><h1>${esc(title)}</h1>
    <p>${esc(text)}</p><button class="act" data-action="${action}">Повторить</button></section>`;
}

function loading(text) {
  app.setAttribute("aria-busy", "true");
  app.innerHTML = `${tabs(state.vacancyId)}<p role="status">${esc(text)}</p>`;
}

function renderList(vacancy, items) {
  vacancy.responses_count = items.length;
  const counts = { all: items.length, green: 0, yellow: 0, red: 0 };
  for (const item of items) counts[item.match_status]++;
  const viewedCount = items.filter((item) => item.is_viewed).length;
  const viewCounts = { all: items.length, viewed: viewedCount, unviewed: items.length - viewedCount };
  const shown = items.filter((item) => (state.filter === "all" || item.match_status === state.filter)
    && (state.viewFilter === "all" || item.is_viewed === (state.viewFilter === "viewed")));
  const chips = [["all", "Все"], ["green", COLORS.green.label], ["yellow", COLORS.yellow.label]]
    .map(([key, label]) => `<button class="chip ${state.filter === key ? "on" : ""}" data-filter="${key}"
      aria-pressed="${state.filter === key}">${key === "all" ? "" : dot(key) + " "}${esc(label)} ${counts[key]}</button>`).join("");
  const viewChips = [["all", "Все"], ["unviewed", "Не просмотрено"], ["viewed", "Просмотрено"]]
    .map(([key, label]) => `<button class="chip ${state.viewFilter === key ? "on" : ""}" data-view-filter="${key}"
      aria-pressed="${state.viewFilter === key}">${label} ${viewCounts[key]}</button>`).join("");
  const cards = shown.map((item) => `<article class="card" data-card-id="${esc(item.id)}">
    <a class="card-link" href="${reportLink(vacancy.id, item.id)}">
      ${dot(item.match_status)}
      <div class="name">${esc(item.candidate_name || "ФИО не указано")}</div>
      <div class="sub">Совпадение навыков: ${share(item.match_share)}</div>
      ${item.short_summary ? `<div class="sub">${esc(item.short_summary)}</div>` : ""}
      ${item.workflow_status !== "new" ? `<div class="mark">${esc(STATUS[item.workflow_status])}</div>` : ""}
      <div class="review-badge">${item.is_viewed ? "Просмотрено" : "Не просмотрено"}</div>
    </a>${reviewControls(item)}</article>`).join("");
  app.setAttribute("aria-busy", "false");
  app.innerHTML = `${tabs(vacancy.id)}<div class="bar"><h1>${esc(vacancy.title)}</h1>
    <span class="grow"></span><button class="act" data-action="refresh">Обновить</button></div>
    <div class="chips" aria-label="Фильтр по цвету">${chips}</div>
    <div class="chips" aria-label="Фильтр по просмотру">${viewChips}</div>
    <div class="grid">${cards || '<p role="status">Нет откликов по выбранному фильтру.</p>'}</div>`;
  state.items = items;
}

function reviewControls(item) {
  const pending = state.reviewing.has(item.id);
  const notice = state.reviewNotices.get(item.id);
  return `<div class="review-block" data-review-block="${esc(item.id)}">
    <div class="review-controls" role="group" aria-label="Просмотр отклика">
      ${[[true, "Просмотрено"], [false, "Не просмотрено"]].map(([value, label]) =>
        `<button class="act ${item.is_viewed === value ? "selected" : ""}" data-response-id="${esc(item.id)}" data-viewed="${value}"
          aria-pressed="${item.is_viewed === value}" ${pending ? "disabled" : ""}>${label}</button>`).join("")}
    </div><p class="review-notice ${notice?.error ? "error" : ""}" role="status" aria-live="polite">${pending ? "Сохраняем…" : esc(notice?.text || "")}</p>
  </div>`;
}

function refreshReviewControls(identifier) {
  const current = state.report?.id === identifier ? state.report : state.items?.find((item) => item.id === identifier);
  if (!current) return;
  if (state.report?.id === identifier) {
    const block = app.querySelector(`[data-review-block="${identifier}"]`);
    if (block) block.outerHTML = reviewControls(current);
    const badge = app.querySelector("[data-report-viewed]");
    if (badge) badge.textContent = current.is_viewed ? "Просмотрено" : "Не просмотрено";
  } else if (!state.report && app.querySelector(".grid")) {
    const vacancy = state.vacancies.find((item) => item.id === state.vacancyId);
    if (vacancy && state.items) renderList(vacancy, state.items);
  }
}

async function saveViewed(identifier, viewed) {
  if (state.reviewing.has(identifier)) return;
  state.reviewing.add(identifier);
  state.reviewNotices.delete(identifier);
  refreshReviewControls(identifier);
  try {
    const result = await api.updateViewed(identifier, viewed);
    if (result.id !== identifier || typeof result.is_viewed !== "boolean") {
      throw new Error("Не удалось подтвердить отметку просмотра.");
    }
    state.reviewWrites.set(identifier, { value: result.is_viewed, at: performance.now() });
    if (state.report?.id === identifier) state.report.is_viewed = result.is_viewed;
    const item = state.items?.find((item) => item.id === identifier);
    if (item) item.is_viewed = result.is_viewed;
    state.reviewNotices.set(identifier, { text: "Отметка сохранена.", error: false });
  } catch (error) {
    state.reviewNotices.set(identifier, { text: error.message, error: true });
  } finally {
    state.reviewing.delete(identifier);
    refreshReviewControls(identifier);
  }
}

function contactLink(label, value, href) {
  return value ? `<span>${esc(label)}: <a href="${esc(href)}" target="_blank" rel="noopener noreferrer">${esc(value)}</a></span>` : "";
}

function renderContacts(contacts) {
  const other = contacts.other.map((value) => {
    try {
      const url = new URL(value);
      if (["https:", "http:"].includes(url.protocol)) return contactLink("Ссылка", value, url.href);
    } catch { /* A non-URL contact is displayed as plain text. */ }
    return `<span>${esc(value)}</span>`;
  });
  const content = [
    contactLink("Телефон", contacts.phone, `tel:${encodeURIComponent(contacts.phone || "")}`),
    contactLink("Email", contacts.email, `mailto:${encodeURIComponent(contacts.email || "")}`),
    contactLink("Telegram", contacts.telegram, `https://t.me/${encodeURIComponent((contacts.telegram || "").replace(/^@/, ""))}`),
    ...other,
  ].filter(Boolean).join("");
  return content || '<span class="muted">Контакты не указаны</span>';
}

function renderQuestion(question) {
  return `<li><span class="question-kind">${question.kind === "missing" ? "Уточнение опыта" : "Вопрос по навыку"}</span>
    <div>${esc(question.question)}</div>
    ${question.expected_answer ? `<details><summary>Ориентир ответа</summary><p class="expected">${esc(question.expected_answer)}</p></details>` : ""}
    ${question.follow_up_hint ? `<p class="muted">${esc(question.follow_up_hint)}</p>` : ""}</li>`;
}

function renderReport(report, vacancy) {
  app.setAttribute("aria-busy", "false");
  app.innerHTML = `${tabs(vacancy.id)}<div class="bar"><a href="${vacancyLink(vacancy.id)}">К списку откликов</a></div>
    <section class="panel">
      <h1>${esc(report.candidate_name || "ФИО не указано")}</h1>
      <div class="bar">${dot(report.match_status)}<b>${esc(report.match_label)}</b>
        <span>Совпадение навыков: ${share(report.match_share)}</span></div>
      <div class="contact-list">${renderContacts(report.contacts)}</div>
      <p class="review-badge" data-report-viewed>${report.is_viewed ? "Просмотрено" : "Не просмотрено"}</p>
      ${reviewControls(report)}
    </section>
    <section class="panel"><h2>Итог</h2><ul class="summary-lines">${report.summary.map((line) => `<li>${esc(line)}</li>`).join("")}</ul></section>
    <section class="panel"><h2>Навыки и вопросы</h2>${report.skills.map((skill) => `<article class="skill">
      <div class="skill-heading">${dot(skill.relevance)}<h3>${esc(skill.skill)}</h3>
        <span class="level">${esc(LEVELS[skill.resume_level] || "Уровень не указан")}</span></div>
      <p>${esc(skill.explanation)}</p>
      ${skill.questions.length ? `<ol class="questions">${skill.questions.map(renderQuestion).join("")}</ol>` : ""}
    </article>`).join("") || '<p class="muted">Навыки вакансии не указаны.</p>'}</section>
    <section class="panel"><h2>Недостающие сведения</h2>${report.missing_data.length
      ? `<ul>${report.missing_data.map((item) => `<li>${esc(item.text)}</li>`).join("")}</ul>`
      : '<p class="muted">Нет уточнений по основным сведениям.</p>'}</section>
    <section class="panel"><h2>Результат звонка</h2><p>Статус: <b data-current-status></b></p>
      <div class="bar actions">
        <button class="act main" data-workflow="forwarded">Созвонились, передаю</button>
        <button class="act" data-workflow="not_forwarded">Созвонились, не передаю</button>
        <button class="act" data-workflow="new">Сбросить отметку</button>
      </div><p class="notice" data-save-notice role="status" aria-live="polite"></p></section>
    <section class="panel"><h2>Исходный текст резюме</h2><pre class="source" data-source></pre></section>`;
  app.querySelector("[data-source]").textContent = report.source_text;
  updateStatusControls();
}

function updateStatusControls(notice = "", isError = false) {
  const report = state.report;
  if (!report) return;
  const current = app.querySelector("[data-current-status]");
  if (!current) return;
  const saving = state.saving.has(report.id);
  current.textContent = STATUS[report.workflow_status] || "Статус не указан";
  app.querySelectorAll("[data-workflow]").forEach((button) => {
    const selected = button.dataset.workflow === report.workflow_status;
    button.disabled = saving;
    button.classList.toggle("selected", selected);
    button.setAttribute("aria-pressed", String(selected));
  });
  const output = app.querySelector("[data-save-notice]");
  output.textContent = saving ? "Сохраняем…" : notice;
  output.className = `notice ${isError ? "error" : notice ? "success" : ""}`;
}

async function saveStatus(status) {
  const report = state.report;
  if (!report || state.saving.has(report.id) || !Object.hasOwn(STATUS, status)) return;
  state.saving.add(report.id);
  updateStatusControls();
  let notice;
  let failed = false;
  try {
    const result = await api.updateStatus(report.id, status);
    if (result.id !== report.id || !Object.hasOwn(STATUS, result.workflow_status)) {
      throw new Error("Не удалось подтвердить сохранение статуса.");
    }
    report.workflow_status = result.workflow_status;
    if (state.report?.id === report.id) state.report.workflow_status = result.workflow_status;
    notice = "Статус сохранён.";
  } catch (error) {
    failed = true;
    notice = error.message;
  } finally {
    state.saving.delete(report.id);
    if (state.report?.id === report.id) updateStatusControls(notice, failed);
  }
}

async function route() {
  const navigation = ++state.navigation;
  state.controller?.abort();
  state.controller = new AbortController();
  const { signal } = state.controller;
  state.report = null;
  let vacancyId, responseId;
  try {
    const parts = location.hash.match(/^#\/v\/([^/]+)(?:\/r\/([^/]+))?\/?$/);
    if (!parts) {
      if (!location.hash && state.vacancies.length) {
        location.replace(vacancyLink(state.vacancies[0].id));
        return;
      }
      throw new Error("Страница не найдена. Выберите вакансию.");
    }
    vacancyId = decodeURIComponent(parts[1]);
    responseId = parts[2] ? decodeURIComponent(parts[2]) : null;
    // Existing bookmarks in the prototype used a numeric vacancy tab index.
    if (!state.vacancies.some((v) => v.id === vacancyId) && /^\d+$/.test(vacancyId)) {
      const legacy = state.vacancies[Number(vacancyId)];
      if (legacy) {
        location.replace(responseId ? reportLink(legacy.id, responseId) : vacancyLink(legacy.id));
        return;
      }
    }
    const vacancy = state.vacancies.find((item) => item.id === vacancyId);
    if (!vacancy) throw new Error("Вакансия не найдена. Выберите другую вакансию.");
    if (state.vacancyId !== vacancyId) {
      state.filter = "all";
      state.viewFilter = "all";
    }
    state.vacancyId = vacancyId;
    loading(responseId ? "Загрузка отчёта…" : "Загрузка откликов…");
    const requestStarted = performance.now();
    if (responseId) {
      const report = await api.getReport(responseId, { signal });
      if (navigation !== state.navigation) return;
      if (report.vacancy_id !== vacancyId) throw new Error("Отклик относится к другой вакансии.");
      applyRecentViewed(report, requestStarted);
      state.report = report;
      renderReport(report, vacancy);
    } else {
      const items = await api.listResponses(vacancyId, { signal });
      if (navigation !== state.navigation) return;
      items.forEach((item) => applyRecentViewed(item, requestStarted));
      renderList(vacancy, items);
    }
    window.scrollTo(0, 0);
  } catch (error) {
    if (signal.aborted || navigation !== state.navigation) return;
    message("Не удалось загрузить данные", error.message);
  }
}

async function start() {
  loading("Загрузка вакансий…");
  try {
    const vacancies = await api.listVacancies();
    if (!Array.isArray(vacancies)) throw new Error("Не удалось прочитать список вакансий.");
    state.vacancies = vacancies;
    if (!vacancies.length) {
      message("Вакансий пока нет", "После загрузки вакансий они появятся здесь.", "start");
      return;
    }
    window.onhashchange = route;
    await route();
  } catch (error) {
    message("Не удалось загрузить вакансии", error.message, "start");
  }
}

app.addEventListener("click", (event) => {
  const button = event.target instanceof Element ? event.target.closest("button") : null;
  if (!button || button.disabled) return;
  if (button.dataset.action === "start") start();
  else if (button.dataset.action === "refresh") route();
  else if (button.dataset.filter) {
    state.filter = button.dataset.filter;
    const vacancy = state.vacancies.find((item) => item.id === state.vacancyId);
    if (vacancy && state.items) renderList(vacancy, state.items);
  } else if (button.dataset.viewFilter) {
    state.viewFilter = button.dataset.viewFilter;
    const vacancy = state.vacancies.find((item) => item.id === state.vacancyId);
    if (vacancy && state.items) renderList(vacancy, state.items);
  } else if (button.dataset.viewed !== undefined && button.dataset.responseId) {
    saveViewed(button.dataset.responseId, button.dataset.viewed === "true");
  } else if (button.dataset.workflow) saveStatus(button.dataset.workflow);
});

start();
