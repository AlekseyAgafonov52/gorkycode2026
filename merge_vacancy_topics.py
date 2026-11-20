"""Combine technical skills and professional knowledge without losing evidence.

Local only: no LLM calls, no resume access, no changes to source JSON.

Examples:
  python merge_vacancy_topics.py vacansies/python.json --preview
  python merge_vacancy_topics.py vacansies/python.json --existing-bank question_banks/python.json --preview
  python merge_vacancy_topics.py vacansies/python.json --existing-bank question_banks/python.json --output topic_plans/python.json --new-vacancy-output vacansies_for_bank/python_extra.json

The --new-vacancy-output contains ONLY uncovered topics, ready for
technical_question_bank_v2.py. Human review is required for contextual overlap.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from vacancy_parser_extended import enrich_vacancy, load_vacancy


def norm(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().casefold()


def _read_bank(path: Path | None) -> dict:
    if path is None:
        return {"questions": []}
    bank = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(bank, dict) or not isinstance(bank.get("questions"), list):
        raise ValueError("Банк вопросов должен иметь поле questions (список)")
    return bank


def make_topic_plan(vacancy, bank: dict | None = None) -> dict:
    enriched = enrich_vacancy(vacancy)
    topics: dict[str, dict[str, Any]] = {}

    def add(name: str, kind: str, source: str, *, priority: str = "required") -> None:
        key = norm(name)
        if not key:
            return
        if key not in topics:
            topics[key] = {
                "name": name,
                "categories": [],
                "priority": priority,
                "source_requirements": [],
                "mentions": [],
                "existing_question_count": 0,
                "coverage": "not_generated",
            }
        item = topics[key]
        if kind not in item["categories"]:
            item["categories"].append(kind)
        if source and source not in item["source_requirements"]:
            item["source_requirements"].append(source)
        if priority == "required":
            item["priority"] = "required"

    for skill in vacancy.required_skills:
        # Retain original parser classification, but do not assume examples
        # are independently mandatory.
        add(skill, "technical_skill", "")
    for item in enriched.professional_knowledge:
        add(item.name, "professional_knowledge", item.source_requirement, priority=item.priority)

    for group in enriched.requirement_groups:
        for skill in group.items:
            found = topics.get(norm(skill))
            if found and group.relation not in found["mentions"]:
                found["mentions"].append(group.relation)
            if found and group.source_requirement not in found["source_requirements"]:
                found["source_requirements"].append(group.source_requirement)

    # A technology appearing only in an example/alternative group is a
    # topic for OPTIONAL exploration, not an independent strict requirement.
    required_lines = vacancy.original_requirements.required
    for item in topics.values():
        if "technical_skill" not in item["categories"]:
            item["mandatory"] = item["priority"] == "required"
            continue
        skill = item["name"]
        standalone = any(norm(line) in {norm(skill), norm("Yandex.DataLens") if norm(skill) == norm("Yandex DataLens") else ""}
                         for line in required_lines)
        group_only = bool(item["mentions"]) and not standalone
        item["mandatory"] = not group_only
        if group_only:
            item["priority"] = "exploratory"

    # Existing bank question counts by exact topic, not fuzzy semantic guesses.
    bank = bank or {"questions": []}
    for q in bank.get("questions", []):
        if not isinstance(q, dict):
            raise ValueError("questions должен содержать JSON-объекты")
        key = norm(q.get("skill", ""))
        if key in topics:
            topics[key]["existing_question_count"] += 1

    # Explicit thematic matches permit non-LLM coverage hints. Keep these as
    # review flags rather than claiming semantic equivalence is proven.
    text_by_question = [str(q.get("question", "")) for q in bank.get("questions", [])]
    hints = {
        "ООП": re.compile(r"\bООП\b|объектно.ориентирован", re.I),
        "Структуры данных": re.compile(r"структур\w* данных", re.I),
        "Работа с API": re.compile(r"\bAPI\b|\bАПИ\b", re.I),
        "Аналитика данных": re.compile(r"аналитик\w* данных", re.I),
        "Базы данных": re.compile(r"\bбаз\w* данных", re.I),
    }
    for item in topics.values():
        count = item["existing_question_count"]
        if count:
            item["coverage"] = "generated"
        elif item["name"] in hints and any(hints[item["name"]].search(q) for q in text_by_question):
            item["coverage"] = "possibly_covered_review"
        elif item["priority"] == "exploratory":
            item["coverage"] = "exploratory_not_generated"

    values = list(topics.values())
    return {
        "vacancy_title": vacancy.title,
        "topics": values,
        "summary": {
            "total_topics": len(values),
            "mandatory_topics": sum(item["mandatory"] for item in values),
            "exploratory_topics": sum(not item["mandatory"] for item in values),
            "with_existing_questions": sum(item["existing_question_count"] > 0 for item in values),
            "needs_generation_or_review": sum(item["coverage"] in ("not_generated", "possibly_covered_review") for item in values),
        },
        "language_requirements": [x.model_dump() for x in enriched.language_requirements],
        "soft_skills": [x.model_dump() for x in enriched.soft_skills],
        "requirement_groups": [x.model_dump() for x in enriched.requirement_groups],
        "notes": "Пересечения по смыслу требуют ручной проверки. Темы из примеров/альтернатив не являются автоматически обязательными.",
    }



def merge_question_banks(existing: dict, additional: dict) -> dict:
    """Join validated question lists; do not rewrite either source bank."""
    if existing.get("vacancy_title") != additional.get("vacancy_title"):
        raise ValueError("Банки относятся к разным вакансиям")
    result_questions = []
    seen = set()
    for bank in (existing, additional):
        for q in bank.get("questions", []):
            if not isinstance(q, dict) or not q.get("skill") or not q.get("question"):
                raise ValueError("В банке есть некорректный вопрос")
            key = (norm(q["skill"]), norm(q["question"]))
            if key in seen:
                continue
            seen.add(key)
            result_questions.append({"id": f"tech_{len(result_questions) + 1:03d}",
                                     **{k: v for k, v in q.items() if k != "id"}})
    return {
        "vacancy_title": existing["vacancy_title"],
        "source": "merged_yandexgpt_banks",
        "review_status": "requires_human_review",
        "phrasings_per_question": 3,
        "skills": list(dict.fromkeys(q["skill"] for q in result_questions)),
        "notes": "Объединение банков без повторных API-запросов; проверить ответы и тематические пересечения вручную.",
        "questions": result_questions,
    }


def save_new(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
        f.write("\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("vacancy", type=Path)
    parser.add_argument("--existing-bank", type=Path, help="Уже созданный банк; не перегенерируем его вопросы")
    parser.add_argument("--preview", action="store_true")
    parser.add_argument("--output", type=Path, help="Сохранить полный план тем (новый файл)")
    parser.add_argument("--new-vacancy-output", type=Path, help="Отдельный JSON только для новых тем")
    parser.add_argument("--additional-bank", type=Path, help="Дополнительный сгенерированный банк")
    parser.add_argument("--merged-bank-output", type=Path, help="Новый файл с объединенными вопросами")
    parser.add_argument("--include-exploratory", action="store_true", help="Также планировать темы-примеры")
    args = parser.parse_args()
    if not (args.preview or args.output or args.new_vacancy_output or args.merged_bank_output):
        parser.error("Укажите --preview, --output, --new-vacancy-output или --merged-bank-output")
    vacancy = load_vacancy(args.vacancy)
    bank = _read_bank(args.existing_bank)
    plan = make_topic_plan(vacancy, bank)
    if args.preview:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
    if args.output:
        if args.output.resolve() == args.vacancy.resolve():
            parser.error("Нельзя перезаписать исходную вакансию")
        save_new(args.output, plan)
        print(f"План сохранен: {args.output}")
    if args.new_vacancy_output:
        names = [x["name"] for x in plan["topics"]
                 if x["coverage"] == "not_generated"
                 and (x["mandatory"] or args.include_exploratory)]
        if args.new_vacancy_output.resolve() == args.vacancy.resolve():
            parser.error("Нельзя перезаписать исходную вакансию")
        # This is a separate, validated Vacancy JSON accepted by the current
        # question-bank CLI without editing the original source or current bank.
        payload = vacancy.model_dump()
        payload["required_skills"] = names
        if names:
            save_new(args.new_vacancy_output, payload)
            print(f"Вакансия только для новых тем: {args.new_vacancy_output} ({len(names)} тем)")
        else:
            print("Новых тем для автоматической генерации нет; проверьте possibly_covered_review")
    if args.merged_bank_output:
        if not args.existing_bank or not args.additional_bank:
            parser.error("Для объединения нужны --existing-bank и --additional-bank")
        if args.merged_bank_output.resolve() in {args.existing_bank.resolve(), args.additional_bank.resolve(), args.vacancy.resolve()}:
            parser.error("Нельзя перезаписывать исходные файлы")
        merged = merge_question_banks(bank, _read_bank(args.additional_bank))
        save_new(args.merged_bank_output, merged)
        print(f"Объединенный банк сохранен: {args.merged_bank_output} ({len(merged['questions'])} вопросов)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
