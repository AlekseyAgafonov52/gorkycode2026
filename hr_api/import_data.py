"""Idempotently import the five supplied datasets; never overwrite HR edits."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from check_candidate import PROFILES
from storage.database import make_engine, make_session_factory
from storage.models import Response, Vacancy, VacancyCriterion
from storage.schemas import CriterionCreate, ResponseCreate, VacancyCreate
from vacancy_parser import Vacancy as ParsedVacancy, parse_vacancy_file


JSON_SOURCES = {"frontend": "frontend.json", "junior-python": "python.json", "designer": "designer.json"}


def import_datasets(factory, root: Path) -> dict[str, int]:
    counts = {"vacancies_created": 0, "responses_created": 0}
    with factory.begin() as session:
        for profile, folder_name in PROFILES.items():
            folder = root / "parsed_responses" / folder_name
            if not folder.is_dir():
                continue
            identifier = uuid5(NAMESPACE_URL, "hr-platform/vacancies/" + profile)
            row = session.get(Vacancy, identifier)
            if row is None:
                path = root / "vacansies" / JSON_SOURCES.get(profile, "missing.json")
                if path.is_file():
                    source = json.loads(path.read_text(encoding="utf-8-sig"))
                    parsed = ParsedVacancy.model_validate(source)
                else:
                    parsed = parse_vacancy_file(folder / "job_vacancy.txt")
                    source = parsed.model_dump(mode="json")
                if not parsed.title:
                    raise ValueError("Вакансия не содержит названия: " + profile)
                requirements = []
                seen = set()

                def add(name: str, importance: str):
                    normalized = name.strip().casefold()
                    if not normalized or normalized in seen:
                        return
                    seen.add(normalized)
                    requirements.append(CriterionCreate.model_validate({"name": name if len(name) <= 255 else name[:252] + "…",
                        "description": name, "importance": importance}))

                for name in parsed.required_skills + parsed.additional_requirements:
                    add(name, "required")
                if parsed.education:
                    add(parsed.education, "required")
                if parsed.experience_min_years:
                    add(f"Опыт работы от {parsed.experience_min_years:g} лет", "required")
                for name in parsed.preferred_skills:
                    add(name, "preferred")
                data = VacancyCreate(title=parsed.title, source_json=source, criteria=requirements)
                row = Vacancy(id=identifier, title=data.title, status=data.status, source_json=data.source_json, criteria_version=1)
                row.criteria = [VacancyCriterion(**criterion.model_dump()) for criterion in data.criteria]
                session.add(row)
                session.flush()
                counts["vacancies_created"] += 1
            for path in sorted(folder.glob("*.json")):
                number = path.stem.rsplit(" ", 1)[-1]
                if not number.isdigit():
                    continue
                response_id = uuid5(NAMESPACE_URL, f"hr-platform/responses/{profile}/{number}")
                if session.get(Response, response_id) is not None:
                    continue
                source = json.loads(path.read_text(encoding="utf-8-sig"))
                name = source.get("candidate_name") or source.get("full_name")
                data = ResponseCreate(vacancy_id=identifier, source_json=source,
                                      candidate_name=name if isinstance(name, str) else None)
                session.add(Response(id=response_id, **data.model_dump()))
                counts["responses_created"] += 1
        session.flush()
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    args = parser.parse_args()
    engine = make_engine()
    try:
        print(json.dumps(import_datasets(make_session_factory(engine), args.root), ensure_ascii=False))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
