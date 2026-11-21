import { createApi } from "./api.js";

const MATCH_GROUPS = {
  green: "Высокое соответствие",
  yellow: "Среднее соответствие",
  red: "Отсутствуют необходимые данные",
};
const STATUS = {
  reviewed: "Просмотрено",
  call_pending: "Созвониться",
  not_forwarded: "Созвонились",
  forwarded: "Отправлено",
};
const REPORT_TABS = [["pdf", "Отчёт"], ["review", "Отзыв"], ["resume", "Резюме"]];
const api = createApi(window.HR_CONFIG);
const app = document.getElementById("app");
const vacancyCrumb = document.getElementById("vacancy-crumb");
const responseCrumb = document.getElementById("response-crumb");
const preview = document.getElementById("vacancy-preview");
const previewContent = preview.querySelector(".preview-content");
const previewText = preview.querySelector(".preview-text");
const previewStatus = preview.querySelector(".preview-status");
let previewRequest;
const state = { vacancies: [], vacancyId: null, report: null,
  navigation: 0, controller: null, saving: new Map(),
  workflowWrites: new Map(), viewedWrites: new Map(), changedStatuses: new Set(), markingViewed: new Set(),
  sourceDrafts: new Map(), sourceWrites: new Map(), sourceNotices: new Map(), recommendationDrafts: new Map() };

