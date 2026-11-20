// HTTP client for the HR endpoints. No model calls or local persistence.
export class ApiError extends Error {
  constructor(message, status = 0, code = "network_error") {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
  }
}

export function createApi({ apiBaseUrl = "/api/v1", requestTimeoutMs = 30000 } = {}) {
  const base = apiBaseUrl.replace(/\/+$/, "");

  async function request(path, { signal, ...options } = {}) {
    const controller = new AbortController();
    const abort = () => controller.abort();
    if (signal?.aborted) abort();
    signal?.addEventListener("abort", abort, { once: true });
    const timeout = setTimeout(abort, requestTimeoutMs);
    try {
      const response = await fetch(base + path, {
        ...options,
        signal: controller.signal,
        headers: { Accept: "application/json", ...options.headers },
        cache: "no-store",
      });
      let data;
      try {
        data = await response.json();
      } catch {
        throw new ApiError("Сервер вернул некорректный ответ. Попробуйте ещё раз.", response.status);
      }
      if (!response.ok) {
        throw new ApiError(data.error?.message || "Не удалось выполнить запрос.",
          response.status, data.error?.code || "internal_error");
      }
      return data;
    } catch (error) {
      if (signal?.aborted) throw error;
      if (error instanceof ApiError) throw error;
      throw new ApiError(controller.signal.aborted
        ? "Сервер не ответил вовремя. Попробуйте ещё раз."
        : "Не удалось связаться с сервером. Попробуйте ещё раз.");
    } finally {
      clearTimeout(timeout);
      signal?.removeEventListener("abort", abort);
    }
  }

  return {
    listVacancies: (options) => request("/vacancies", options),
    async listResponses(vacancyId, options) {
      const items = [];
      const limit = 1000;
      for (let offset = 0; ; offset += limit) {
        const page = await request(`/vacancies/${encodeURIComponent(vacancyId)}/responses?limit=${limit}&offset=${offset}`, options);
        if (!Array.isArray(page)) throw new ApiError("Не удалось прочитать список откликов.");
        items.push(...page);
        if (page.length < limit) return items;
      }
    },
    getReport: (responseId, options) => request(`/responses/${encodeURIComponent(responseId)}/report`, options),
    pdfReportUrl: (responseId, download = false) => `${base}/responses/${encodeURIComponent(responseId)}/report/pdf${download ? "?download=true" : ""}`,
    updateSourceText: (responseId, sourceText, expectedSourceText) => request(`/responses/${encodeURIComponent(responseId)}/source-text`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ source_text: sourceText, expected_source_text: expectedSourceText }),
    }),
    updateViewed: (responseId, isViewed) => request(`/responses/${encodeURIComponent(responseId)}/viewed`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ is_viewed: isViewed }),
    }),
    updateRecommendation: (responseId, recommendation) => request(`/responses/${encodeURIComponent(responseId)}/department-summary`, {
      method: "PATCH", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ recruiter_recommendation: recommendation }),
    }),
    updateVacancyStatus: (vacancyId, status) => request(`/vacancies/${encodeURIComponent(vacancyId)}/status`, {
      method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ status }),
    }),
    updateStatus: (responseId, workflowStatus, recommendation) => request(`/responses/${encodeURIComponent(responseId)}/status`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ workflow_status: workflowStatus, recruiter_recommendation: recommendation }),
    }),
  };
}
