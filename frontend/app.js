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
  reviewing: new Set(), reviewNotices: new Map(), reviewWrites: new Map(),
  sourceDrafts: new Map(), sourceWrites: new Map(), sourceNotices: new Map(), recommendationDrafts: new Map(), vacancySaving: new Set() };

const esc = (value) => String(value ?? "").replace(/[&<>"']/g,
  (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));
const colorOf = (value) => Object.hasOwn(COLORS, value) ? value : "grey";
const share = (value) => new Intl.NumberFormat("ru-RU", { style: "percent", maximumFractionDigits: 1 }).format(value);
const vacancyLink = (id) => `#/v/${encodeURIComponent(id)}`;
const reportLink = (vacancyId, id) => `${vacancyLink(vacancyId)}/r/${encodeURIComponent(id)}`;
const dot = (color) => `<span class="dot ${colorOf(color)}" role="img" aria-label="${esc(COLORS[color]?.label || "Цвет не определён")}"></span>`;
const editorText = (value) => value.replace(/\r\n?/g, "\n");

function applyRecentViewed(item, requestStarted) {
  const saved = state.reviewWrites.get(item.id);
  if (saved && saved.at >= requestStarted) item.is_viewed = saved.value;
}

function applyRecentSource(report, requestStarted) {
  const saved = state.sourceWrites.get(report.id);
  if (saved && saved.at >= requestStarted) {
    return { ...saved.report, workflow_status: report.workflow_status, is_viewed: report.is_viewed,
      is_closed: report.is_closed, closed_at: report.closed_at, hired_at: report.hired_at,
      recruiter_recommendation: report.recruiter_recommendation };
  }
  if (saved) state.sourceWrites.delete(report.id);
  return report;
}

function tabs(selectedId) {
  return `<nav class="tabs" aria-label="Вакансии">${state.vacancies.map((vacancy) =>
    `<a class="tab ${vacancy.id === selectedId ? "on" : ""}" ${vacancy.id === selectedId ? 'aria-current="page"' : ""}
      href="${vacancyLink(vacancy.id)}">${esc(vacancy.title)} · ${vacancy.responses_count}${vacancy.status === "closed" ? " · Закрыта" : vacancy.hired_candidates?.length ? " · Найм подтверждён" : ""}</a>`).join("")}</nav>`;
}

function hiringBanner(vacancy) {
  const hired = vacancy.hired_candidates || [];
  if (!hired.length) return "";
  return `<section class="hiring-banner" role="status"><b>Департамент подтвердил подписание трудового договора</b>
    <ul>${hired.map((candidate) => `<li><a href="${reportLink(vacancy.id, candidate.id)}">${esc(candidate.candidate_name || "ФИО не указано")}</a></li>`).join("")}</ul>
    <p>${vacancy.status === "closed" ? "Вакансия закрыта HR." : "Проверьте результат найма и закройте вакансию."}</p></section>`;
}

function vacancyControl(vacancy) {
  return `<button class="act" data-vacancy-status="${vacancy.status === "closed" ? "active" : "closed"}" ${state.vacancySaving.has(vacancy.id) ? "disabled" : ""}>${vacancy.status === "closed" ? "Открыть вакансию заново" : "Закрыть вакансию"}</button>`;
}

const RECOMMENDATION_FIELDS = [["strengths", "Сильные стороны (необязательно)"], ["weaknesses", "Слабые места (необязательно)"], ["interview_focus", "Что проверить на собеседовании (необязательно)"]];

function recommendationForm(report) {
  const value = state.recommendationDrafts.get(report.id) || report.recruiter_recommendation || {};
  return `<div class="recommendation-form">${RECOMMENDATION_FIELDS.map(([key, label]) =>
    `<label>${label}<textarea data-recommendation-field="${key}" maxlength="800" rows="3">${esc(value[key] || "")}</textarea></label>`).join("")}
    <button class="act" data-action="save-recommendation">Сохранить рекомендации</button></div>`;
}

function readRecommendation() {
  return Object.fromEntries([...app.querySelectorAll("[data-recommendation-field]")]
    .map((input) => [input.dataset.recommendationField, input.value.trim() || null]));
}

async function saveRecommendation() {
  const report = state.report;
  if (!report || state.saving.has(report.id)) return;
  const recommendation = readRecommendation();
  state.saving.add(report.id);
  updateStatusControls();
  let notice = "Рекомендации сохранены.", failed = false;
  try {
    const result = await api.updateRecommendation(report.id, recommendation);
    report.recruiter_recommendation = result.recruiter_recommendation;
    state.recommendationDrafts.delete(report.id);
    if (report.workflow_status === "forwarded") notice += " Бот доставит обновление руководителю.";
  } catch (error) {
    notice = error.message;
    failed = true;
  } finally {
    state.saving.delete(report.id);
    if (state.report?.id === report.id) updateStatusControls(notice, failed);
  }
}

async function saveVacancyStatus(status) {
  const vacancy = state.vacancies.find((item) => item.id === state.vacancyId);
  if (!vacancy || state.vacancySaving.has(vacancy.id)) return;
  const action = status === "closed" ? "Закрыть" : "Открыть заново";
  if (!window.confirm(`${action} вакансию «${vacancy.title}»? Отметки найма кандидатов сохранятся.`)) return;
  state.vacancySaving.add(vacancy.id);
  app.querySelectorAll("[data-vacancy-status]").forEach((button) => { button.disabled = true; });
  try {
    await api.updateVacancyStatus(vacancy.id, status);
  } catch (error) {
    window.alert(error.message);
  } finally {
    state.vacancySaving.delete(vacancy.id);
    if (state.vacancyId === vacancy.id) await route();
  }
}

// Страница вакансий: названия строками через точку, сначала открытые, потом закрытые.
// Бэк пока не отдаёт статус вакансии, поэтому без поля status вакансия считается открытой.
function renderVacancies() {
  const open = state.vacancies.filter((vacancy) => vacancy.status !== "closed");
  const closed = state.vacancies.filter((vacancy) => vacancy.status === "closed");
  const rows = (list) => list.length
    ? list.map((vacancy, i) => `<span class="vac"><a href="${vacancyLink(vacancy.id)}">${esc(vacancy.title)}</a>${
      i < list.length - 1 ? '<span class="vac-sep" aria-hidden="true"></span>' : ""}</span> `).join("")
    : '<span class="vac-empty">Пока нет</span>';
  const group = (title, list, extra = "") => `<section class="vac-group ${extra}"><h2>${title}</h2><hr>
    <div class="vac-rows">${rows(list)}</div></section>`;
  app.setAttribute("aria-busy", "false");
  app.innerHTML = group("Открытые вакансии", open) + group("Закрытые вакансии", closed, "closed");
  hideLineEndDots();
}

// Точка нужна только между вакансиями в одной строке: в конце строки прячем её.
function hideLineEndDots() {
  app.querySelectorAll(".vac-rows").forEach((rows) => {
    const items = [...rows.querySelectorAll(".vac")];
    items.forEach((item, i) => {
      const dot = item.querySelector(".vac-sep");
      if (!dot) return;
      dot.classList.toggle("hidden", Boolean(items[i + 1]) && items[i + 1].offsetTop > item.offsetTop);
    });
  });
}
window.addEventListener("resize", hideLineEndDots);

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
      ${item.hired_at ? '<div class="hiring-badge">Найм закрыт · договор подписан</div>' : item.is_closed ? '<div class="closure-badge">Работа в департаменте завершена</div>' : ""}
    </a>${reviewControls(item)}</article>`).join("");
  app.setAttribute("aria-busy", "false");
  app.innerHTML = `${tabs(vacancy.id)}<div class="bar"><h1>${esc(vacancy.title)}</h1>
    <span class="grow"></span>${vacancyControl(vacancy)}<button class="act" data-action="refresh">Обновить</button></div>
    ${vacancy.status === "closed" ? '<p class="closure-badge">Вакансия закрыта HR</p>' : ""}
    ${hiringBanner(vacancy)}
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

function renderPdfReport(report) {
  if (!report.pdf_report) return "";
  const url = esc(api.pdfReportUrl(report.id));
  const download = esc(api.pdfReportUrl(report.id, true));
  return `<section class="panel pdf-panel" aria-label="PDF-отчёт по отклику">
    <div class="bar"><h2>PDF-отчёт</h2><span class="grow"></span>
      <a class="act" href="${url}" target="_blank" rel="noopener noreferrer">Открыть PDF</a>
      <a class="act" href="${download}">Скачать PDF</a></div>
    ${report.pdf_report.is_stale ? '<p class="pdf-stale" role="status">Текст резюме изменён после создания PDF. В документе сохранена предыдущая версия отчёта.</p>' : ""}
    <iframe class="pdf-viewer" src="${url}#view=FitH" title="PDF-отчёт по текущему отклику" loading="lazy"></iframe>
    <p class="muted">Если документ не отображается, откройте или скачайте PDF по кнопкам выше.</p></section>`;
}

function renderReport(report, vacancy) {
  app.setAttribute("aria-busy", "false");
  app.innerHTML = `${tabs(vacancy.id)}<div class="bar"><a href="${vacancyLink(vacancy.id)}">К списку откликов</a>
    <span class="grow"></span>${vacancyControl(vacancy)}<button class="act" data-action="refresh">Обновить</button></div>
    ${hiringBanner(vacancy)}
    <section class="panel">
      <h1>${esc(report.candidate_name || "ФИО не указано")}</h1>
      ${vacancy.status === "closed" ? '<p class="closure-badge">Вакансия закрыта HR</p>' : ""}
      ${report.hired_at ? '<p class="hiring-badge">Найм закрыт · трудовой договор подписан</p>' : report.is_closed ? '<p class="closure-badge">Работа с откликом в департаменте завершена</p>' : ""}
      <div class="bar">${dot(report.match_status)}<b>${esc(report.match_label)}</b>
        <span>Совпадение навыков: ${share(report.match_share)}</span></div>
      <div class="contact-list">${renderContacts(report.contacts)}</div>
      <p class="review-badge" data-report-viewed>${report.is_viewed ? "Просмотрено" : "Не просмотрено"}</p>
      ${reviewControls(report)}
    </section>
    <section class="panel"><h2>Итог</h2><ul class="summary-lines">${report.summary.map((line) => `<li>${esc(line)}</li>`).join("")}</ul></section>
    ${renderPdfReport(report)}
    <section class="panel"><h2>Навыки и вопросы</h2>${report.skills.map((skill) => `<article class="skill">
      <div class="skill-heading">${dot(skill.relevance)}<h3>${esc(skill.skill)}</h3>
        <span class="level">${esc(LEVELS[skill.resume_level] || "Уровень не указан")}</span></div>
      <p>${esc(skill.explanation)}</p>
      ${skill.questions.length ? `<ol class="questions">${skill.questions.map(renderQuestion).join("")}</ol>` : ""}
    </article>`).join("") || '<p class="muted">Навыки вакансии не указаны.</p>'}</section>
    <section class="panel"><h2>Недостающие сведения</h2>${report.missing_data.length
      ? `<ul>${report.missing_data.map((item) => `<li>${esc(item.text)}</li>`).join("")}</ul>`
      : '<p class="muted">Нет уточнений по основным сведениям.</p>'}</section>
    <section class="panel"><h2>Передача в департамент</h2>
      <p>Рекомендации рекрутера будут показаны руководителю в Telegram.</p>${recommendationForm(report)}
      <p>Статус звонка: <b data-current-status></b></p>
      <div class="bar actions">
        <button class="act main" data-workflow="forwarded">Созвонились, передаю</button>
        <button class="act" data-workflow="not_forwarded">Созвонились, не передаю</button>
        <button class="act" data-workflow="new">Сбросить отметку</button>
      </div><p class="notice" data-save-notice role="status" aria-live="polite"></p></section>
    <section class="panel" data-source-panel>
      <div class="source-heading"><h2>Исходный текст резюме</h2>
        <button class="icon-button" type="button" data-action="edit-source" title="Редактировать текст резюме"
          aria-label="Редактировать текст резюме" aria-controls="source-editor" aria-expanded="false">
          <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <path d="M16 3a2.1 2.1 0 0 1 3 3L8 17l-4 1 1-4Z"/><path d="m14 5 3 3M4 22h16"/>
          </svg></button></div>
      <pre class="source" data-source></pre>
      <div id="source-editor" class="source-editor" data-source-editor hidden>
        <label for="source-text-input" class="muted">Текст резюме</label>
        <textarea id="source-text-input" class="source-input" data-source-input rows="18" maxlength="2097152" spellcheck="false"></textarea>
        <p class="muted">После сохранения данные отчёта обновятся.</p>
        <div class="bar"><button class="act main" type="button" data-action="save-source">Сохранить</button>
          <button class="act" type="button" data-action="cancel-source">Отмена</button></div>
      </div><p class="notice" data-source-notice role="status" aria-live="polite"></p>
    </section>`;
  app.querySelector("[data-source]").textContent = report.source_text;
  renderSourceEditor();
  updateStatusControls();
}

function renderSourceEditor() {
  const report = state.report;
  const editor = app.querySelector("[data-source-editor]");
  if (!report || !editor) return;
  const draft = state.sourceDrafts.get(report.id);
  app.querySelector("[data-source]").hidden = Boolean(draft);
  editor.hidden = !draft;
  const pencil = app.querySelector('[data-action="edit-source"]');
  pencil.hidden = Boolean(draft);
  pencil.setAttribute("aria-expanded", String(Boolean(draft)));
  const input = app.querySelector("[data-source-input]");
  if (draft && input.value !== draft.text) input.value = draft.text;
  input.readOnly = Boolean(draft?.saving);
  editor.querySelectorAll("button").forEach((button) => { button.disabled = Boolean(draft?.saving); });
  const output = app.querySelector("[data-source-notice]");
  output.textContent = draft?.saving ? "Сохраняем текст и обновляем отчёт…" : draft?.error || state.sourceNotices.get(report.id) || "";
  output.className = `notice ${draft?.error ? "error" : !draft && output.textContent ? "success" : ""}`;
}

function editSource() {
  const report = state.report;
  if (!report) return;
  if (!state.sourceDrafts.has(report.id)) {
    state.sourceDrafts.set(report.id, { id: report.id, originalText: report.source_text, text: editorText(report.source_text), saving: false, error: "" });
  }
  state.sourceNotices.delete(report.id);
  renderSourceEditor();
  app.querySelector("[data-source-input]").focus();
}

function cancelSource() {
  const report = state.report;
  if (!report || state.sourceDrafts.get(report.id)?.saving) return;
  state.sourceDrafts.delete(report.id);
  state.sourceNotices.delete(report.id);
  renderSourceEditor();
  app.querySelector('[data-action="edit-source"]').focus();
}

async function saveSource() {
  const report = state.report;
  const draft = report && state.sourceDrafts.get(report.id);
  if (!draft || draft.saving) return;
  draft.text = app.querySelector("[data-source-input]").value;
  if (!draft.text.trim()) {
    draft.error = "Введите непустой текст резюме.";
    renderSourceEditor();
    return;
  }
  if (draft.text === editorText(draft.originalText)) {
    cancelSource();
    return;
  }
  const requestStarted = performance.now();
  draft.saving = true;
  draft.error = "";
  renderSourceEditor();
  try {
    const updated = await api.updateSourceText(draft.id, draft.text, draft.originalText);
    if (updated.id !== draft.id || updated.source_text !== draft.text) throw new Error("Не удалось подтвердить сохранение текста.");
    applyRecentViewed(updated, requestStarted);
    state.sourceWrites.set(draft.id, { report: updated, at: performance.now() });
    state.sourceDrafts.delete(draft.id);
    state.sourceNotices.set(draft.id, "Текст сохранён. Отчёт обновлён.");
    if (state.report?.id === draft.id) {
      state.report = updated;
      const vacancy = state.vacancies.find((item) => item.id === updated.vacancy_id);
      renderReport(updated, vacancy);
      app.querySelector("[data-source-panel]").scrollIntoView({ block: "start" });
      app.querySelector('[data-action="edit-source"]').focus({ preventScroll: true });
    } else if (!state.report && state.items?.some((item) => item.id === draft.id)) {
      await route();
    }
  } catch (error) {
    draft.saving = false;
    draft.error = error.message;
    if (state.report?.id === draft.id) renderSourceEditor();
  }
}

function updateStatusControls(notice = "", isError = false) {
  const report = state.report;
  if (!report) return;
  const current = app.querySelector("[data-current-status]");
  if (!current) return;
  const saving = state.saving.has(report.id);
  app.querySelectorAll("[data-recommendation-field], [data-action='save-recommendation']").forEach((input) => { input.disabled = saving; });
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
  let recommendation;
  if (status === "forwarded") {
    recommendation = readRecommendation();
  }
  state.saving.add(report.id);
  updateStatusControls();
  let notice;
  let failed = false;
  try {
    const result = await api.updateStatus(report.id, status, recommendation);
    if (result.id !== report.id || !Object.hasOwn(STATUS, result.workflow_status)) {
      throw new Error("Не удалось подтвердить сохранение статуса.");
    }
    report.workflow_status = result.workflow_status;
    if (recommendation) {
      report.recruiter_recommendation = recommendation;
      state.recommendationDrafts.delete(report.id);
    }
    if (state.report?.id === report.id) state.report.workflow_status = result.workflow_status;
    notice = status === "forwarded" ? "Передача сохранена. Отчёт и резюме поставлены в очередь Telegram." : "Статус сохранён.";
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
    const vacancies = await api.listVacancies({ signal });
    if (navigation !== state.navigation) return;
    if (!Array.isArray(vacancies)) throw new Error("Не удалось прочитать список вакансий.");
    state.vacancies = vacancies;
    const parts = location.hash.match(/^#\/v\/([^/]+)(?:\/r\/([^/]+))?\/?$/);
    if (!parts) {
      if (!location.hash || location.hash === "#/") {
        state.vacancyId = null;
        renderVacancies();
        window.scrollTo(0, 0);
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
      let report = await api.getReport(responseId, { signal });
      if (navigation !== state.navigation) return;
      if (report.vacancy_id !== vacancyId) throw new Error("Отклик относится к другой вакансии.");
      report = applyRecentSource(report, requestStarted);
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
  else if (button.dataset.action === "save-recommendation") saveRecommendation();
  else if (button.dataset.vacancyStatus) saveVacancyStatus(button.dataset.vacancyStatus);
  else if (button.dataset.action === "refresh") route();
  else if (button.dataset.action === "edit-source") editSource();
  else if (button.dataset.action === "save-source") saveSource();
  else if (button.dataset.action === "cancel-source") cancelSource();
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

app.addEventListener("input", (event) => {
  const input = event.target;
  if (!(input instanceof HTMLTextAreaElement) || !input.dataset.recommendationField || !state.report) return;
  const draft = { ...(state.recommendationDrafts.get(state.report.id) || state.report.recruiter_recommendation || {}) };
  draft[input.dataset.recommendationField] = input.value;
  state.recommendationDrafts.set(state.report.id, draft);
});

app.addEventListener("input", (event) => {
  if (!(event.target instanceof HTMLTextAreaElement) || !event.target.matches("[data-source-input]")) return;
  const draft = state.report && state.sourceDrafts.get(state.report.id);
  if (draft) draft.text = event.target.value;
});

app.addEventListener("keydown", (event) => {
  if (!(event.target instanceof HTMLTextAreaElement) || !event.target.matches("[data-source-input]")) return;
  if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) { event.preventDefault(); saveSource(); }
  else if (event.key === "Escape") { event.preventDefault(); cancelSource(); }
});

window.addEventListener("beforeunload", (event) => {
  if (state.recommendationDrafts.size || [...state.sourceDrafts.values()].some((draft) => draft.saving || draft.text !== editorText(draft.originalText))) {
    event.preventDefault();
    event.returnValue = "";
  }
});

start();
