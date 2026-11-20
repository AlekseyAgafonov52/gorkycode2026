"""Idempotent TXT/JSON import followed by persisted MVP preparation."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from candidate_analysis import extract_vacancy_skills, resume_from_json
from check_candidate import PROFILES
from response_parser import parse_response
from storage.database import make_engine, make_session_factory
from storage.models import Response, Vacancy, VacancyCriterion
from storage.schemas import CriterionCreate, ResponseCreate, VacancyCreate
from vacancy_parser import Vacancy as ParsedVacancy, decode_txt, parse_vacancy_file
from .preparation import ReportPreparer

JSON_SOURCES = {"frontend": "frontend.json", "junior-python": "python.json", "designer": "designer.json"}


def import_folder(factory, folder: Path, key: str, *, vacancy_json: Path | None = None) -> dict[str, int]:
    """Import a stable source key; existing records and HR edits are preserved."""
    counts = {"vacancies_created": 0, "responses_created": 0}
    identifier = uuid5(NAMESPACE_URL, "hr-platform/vacancies/" + key)
    with factory.begin() as session:
        row = session.get(Vacancy, identifier)
        if row is None:
            if vacancy_json is not None and vacancy_json.is_file():
                source = json.loads(vacancy_json.read_text(encoding="utf-8-sig"))
                parsed = ParsedVacancy.model_validate(source)
            else:
                vacancy_path = folder / "job_vacancy.txt"
                if not vacancy_path.is_file():
                    vacancy_path = folder / "Вакансия.txt"
                parsed = parse_vacancy_file(vacancy_path)
                source = parsed.model_dump(mode="json")
            if not parsed.title:
                raise ValueError("Вакансия не содержит названия: " + key)
            skills = extract_vacancy_skills(parsed.source_text)
            requirements = [CriterionCreate(name=name[:255], importance="required", description=name) for name in skills]
            data = VacancyCreate(title=parsed.title, source_json=source, criteria=requirements)
            row = Vacancy(id=identifier, title=data.title, status=data.status, source_json=data.source_json, criteria_version=1)
            row.criteria = [VacancyCriterion(**criterion.model_dump()) for criterion in data.criteria]
            session.add(row)
            session.flush()
            counts["vacancies_created"] += 1
        # A parsed JSON takes precedence over the TXT of the same response.
        paths = {path.stem: path for path in sorted(folder.glob("*.txt"))}
        paths.update({path.stem: path for path in sorted(folder.glob("*.json"))})
        for stem, path in paths.items():
            number = stem.rsplit(" ", 1)[-1]
            if not number.isdigit():
                continue
            response_id = uuid5(NAMESPACE_URL, f"hr-platform/responses/{key}/{number}")
            if session.get(Response, response_id) is not None:
                continue
            if path.suffix.lower() == ".json":
                source = json.loads(path.read_text(encoding="utf-8-sig"))
            else:
                source = parse_response(decode_txt(path.read_bytes())).model_dump(mode="json")
            resume = resume_from_json(source)
            data = ResponseCreate(vacancy_id=identifier, source_json=source, candidate_name=resume.candidate_name)
            session.add(Response(id=response_id, **data.model_dump()))
            counts["responses_created"] += 1
        session.flush()
    return counts


def import_datasets(factory, root: Path) -> dict[str, int]:
    counts = {"vacancies_created": 0, "responses_created": 0}
    for profile, folder_name in PROFILES.items():
        folder = root / "parsed_responses" / folder_name
        if folder.is_dir():
            result = import_folder(factory, folder, profile,
                                   vacancy_json=root / "vacansies" / JSON_SOURCES.get(profile, "missing.json"))
            for key, value in result.items():
                counts[key] += value
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--folder", type=Path, help="Папка с Вакансия.txt и отклик N.txt/json")
    parser.add_argument("--key", help="Постоянный уникальный ключ папки для защиты от дубликатов")
    parser.add_argument("--data-only", action="store_true", help="Только импорт, без подготовки отчётов и пула")
    args = parser.parse_args()
    if args.folder and not args.key:
        parser.error("Для --folder нужен постоянный --key")
    engine = make_engine()
    factory = make_session_factory(engine)
    try:
        counts = import_folder(factory, args.folder, args.key) if args.folder else import_datasets(factory, args.root)
        print(json.dumps(counts, ensure_ascii=False), flush=True)
        if not args.data_only:
            preparer = ReportPreparer(factory)
            profiles = [args.key] if args.folder else [key for key, folder in PROFILES.items()
                        if (args.root / "parsed_responses" / folder).is_dir()]
            for profile in profiles:
                identifier = uuid5(NAMESPACE_URL, "hr-platform/vacancies/" + profile)
                print(json.dumps({"profile": profile, "reports_prepared": preparer.prepare_vacancy(identifier)},
                                 ensure_ascii=False), flush=True)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
