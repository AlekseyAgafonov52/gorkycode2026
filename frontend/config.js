// Откуда фронт берёт данные. Одна строка — одна вакансия (одна вкладка).
// ranking — путь к ranking.json, который делает otchet_offline/hr_report.py.
//   PDF-отчёты ищутся в той же папке, что и ranking.json.
// applications — папка с JSON откликов (необязательно), оттуда берётся должность из резюме.
// Пути считаются от папки frontend.
window.HR_CONFIG = {
  vacancies: [
    {
      ranking: "../otchet_offline/reports/ranking.json",
      applications: "../otchet_offline/applications",
    },
  ],
};
