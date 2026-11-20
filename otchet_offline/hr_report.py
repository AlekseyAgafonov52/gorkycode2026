#!/usr/bin/env python3
"""Создать локальный рейтинг откликов и PDF-отчёты для HR."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from reportlab.pdfbase.ttfonts import TTFError
from reportlab.platypus.doctemplate import LayoutError

from ranking import TRAFFIC, assess, make_vacancy, prepare_resume_text, rank_applications, read_json
from report_pdf import percentage, register_fonts, write_atomic, write_pdf, select_technical_questions


def collect_applications(sources: list[Path], excluded: set[Path]) -> tuple[list[Path], list[str]]:
    files = []
    errors = []
    seen = set(excluded)
    for source in sources:
        try:
            is_directory = source.is_dir()
            if is_directory:
                candidates = sorted(
                    (path for path in source.iterdir() if path.suffix.casefold() == ".json" and path.is_file()),
                    key=lambda path: (path.name.casefold(), path.name),
                )
            elif source.is_file():
                candidates = [source]
            else:
                errors.append(f"Файл или папка не найдены · {source}")
                continue

            has_applications = False
            for path in candidates:
                resolved = path.resolve()
                if resolved in excluded:
                    continue
                has_applications = True
                if resolved not in seen:
                    files.append(path)
                    seen.add(resolved)
            if is_directory and not has_applications:
                errors.append(f"Не найдены JSON-отклики · {source}")
        except (OSError, RuntimeError) as error:
            errors.append(f"Ошибка чтения {source}. {error}")
    if not files and not errors:
        errors.append("Не найдены JSON-отклики.")
    return files, errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Ранжировать отклики и создать PDF по одной вакансии.")
    parser.add_argument("vacancy", type=Path, help="JSON вакансии")
    parser.add_argument("applications", nargs="+", type=Path, help="папки с откликами или отдельные JSON-файлы")
    parser.add_argument("--out-dir", type=Path, default=Path("reports"), help="папка для рейтинга и PDF")
    parser.add_argument("--font", type=Path, help="путь к TTF-шрифту с кириллицей")
    parser.add_argument("--bold-font", type=Path, help="путь к полужирному TTF-шрифту")
    parser.add_argument("--question-bank", type=Path, help="JSON с готовыми техническими вопросами")
    args = parser.parse_args()

    try:
        vacancy_data = read_json(args.vacancy)
        vacancy = make_vacancy(vacancy_data)
        bank = read_json(args.question_bank) if args.question_bank else None
        if bank is not None:
            select_technical_questions(bank, vacancy.title, "__validation__")
        register_fonts(args.font, args.bold_font)
        args.out_dir.mkdir(parents=True, exist_ok=True)
        ranking_path = args.out_dir / "ranking.json"
        excluded = {args.vacancy.resolve(), ranking_path.resolve()}
    except (OSError, ValueError, RuntimeError, TTFError) as error:
        parser.exit(2, f"Ошибка подготовки. {error}\n")

    entries = []
    experience_by_path: dict[Path, int | None] = {}
    applications, input_errors = collect_applications(args.applications, excluded)
    for error in input_errors:
        print(error, file=sys.stderr)
    failed = bool(input_errors)
    for application in applications:
        try:
            resume = read_json(application)
            months = resume.get("experience_total_months")
            experience_by_path[application.resolve()] = (months if type(months) is int and months >= 0 else None)
            prepared = prepare_resume_text(resume)
            assessment = assess(vacancy, resume, application.stem, prepared=prepared)
            entries.append((application, assessment, prepared.passages))
        except (OSError, ValueError) as error:
            print(f"Ошибка в {application.name}. {error}", file=sys.stderr)
            failed = True

    succeeded = []
    used_names = set()
    for application, assessment in rank_applications(vacancy, vacancy_data, entries):
        file_id = re.sub(r"[^\w.-]+", "-", assessment.application_id).strip(".-_")[:90] or "application"
        name = f"report-{file_id}.pdf"
        suffix = 2
        while name.casefold() in used_names:
            name = f"report-{file_id}-{suffix}.pdf"
            suffix += 1
        path = args.out_dir / name
        try:
            technical_questions = (select_technical_questions(bank, vacancy.title, assessment.application_id)
                                   if bank is not None else None)
            write_pdf(path, vacancy, assessment, technical_questions,
                      experience_by_path.get(application.resolve()))
        except (OSError, ValueError, LayoutError) as error:
            print(f"Ошибка отчёта для {application.name}. {error}", file=sys.stderr)
            failed = True
            continue
        succeeded.append((assessment, path))
        used_names.add(name.casefold())

    ranking = {
        "vacancy_id": vacancy.vacancy_id or None,
        "vacancy_title": vacancy.title,
        "best_report": succeeded[0][1].name if succeeded else None,
        "applications": [
            {"rank": rank, "application_id": item.application_id, "color": item.color,
             "confirmed": item.confirmed, "score": item.score, "coverage": item.coverage,
             "lexical_score": round(item.lexical_score, 6), "report": path.name}
            for rank, (item, path) in enumerate(succeeded, 1)
        ],
    }
    try:
        text = json.dumps(ranking, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        write_atomic(ranking_path, lambda path: path.write_text(text, encoding="utf-8"))
    except OSError as error:
        print(f"Ошибка сохранения рейтинга. {error}", file=sys.stderr)
        failed = True

    for assessment, path in succeeded:
        print(f"{path} · {TRAFFIC[assessment.color][0]} · подтверждено {percentage(assessment.confirmed)} · "
              f"по проверенным {percentage(assessment.score)} · покрытие {percentage(assessment.coverage)}")
    if succeeded:
        print(f"Первый в рейтинге · {succeeded[0][1]}")
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())

# RESTORED_OFFLINE_TECHNICAL_QUESTIONS_V1

# PDF_LAYOUT_EXPERIENCE_FIRST_V1
