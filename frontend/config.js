// В Docker запросы /api/v1 проксируются к FastAPI через nginx.
// При отдельном статическом сервере укажите http://localhost:8000/api/v1.
// В этом файле только публичные настройки, ключи YandexGPT остаются на backend.
window.HR_CONFIG = {
  apiBaseUrl: "/api/v1",
  requestTimeoutMs: 30000,
};
