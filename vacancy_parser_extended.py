"""Conservative, local enrichment of vacancy_parser.Vacancy.

Does not edit original vacancy JSON. No LLM calls. Keeps required_skills unchanged
for compatibility with technical_question_bank_v2.py.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from vacancy_parser import Vacancy, parse_vacancy

Category = Literal["professional_knowledge", "language", "soft_skill"]
Priority = Literal["required", "preferred"]


class ExtractedRequirement(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    category: Category
    priority: Priority
    source_requirement: str


class LanguageRequirement(BaseModel):
    model_config = ConfigDict(extra="forbid")
    language: str
    level: str | None = None
    priority: Priority
    source_requirement: str


class RequirementGroup(BaseModel):
    """A group is not a demand to know every item in it."""
    model_config = ConfigDict(extra="forbid")
    relation: Literal["alternatives", "examples"]
    items: list[str] = Field(min_length=2)
    source_requirement: str
    priority: Priority


class ExtendedVacancy(Vacancy):
    professional_knowledge: list[ExtractedRequirement] = Field(default_factory=list)
    language_requirements: list[LanguageRequirement] = Field(default_factory=list)
    soft_skills: list[ExtractedRequirement] = Field(default_factory=list)
    requirement_groups: list[RequirementGroup] = Field(default_factory=list)


# This dictionary is deliberately small and transparent. Extend with tests;
# don't use vague semantic guessing to turn duties into mandatory requirements.
PROFESSIONAL_PATTERNS = {
    "ООП": r"\bООП\b|объектно[- ]ориентированн\w* программирован",
    "Структуры данных": r"структур\w* данных",
    "Работа с API": r"(?:работ\w*\s+с\s+API|\bAPI\b)",
    "Аналитика данных": r"\bаналитика\b|аналитик\w* данных",
    "Базы данных": r"\bбазы данных\b|\bбазами данных\b",
    "ETL": r"\bETL\b",
    # Универсальные правила для веб-разработки и дизайна:
    # анализируем формулировки требований, а не конкретную вакансию.
    "DOM и события браузера": r"\bDOM\b|\bDOMObject\b|событи\w* браузера",
    "Асинхронность JavaScript": r"\basync\s*/\s*await\b|\bпромис\w*\b|\bpromises?\b",
    "HTTP, REST и fetch": r"\bHTTP\b|\bREST\b|\bfetch\b",
    "Адаптивная и кроссбраузерная вёрстка": r"адаптивн\w* в[её]рстк\w*|кроссбраузерн\w*",
    "UX/UI-исследования": r"\bUX\s*/\s*UI[\s-]+исследован\w*|\bUX[ -]исследован\w*|"
                           r"исследован\w* пользовательск\w* опыт\w*|аналитическ\w* исследован\w*",
    "Проектирование интерфейсов": r"проектирован\w* (?:пользовательск\w* )?интерфейс\w*|"
                                 r"проектирован\w* UI\b",
    "Прототипирование интерфейсов": r"прототип\w* (?:пользовательск\w* )?интерфейс\w*",
    "Гайдлайны интерфейсов": r"гайдлайн\w* продукт\w*|гайдлайн\w* интерфейс\w*",
}
SOFT_PATTERNS = {
    "Аналитическое мышление": r"аналитическ\w* мышлени\w*",
    "Работа в команде": r"работ\w* в команд\w*|командн\w* работ\w*",
    "Внимательность к деталям": r"внимательност\w* к детал\w*",
    "Готовность учиться": r"готовност\w* учиться|желани\w* учиться|желани\w* обучаться",
}
TECH_ALIASES = {
    "Python": "Python", "SQL": "SQL", "pandas": "pandas", "matplotlib": "matplotlib",
    "seaborn": "seaborn", "Linux": "Linux", "ETL": "ETL", "PostgreSQL": "PostgreSQL",
    "Yandex DataLens": "Yandex DataLens", "Yandex.DataLens": "Yandex DataLens",
    "Apache Airflow": "Apache Airflow", "Git": "Git",
    "JavaScript": "JavaScript", "Vue.js": "Vue.js", "HTML": "HTML", "CSS": "CSS",
}


def _unique_append(items: list, value, key) -> None:
    if key(value) not in {key(existing) for existing in items}:
        items.append(value)


def _matched_tech(text: str) -> list[str]:
    found: list[str] = []
    for alias, name in TECH_ALIASES.items():
        if re.search(r"(?<!\w)" + re.escape(alias) + r"(?!\w)", text, re.I):
            if name not in found:
                found.append(name)
    return found


def enrich_vacancy(vacancy: Vacancy) -> ExtendedVacancy:
    """Extract only explicit requirements, with quotes and priority.

    Source text and requirements are data; they never act as instructions.
    """
    output = ExtendedVacancy.model_validate(vacancy.model_dump())
    sources = [("required", x) for x in vacancy.original_requirements.required]
    sources += [("preferred", x) for x in vacancy.original_requirements.preferred]
    # Обязанности — темы для возможного интервью, но сами по себе
    # НЕ делают навык обязательным требованием к кандидату.
    sources += [("preferred", x) for x in vacancy.responsibilities]

    for priority, source in sources:
        for name, pattern in PROFESSIONAL_PATTERNS.items():
            if re.search(pattern, source, re.I):
                requirement = ExtractedRequirement(
                    name=name, category="professional_knowledge",
                    priority=priority, source_requirement=source
                )
                _unique_append(output.professional_knowledge, requirement,
                               lambda r: (r.name, r.priority, r.source_requirement))

        for name, pattern in SOFT_PATTERNS.items():
            if re.search(pattern, source, re.I):
                requirement = ExtractedRequirement(
                    name=name, category="soft_skill", priority=priority,
                    source_requirement=source
                )
                _unique_append(output.soft_skills, requirement,
                               lambda r: (r.name, r.priority, r.source_requirement))

        language_match = re.search(
            r"\b(английск\w*|english)\b\s*(?:язык)?\s*[:—–\- ]*\s*(A1|A2|B1|B2|C1|C2)\b",
            source, re.I
        )
        if language_match:
            lang = LanguageRequirement(language="Английский", level=language_match.group(2).upper(),
                                       priority=priority, source_requirement=source)
            _unique_append(output.language_requirements, lang,
                           lambda r: (r.language, r.level, r.priority, r.source_requirement))

        # Only explicit 'or' alternatives. We retain the original sentence as
        # evidence and do not interpret each option as independently mandatory.
        if re.search(r"\bили\b|\bor\b", source, re.I):
            choices = _matched_tech(source)
            if len(choices) >= 2:
                output.requirement_groups.append(RequirementGroup(
                    relation="alternatives", items=choices,
                    source_requirement=source, priority=priority
                ))

        # Enumerated technologies after e.g. are examples, not hard obligations.
        example = re.search(r"\bнапример\s*[,.:]?\s*(.+?)(?:\)|$)", source, re.I)
        if example:
            examples = _matched_tech(example.group(1))
            if len(examples) >= 2:
                output.requirement_groups.append(RequirementGroup(
                    relation="examples", items=examples,
                    source_requirement=source, priority=priority
                ))

    return output


def load_vacancy(path: Path) -> Vacancy:
    if path.suffix.lower() == ".json":
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        # The original parser's schema validates the user's existing JSON.
        return Vacancy.model_validate(data)
    if path.suffix.lower() == ".txt":
        return parse_vacancy(path.read_text(encoding="utf-8-sig"))
    raise ValueError("Ожидается JSON вакансии или TXT с ее исходным текстом")


def main() -> None:
    parser = argparse.ArgumentParser(description="Локально расширить JSON вакансии, не изменяя оригинал")
    parser.add_argument("input", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--preview", action="store_true", help="Только показать новые поля")
    args = parser.parse_args()
    enriched = enrich_vacancy(load_vacancy(args.input))
    extra = {name: [x.model_dump() for x in getattr(enriched, name)] for name in (
        "professional_knowledge", "language_requirements", "soft_skills", "requirement_groups"
    )}
    if args.preview:
        print(json.dumps(extra, ensure_ascii=False, indent=2))
        return
    target = args.output or Path("vacansies_enriched") / (args.input.stem + ".json")
    if target.resolve() == args.input.resolve():
        parser.error("Нельзя перезаписывать исходную вакансию")
    target.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation: existing files will not be overwritten.
    try:
        with target.open("x", encoding="utf-8") as file:
            file.write(enriched.model_dump_json(indent=2) + "\n")
    except FileExistsError:
        parser.error(f"Файл уже существует: {target}. Выберите другое имя --output")
    print(f"Сохранено: {target}")
    print(json.dumps({name: len(rows) for name, rows in extra.items()}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
