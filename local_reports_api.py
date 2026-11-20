"""Локальный API для генерации PDF по трем демо-вакансиям.

Запуск из корня репозитория:
    python -m uvicorn local_reports_api:app --host 127.0.0.1 --port 8000
    http://127.0.0.1:8000/frontend/

Не требует PostgreSQL и не обращается к LLM. Работает с локальными JSON.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import unicodedata
from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from main import app

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / 'interview_reports' / 'auto'
CONFIG = {
    'python': ('python.json', 'python_merged.json', 'Junior Python-разработчик'),
    'frontend': ('frontend.json', 'frontend.json', 'Frontend developer'),
    'designer': ('designer.json', 'designer.json', 'Junior UX_UI Дизайнер'),
}


def _config(key: str) -> tuple[Path, Path, Path]:
    if key not in CONFIG:
        raise HTTPException(404, 'Неизвестная вакансия')
    vacancy_name, bank_name, folder_name = CONFIG[key]
    vacancy = ROOT / 'examples' / vacancy_name
    bank = ROOT / 'question_banks' / bank_name
    source = ROOT / 'parsed_responses'
    if not vacancy.is_file() or not bank.is_file() or not source.is_dir():
        raise HTTPException(404, 'Не найдена вакансия, банк вопросов или папка откликов')
    target = unicodedata.normalize('NFC', folder_name).casefold()
    folders = [p for p in source.iterdir() if p.is_dir()
               and unicodedata.normalize('NFC', p.name).casefold() == target]
    if len(folders) != 1:
        raise HTTPException(404, f'Папка откликов не найдена: {folder_name}')
    return vacancy, bank, folders[0]


def _output_dir(key: str) -> Path:
    if key not in CONFIG:
        raise HTTPException(404, 'Неизвестная вакансия')
    return OUTPUT / key


@app.post('/api/local/vacancies/{key}/generate', tags=['Локальные PDF'])
def generate(key: str):
    vacancy, bank, folder = _config(key)
    if not any(p.is_file() and p.suffix.lower() == '.json' for p in folder.iterdir()):
        raise HTTPException(422, 'В папке вакансии нет JSON-откликов')
    try:
        vacancy_data = json.loads(vacancy.read_text(encoding='utf-8-sig'))
        bank_data = json.loads(bank.read_text(encoding='utf-8-sig'))
        if bank_data.get('vacancy_title') != vacancy_data.get('title'):
            raise HTTPException(422, 'Банк вопросов не соответствует вакансии')
    except (OSError, ValueError) as exc:
        raise HTTPException(422, f'Не удалось прочитать вакансию или банк: {exc}') from exc

    out = _output_dir(key)
    out.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, str(ROOT / 'otchet_offline' / 'hr_report.py'),
           str(vacancy), str(folder), '--question-bank', str(bank),
           '--out-dir', str(out)]
    try:
        completed = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True,
                                   timeout=300, check=False)
    except subprocess.TimeoutExpired:
        raise HTTPException(504, 'Генерация заняла слишком много времени') from None
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or 'Неизвестная ошибка').strip()
        raise HTTPException(500, 'Не удалось сформировать все отчёты: ' + detail[-1200:])
    ranking = out / 'ranking.json'
    if not ranking.is_file():
        raise HTTPException(500, 'Генератор не создал ranking.json')
    result = json.loads(ranking.read_text(encoding='utf-8'))
    return {'ok': True, 'vacancy': key, 'reports_count': len(result.get('applications', [])),
            'ranking_url': f'/api/local/reports/{key}/ranking.json'}


@app.get('/api/local/reports/{key}/ranking.json', tags=['Локальные PDF'])
def ranking(key: str):
    path = _output_dir(key) / 'ranking.json'
    if not path.is_file():
        raise HTTPException(404, 'Рейтинг ещё не создан. Нажмите «Сформировать отчёты».')
    return FileResponse(path, media_type='application/json')


@app.get('/api/local/reports/{key}/{filename}', tags=['Локальные PDF'])
def pdf(key: str, filename: str):
    if not re.fullmatch(r'report-[\w.-]+\.pdf', filename, flags=re.UNICODE):
        raise HTTPException(404, 'Отчёт не найден')
    out = _output_dir(key)
    ranking_path = out / 'ranking.json'
    if not ranking_path.is_file():
        raise HTTPException(404, 'Рейтинг не найден')
    ranking_data = json.loads(ranking_path.read_text(encoding='utf-8'))
    allowed = {row.get('report') for row in ranking_data.get('applications', [])}
    if filename not in allowed:
        raise HTTPException(404, 'Отчёт не найден')
    path = out / filename
    if not path.is_file():
        raise HTTPException(404, 'PDF не найден')
    return FileResponse(path, media_type='application/pdf', filename=filename)


app.mount('/frontend', StaticFiles(directory=str(ROOT / 'frontend'), html=True), name='frontend')