const esc = (value) => String(value ?? "").replace(/[&<>"']/g,
  (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));
const vacancyLink = (id) => `#/v/${encodeURIComponent(id)}`;
const reportLink = (vacancyId, id) => `${vacancyLink(vacancyId)}/r/${encodeURIComponent(id)}`;
const editorText = (value) => value.replace(/\r\n?/g, "\n");
const yearsFormat = new Intl.NumberFormat("ru-RU", { style: "unit", unit: "year", unitDisplay: "long" });

function experienceLabel(months) {
  if (!Number.isInteger(months) || months < 0) return "Опыт не указан";
  if (months === 0) return "Без опыта";
  if (months < 12) return "Меньше года опыта";
  return `${yearsFormat.format(Math.floor(months / 12))} опыта`;
}

async function showVacancyPreview() {
  if (preview.open) return;
  const request = new AbortController();
  previewRequest = request;
  previewText.textContent = "";
  previewStatus.textContent = "Загрузка файла вакансии…";
  preview.showModal();
  previewContent.scrollTop = 0;
  try {
    const vacancy = await api.getVacancy(state.vacancyId, { signal: request.signal });
    if (request.signal.aborted) return;
    const text = vacancy?.source_json?.source_text;
    if (typeof text === "string" && text.trim()) {
      previewText.textContent = text;
      previewStatus.textContent = "";
    } else {
      previewStatus.textContent = "Исходный текст вакансии недоступен.";
    }
  } catch (error) {
    if (!request.signal.aborted) previewStatus.textContent = error.message;
  }
}

function closeVacancyPreview() {
  if (!preview.open) return;
  previewRequest?.abort();
  preview.close();
}

preview.querySelector(".preview-close").addEventListener("click", closeVacancyPreview);
preview.addEventListener("cancel", (event) => {
  event.preventDefault();
  closeVacancyPreview();
});
let pressedBackdrop = false;
preview.addEventListener("pointerdown", (event) => {
  pressedBackdrop = event.target === preview;
});
preview.addEventListener("click", (event) => {
  if (pressedBackdrop && event.target === preview) closeVacancyPreview();
});

function responseStatus(item) {
  if (Object.hasOwn(STATUS, item.workflow_status)) return item.workflow_status;
  return item.is_viewed || state.report?.id === item.id ? "reviewed" : null;
}

function rememberResponseUpdate(writes, id, fields) {
  writes.set(id, { fields, at: performance.now() });
  if (state.report?.id === id) Object.assign(state.report, fields);
  const item = state.items?.find((entry) => entry.id === id);
  if (item) {
    Object.assign(item, fields);
    const vacancy = state.vacancies.find((entry) => entry.id === state.vacancyId);
    if (vacancy) renderList(vacancy, state.items);
  }
}

function applyRecentResponseUpdates(item, requestStarted) {
  for (const writes of [state.viewedWrites, state.workflowWrites]) {
    const saved = writes.get(item.id);
    if (!saved) continue;
    if (saved.at >= requestStarted) Object.assign(item, saved.fields);
    else writes.delete(item.id);
  }
  return item;
}

async function markViewed(report) {
  if (report.is_viewed || state.markingViewed.has(report.id)) return;
  state.markingViewed.add(report.id);
  try {
    const result = await api.updateViewed(report.id, true);
    if (result.id !== report.id || result.is_viewed !== true) throw new Error("Сервер не подтвердил отметку");
    rememberResponseUpdate(state.viewedWrites, report.id, { is_viewed: true });
  } catch (error) {
    console.warn("Не удалось сохранить отметку просмотра.", error);
  } finally {
    state.markingViewed.delete(report.id);
  }
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

function hiringBanner(vacancy) {
  const hired = vacancy.hired_candidates || [];
  if (!hired.length) return "";
  return `<section class="hiring-banner" role="status"><b>Департамент подтвердил подписание трудового договора</b>
    <ul>${hired.map((candidate) => `<li><a href="${reportLink(vacancy.id, candidate.id)}">${esc(candidate.candidate_name || "ФИО не указано")}</a></li>`).join("")}</ul>
    <p>${vacancy.status === "closed" ? "Вакансия закрыта HR." : "Проверьте результат найма и закройте вакансию."}</p></section>`;
}

const RECOMMENDATION_FIELDS = [
  ["strengths", "Сильные стороны"],
  ["weaknesses", "Слабые места"],
  ["interview_focus", "Что проверить на собеседовании"],
  ["notes", "Заметки"],
];

function recommendationForm(report) {
  const value = state.recommendationDrafts.get(report.id) || report.recruiter_recommendation || {};
  return `<div class="recommendation-form">${RECOMMENDATION_FIELDS.map(([key, label]) =>
    `<label>${label}<textarea data-recommendation-field="${key}" maxlength="800" rows="3">${esc(value[key] || "")}</textarea></label>`).join("")}
    <button class="act main" type="button" data-action="send-resume">Отправить резюме с отзывом</button></div>`;
}

function readRecommendation() {
  return Object.fromEntries([...app.querySelectorAll("[data-recommendation-field]")]
    .map((input) => [input.dataset.recommendationField, input.value.trim() || null]));
}

// Страница вакансий: названия строками через точку, сначала открытые, потом закрытые.
let vacanciesRequested = false;
const responsesRequested = new Set();

function uploadButton(action, title, hint) {
  return `<button type="button" class="upload-zone" data-upload="${action}">
    <strong>${title}</strong><span>${hint}</span></button>`;
}

function renderVacanciesPage(content = "", busy = false) {
  app.setAttribute("aria-busy", String(busy));
  app.innerHTML = content + uploadButton("vacancies", "Загрузить вакансию", "Нажмите или перетащите файл вакансии.");
}

function renderVacancies() {
  const open = state.vacancies.filter((vacancy) => vacancy.status !== "closed");
  const closed = state.vacancies.filter((vacancy) => vacancy.status === "closed");
  const rows = (list) => list.length
    ? list.map((vacancy, i) => `<span class="vac"><a href="${vacancyLink(vacancy.id)}">${esc(vacancy.title)}</a>${
      i < list.length - 1 ? '<span class="vac-sep" aria-hidden="true"></span>' : ""}</span> `).join("")
    : '<span class="vac-empty">Пока нет</span>';
  const group = (title, list, extra = "") => `<section class="vac-group ${extra}"><h2>${title}</h2><hr>
    <div class="vac-rows">${rows(list)}</div></section>`;
  const content = state.vacancies.length
    ? group("Открытые вакансии", open) + group("Закрытые вакансии", closed, "closed")
    : '<p role="status">В базе пока нет вакансий.</p>';
  renderVacanciesPage(content);
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
  app.innerHTML = `<section class="msg" role="status"><h1>${esc(title)}</h1>
    <p>${esc(text)}</p><button class="act" data-action="${action}">Повторить</button></section>`;
}

function loading(text) {
  app.setAttribute("aria-busy", "true");
  app.innerHTML = `<p role="status">${esc(text)}</p>`;
}

function renderList(vacancy, items) {
  vacancy.responses_count = items.length;
  const groups = { green: [], yellow: [], red: [] };
  for (const item of items) {
    const group = item.candidate_name?.trim() && item.has_contacts ? item.match_status : "red";
    groups[group].push(item);
  }
  for (const group of Object.values(groups)) {
    group.sort((first, second) =>
      Number(first.workflow_status === "forwarded") - Number(second.workflow_status === "forwarded"));
  }
  const card = (item) => {
    const status = responseStatus(item);
    const label = status ? STATUS[status] : "Новое";
    return `<a class="candidate-card" data-card-status="${status ?? "unviewed"}" href="${reportLink(vacancy.id, item.id)}">
      <span class="candidate-name">${esc(item.candidate_name?.trim() || "ФИО не указано")}</span>
      <span class="candidate-meta">
        <span class="candidate-experience">${experienceLabel(item.experience_months)}</span>
        ${status === "reviewed" ? `<span class="sr-only">${STATUS.reviewed}</span>`
          : `<span class="candidate-status" data-status-label="${status ?? "unviewed"}">${esc(label)}</span>`}
      </span>
    </a>`;
  };
  const sections = Object.entries(MATCH_GROUPS).map(([key, label]) => `<section class="candidate-group" aria-labelledby="match-group-${key}">
      <h2 id="match-group-${key}">${label}</h2><hr>
      ${groups[key].length ? `<div class="candidate-grid">${groups[key].map(card).join("")}</div>` : '<p class="candidate-group-empty">Нет откликов</p>'}
    </section>`).join("");
  state.items = items;
  const content = items.length ? sections : '<p role="status">Нет откликов.</p>';
  renderResponsesPage(vacancy, `<div class="candidate-groups">${content}</div>`);
}

function renderResponsesPage(vacancy, content = "") {
  app.setAttribute("aria-busy", "false");
  app.innerHTML = `<div class="responses-page">
    <div class="responses-heading">
      <div class="vacancy-heading">
        <h1>${esc(vacancy.title)}</h1>
        <button type="button" class="icon-button vacancy-file" data-action="preview-vacancy"
          aria-label="Предпросмотр файла вакансии" aria-haspopup="dialog" aria-controls="vacancy-preview" title="Предпросмотр файла вакансии">
          <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <path d="M6 3h8l4 4v14H6z M14 3v5h4 M9 12h6 M9 16h6"/>
          </svg>
        </button>
        <span class="vacancy-state">(${vacancy.status === "closed" ? "вакансия закрыта" : "вакансия открыта"})</span>
      </div>
    </div>
    ${hiringBanner(vacancy)}
    ${content}
    ${uploadButton("responses", "Загрузить отклики", "Нажмите или перетащите файлы откликов.")}
  </div>`;
}

function renderPdfReport(report) {
  if (!report.pdf_report) return '<p class="panel" role="status">PDF-отчёт для этого отклика пока недоступен.</p>';
  return `<p class="pdf-stale" data-pdf-stale role="status" ${report.pdf_report.is_stale ? "" : "hidden"}>Текст резюме изменён после создания PDF.</p>
    <div class="pdf-viewer" data-pdf-viewer role="region" aria-label="Страницы PDF-отчёта" tabindex="0">
      <p class="pdf-status" role="status">Загрузка PDF…</p>
    </div>`;
}

function showPdfPreviewError(viewer, id) {
  const status = document.createElement("p");
  status.className = "pdf-status";
  status.setAttribute("role", "status");
  const html = document.createElement("a");
  html.href = api.offlineReportUrl(id);
  html.textContent = "Открыть HTML-версию";
  const download = document.createElement("a");
  download.href = api.pdfReportUrl(id, true);
  download.textContent = "Скачать PDF";
  status.append("Не удалось открыть предпросмотр. ", html, " · ", download);
  viewer.replaceChildren(status);
}

async function loadPdfPreview(id, signal) {
  const viewer = app.querySelector("[data-pdf-viewer]");
  if (!viewer) return;
  try {
    const { pages } = await api.getPdfPreview(id, { signal });
    if (signal.aborted || !viewer.isConnected) return;
    if (!Array.isArray(pages) || !pages.length) {
      showPdfPreviewError(viewer, id);
      return;
    }
    viewer.replaceChildren(...pages.map(({ width, height }, index) => {
      const image = document.createElement("img");
      const page = index + 1;
      image.className = "pdf-page";
      image.alt = `Страница ${page} PDF-отчёта`;
      image.width = Math.round(width);
      image.height = Math.round(height);
      image.loading = index === 0 ? "eager" : "lazy";
      image.decoding = "async";
      image.addEventListener("error", () => {
        if (!signal.aborted && image.isConnected) showPdfPreviewError(viewer, id);
      }, { once: true });
      image.src = api.pdfPreviewPageUrl(id, page);
      return image;
    }));
  } catch {
    if (!signal.aborted && viewer.isConnected) showPdfPreviewError(viewer, id);
  }
}

function renderReport(report, vacancy) {
  app.setAttribute("aria-busy", "false");
  vacancyCrumb.removeAttribute("aria-current");
  responseCrumb.textContent = report.candidate_number ? `Отчёт по отклику № ${report.candidate_number}` : "Отчёт по отклику";
  responseCrumb.href = reportLink(vacancy.id, report.id);
  app.innerHTML = `<div class="report-page">
    <div class="report-toolbar">
      <a class="icon-button report-back" href="${vacancyLink(vacancy.id)}" aria-label="Назад к списку откликов" title="Назад к списку откликов">
        <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <path d="m12 5-7 7 7 7 M5 12h14"/>
        </svg>
      </a>
      <div class="report-tabs" role="tablist" aria-label="Отклик">
        ${REPORT_TABS.map(([key, label], index) => `<button type="button" id="report-tab-${key}" role="tab" data-report-tab="${key}"
          aria-selected="${index === 0}" aria-controls="report-panel-${key}" tabindex="${index === 0 ? 0 : -1}">${label}</button>`).join("")}
      </div>
      <div class="report-progress">
        <button type="button" class="act report-next-action" data-action="advance-status" hidden></button>
        <details class="report-status">
          <summary><span class="sr-only">Статус отклика</span><span data-current-status></span>
            <svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m4 6 4 4 4-4"/></svg>
          </summary>
          <div class="report-status-options" role="group" aria-label="Статусы отклика">
            ${Object.entries(STATUS).map(([value, label]) => `<button type="button" data-workflow-choice="${value}">${esc(label)}</button>`).join("")}
          </div>
        </details>
      </div>
    </div>
    <p class="notice error" data-workflow-error role="status" hidden></p>
    <section id="report-panel-pdf" role="tabpanel" aria-labelledby="report-tab-pdf" tabindex="0">
      ${renderPdfReport(report)}
    </section>
    <section class="panel" id="report-panel-review" role="tabpanel" aria-labelledby="report-tab-review" tabindex="0" hidden>
      <h1 data-candidate-name>${esc(report.candidate_name || "ФИО не указано")}</h1>
      ${vacancy.status === "closed" ? '<p class="closure-badge">Вакансия закрыта HR</p>' : ""}
      ${report.hired_at ? '<p class="hiring-badge">Найм закрыт · трудовой договор подписан</p>' : report.is_closed ? '<p class="closure-badge">Работа с откликом в департаменте завершена</p>' : ""}
      <p>Рекомендации рекрутера будут переданы в департамент.</p>${recommendationForm(report)}
      <p class="notice" data-save-notice role="status" aria-live="polite"></p>
    </section>
    <section class="panel" id="report-panel-resume" role="tabpanel" aria-labelledby="report-tab-resume" tabindex="0" data-source-panel hidden>
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
    </section>
  </div>`;
  app.querySelector("[data-source]").textContent = report.source_text;
  renderSourceEditor();
  updateStatusControls();
}

function selectReportTab(selectedTab) {
  app.querySelectorAll("[data-report-tab]").forEach((tab) => {
    const selected = tab === selectedTab;
    tab.setAttribute("aria-selected", String(selected));
    tab.tabIndex = selected ? 0 : -1;
    document.getElementById(tab.getAttribute("aria-controls")).hidden = !selected;
  });
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
  draft.saving = true;
  draft.error = "";
  renderSourceEditor();
  const requestStarted = performance.now();
  try {
    const updated = await api.updateSourceText(draft.id, draft.text, draft.originalText);
    if (updated.id !== draft.id || updated.source_text !== draft.text) throw new Error("Не удалось подтвердить сохранение текста.");
    applyRecentResponseUpdates(updated, requestStarted);
    state.sourceWrites.set(draft.id, { report: updated, at: performance.now() });
    state.sourceDrafts.delete(draft.id);
    state.sourceNotices.set(draft.id, "Текст сохранён. Отчёт обновлён.");
    if (state.report?.id === draft.id) {
      state.report = updated;
      app.querySelector("[data-source]").textContent = updated.source_text;
      app.querySelector("[data-candidate-name]").textContent = updated.candidate_name || "ФИО не указано";
      app.querySelector("[data-pdf-stale]")?.toggleAttribute("hidden", !updated.pdf_report?.is_stale);
      renderSourceEditor();
      if (!app.querySelector("[data-source-panel]").hidden) {
        app.querySelector('[data-action="edit-source"]').focus({ preventScroll: true });
      }
    } else if (!state.report && state.items?.some((item) => item.id === draft.id)) {
      await route();
    }
  } catch (error) {
    draft.saving = false;
    draft.error = error.message;
    if (state.report?.id === draft.id) renderSourceEditor();
  }
}

function updateNextAction() {
  const report = state.report;
  const button = app.querySelector('[data-action="advance-status"]');
  if (!report || !button) return;
  const status = responseStatus(report);
  let nextStatus;
  if (status === "not_forwarded") nextStatus = "forwarded";
  else if (status === "call_pending" || (status === "reviewed" && Object.values(readRecommendation()).some(Boolean))) nextStatus = "not_forwarded";
  else if (status === "reviewed" && !state.changedStatuses.has(report.id)) nextStatus = "call_pending";
  button.hidden = !nextStatus;
  if (!nextStatus) return;
  button.dataset.nextStatus = nextStatus;
  button.textContent = nextStatus === "forwarded" ? "Отправить резюме" : STATUS[nextStatus];
  button.disabled = state.saving.has(report.id);
}

function updateStatusControls({ notice = "", error = false, transfer = false } = {}) {
  const report = state.report;
  if (!report) return;
  const current = app.querySelector("[data-current-status]");
  if (!current) return;
  const saving = state.saving.has(report.id);
  const status = responseStatus(report);
  current.textContent = STATUS[status];
  current.dataset.currentStatus = status;
  app.querySelectorAll("[data-workflow-choice]").forEach((option) => {
    option.disabled = saving;
    option.setAttribute("aria-pressed", String(option.dataset.workflowChoice === status));
  });
  app.querySelectorAll("[data-recommendation-field], [data-action='send-resume']").forEach((input) => { input.disabled = saving; });
  const output = app.querySelector("[data-save-notice]");
  output.textContent = state.saving.get(report.id) === "forwarded" ? "Отправляем…" : transfer ? notice : "";
  output.classList.toggle("error", transfer && error);
  output.classList.toggle("success", transfer && !error && Boolean(notice));
  const workflowError = app.querySelector("[data-workflow-error]");
  workflowError.hidden = transfer || !error;
  workflowError.textContent = workflowError.hidden ? "" : notice;
  updateNextAction();
}

function chooseStatus(button) {
  const report = state.report;
  if (!report) return;
  const menu = button.closest("details");
  menu.open = false;
  const status = button.dataset.workflowChoice;
  if (status === "forwarded") {
    if (responseStatus(report) !== "forwarded") {
      selectReportTab(app.querySelector("#report-tab-review"));
      app.querySelector('[data-action="send-resume"]').focus();
    }
    return;
  }
  saveWorkflow(status);
  menu.querySelector("summary").focus();
}

function advanceStatus(button) {
  if (button.dataset.nextStatus === "forwarded") {
    selectReportTab(app.querySelector("#report-tab-review"));
    sendResume();
    return;
  }
  saveWorkflow(button.dataset.nextStatus);
  app.querySelector(".report-status summary").focus();
}

function sendResume() {
  return saveWorkflow("forwarded", readRecommendation());
}

async function saveWorkflow(status, recommendation) {
  const report = state.report;
  if (!report || state.saving.has(report.id)) return;
  const transfer = status === "forwarded";
  if (!transfer && status === responseStatus(report)) return;
  const next = status === "reviewed" ? "new" : status;
  state.saving.set(report.id, status);
  updateStatusControls();
  let notice = "";
  let error = false;
  try {
    const result = await api.updateStatus(report.id, next, recommendation);
    if (result.id !== report.id || result.workflow_status !== next) {
      throw new Error(transfer ? "Не удалось подтвердить отправку резюме." : "Не удалось подтвердить изменение статуса.");
    }
    const fields = { workflow_status: result.workflow_status };
    if (transfer) {
      fields.recruiter_recommendation = recommendation;
      state.recommendationDrafts.delete(report.id);
      notice = "Резюме и отзыв переданы на отправку в департамент.";
    } else {
      state.changedStatuses.add(report.id);
      if (status === "reviewed") markViewed(report);
    }
    rememberResponseUpdate(state.workflowWrites, report.id, fields);
  } catch (failure) {
    error = true;
    notice = failure.message;
  } finally {
    state.saving.delete(report.id);
    if (state.report?.id === report.id) updateStatusControls({ notice, error, transfer });
  }
}

async function route() {
  closeVacancyPreview();
  responseCrumb.textContent = "";
  vacancyCrumb.setAttribute("aria-current", "page");
  const navigation = ++state.navigation;
  state.controller?.abort();
  state.controller = new AbortController();
  const { signal } = state.controller;
  state.report = null;
  state.items = null;
  const home = !location.hash || location.hash === "#/";
  let vacancyId, responseId;
  try {
    if (home) {
      state.vacancyId = null;
      vacancyCrumb.textContent = "";
      if (!vacanciesRequested) {
        renderVacanciesPage();
        return;
      }
      renderVacanciesPage('<p role="status">Загрузка вакансий…</p>', true);
    }
    const vacancies = await api.listVacancies({ signal });
    if (navigation !== state.navigation) return;
    if (!Array.isArray(vacancies)) throw new Error("Не удалось прочитать список вакансий.");
    state.vacancies = vacancies;
    if (home) {
      renderVacancies();
      window.scrollTo(0, 0);
      return;
    }
    const parts = location.hash.match(/^#\/v\/([^/]+)(?:\/r\/([^/]+)(\/pdf)?)?\/?$/);
    if (!parts) throw new Error("Страница не найдена. Выберите вакансию.");
    vacancyId = decodeURIComponent(parts[1]);
    responseId = parts[2] ? decodeURIComponent(parts[2]) : null;
    const legacyReportUrl = Boolean(parts[3]);
    // Existing bookmarks in the prototype used a numeric vacancy tab index.
    if (!state.vacancies.some((v) => v.id === vacancyId) && /^\d+$/.test(vacancyId)) {
      const legacy = state.vacancies[Number(vacancyId)];
      if (legacy) {
        const target = responseId ? reportLink(legacy.id, responseId) : vacancyLink(legacy.id);
        location.replace(legacyReportUrl ? `${target}/pdf` : target);
        return;
      }
    }
    const vacancy = state.vacancies.find((item) => item.id === vacancyId);
    if (!vacancy) throw new Error("Вакансия не найдена. Выберите другую вакансию.");
    vacancyCrumb.textContent = vacancy.title;
    vacancyCrumb.href = vacancyLink(vacancy.id);
    state.vacancyId = vacancyId;
    vacanciesRequested = true;
    if (!responseId && !responsesRequested.has(vacancyId)) {
      renderResponsesPage(vacancy);
      window.scrollTo(0, 0);
      return;
    }
    if (responseId) loading("Загрузка отчёта…");
    const requestStarted = performance.now();
    if (responseId) {
      let report = await api.getReport(responseId, { signal });
      if (navigation !== state.navigation) return;
      if (report.vacancy_id !== vacancyId) throw new Error("Отклик относится к другой вакансии.");
      if (legacyReportUrl && report.pdf_report) {
        location.replace(api.offlineReportUrl(report.id));
        return;
      }
      report = applyRecentSource(report, requestStarted);
      applyRecentResponseUpdates(report, requestStarted);
      state.report = report;
      renderReport(report, vacancy);
      loadPdfPreview(report.id, signal);
      markViewed(report);
    } else {
      renderResponsesPage(vacancy, '<p role="status">Загрузка откликов…</p>');
      let items;
      try {
        items = await api.listResponses(vacancyId, { signal });
      } catch (error) {
        if (!signal.aborted && navigation === state.navigation) {
          renderResponsesPage(vacancy, `<p role="alert">${esc(error.message)}</p>`);
        }
        return;
      }
      if (navigation !== state.navigation) return;
      items.forEach((item) => applyRecentResponseUpdates(item, requestStarted));
      renderList(vacancy, items);
    }
    window.scrollTo(0, 0);
  } catch (error) {
    if (signal.aborted || navigation !== state.navigation) return;
    if (home) renderVacanciesPage(`<p role="alert">${esc(error.message)}</p>`);
    else message("Не удалось загрузить данные", error.message);
  }
}

function activateUpload(action) {
  if (action === "vacancies") vacanciesRequested = true;
  if (action === "responses") responsesRequested.add(state.vacancyId);
  return route();
}

function fileZone(event) {
  if (!event.dataTransfer?.types.includes("Files")) return null;
  return event.target instanceof Element ? event.target.closest("[data-upload]") : null;
}

app.addEventListener("dragover", (event) => {
  const zone = fileZone(event);
  if (!zone) return;
  event.preventDefault();
  event.dataTransfer.dropEffect = "copy";
  zone.classList.add("dragging");
});

app.addEventListener("dragleave", (event) => {
  const zone = event.target instanceof Element && event.target.closest("[data-upload]");
  if (zone && !zone.contains(event.relatedTarget)) zone.classList.remove("dragging");
});

app.addEventListener("drop", (event) => {
  const zone = fileZone(event);
  if (!zone) return;
  event.preventDefault();
  zone.classList.remove("dragging");
  if (event.dataTransfer.files.length) activateUpload(zone.dataset.upload);
});

app.addEventListener("click", (event) => {
  const button = event.target instanceof Element ? event.target.closest("button") : null;
  if (!button || button.disabled) return;
  if (button.dataset.upload) activateUpload(button.dataset.upload);
  else if (button.dataset.action === "send-resume") sendResume();
  else if (button.dataset.action === "advance-status") advanceStatus(button);
  else if (button.dataset.workflowChoice) chooseStatus(button);
  else if (button.dataset.action === "refresh") route();
  else if (button.dataset.reportTab) selectReportTab(button);
  else if (button.dataset.action === "preview-vacancy") showVacancyPreview();
  else if (button.dataset.action === "edit-source") editSource();
  else if (button.dataset.action === "save-source") saveSource();
  else if (button.dataset.action === "cancel-source") cancelSource();
});

document.addEventListener("click", (event) => {
  const menu = app.querySelector(".report-status[open]");
  if (menu && !menu.contains(event.target)) menu.open = false;
});

app.addEventListener("input", (event) => {
  const input = event.target;
  if (!(input instanceof HTMLTextAreaElement) || !input.dataset.recommendationField || !state.report) return;
  const draft = { ...(state.recommendationDrafts.get(state.report.id) || state.report.recruiter_recommendation || {}) };
  draft[input.dataset.recommendationField] = input.value;
  state.recommendationDrafts.set(state.report.id, draft);
  updateNextAction();
});

app.addEventListener("input", (event) => {
  if (!(event.target instanceof HTMLTextAreaElement) || !event.target.matches("[data-source-input]")) return;
  const draft = state.report && state.sourceDrafts.get(state.report.id);
  if (draft) draft.text = event.target.value;
});

app.addEventListener("keydown", (event) => {
  const menu = event.target instanceof Element && event.target.closest(".report-status[open]");
  if (menu && event.key === "Escape") {
    menu.open = false;
    menu.querySelector("summary").focus();
    return;
  }
  if (event.target instanceof HTMLButtonElement && event.target.dataset.reportTab) {
    const tabs = [...event.target.parentElement.querySelectorAll("[data-report-tab]")];
    const index = tabs.indexOf(event.target);
    const nextIndices = { ArrowLeft: (index + tabs.length - 1) % tabs.length,
      ArrowRight: (index + 1) % tabs.length, Home: 0, End: tabs.length - 1 };
    if (!Object.hasOwn(nextIndices, event.key)) return;
    event.preventDefault();
    const nextTab = tabs[nextIndices[event.key]];
    selectReportTab(nextTab);
    nextTab.focus();
    return;
  }
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

window.addEventListener("hashchange", route);
route();
