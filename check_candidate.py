"""Check one candidate against the vacancy and the duties section of its DOCX."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from pydantic import ValidationError

from job_sources import load_vacancy_text
from question_generator import (
    ClarificationResult, QuestionGenerationError, extract_docx_text,
    generate_questions, load_candidate_json, prepare_input,
)
from question_prompts import build_prompts


PROFILES = {
    "frontend": "Frontend developer",
    "junior-python": "Junior Python-разработчик",
    "designer": "Junior UX_UI Дизайнер",
    "python": "Python-разработчик",
    "qa": "QA Engineer Manual",
}
DEFAULT_ROOT = Path(__file__).resolve().parent / "parsed_responses"


def render_report(profile: str, result: ClarificationResult) -> str:
    lines = [f"# {PROFILES[profile]} — кандидат №{result.candidate_id}", "",
             "Уточняющие вопросы для HR. Ответы ещё предстоит получить от кандидата.", "",
             "Вакансия: `../job_vacancy.txt`", "",
             "Дополнительный источник: раздел «Обязанности» из `../job_instruction.docx`", ""]
    for index, question in enumerate(result.questions, 1):
        lines.extend([f"## {index}. {question.question}", "", f"Требование: {question.requirement}",
                      "", f"Причина уточнения: {question.reason}", ""])
    return "\n".join(lines)


def check_candidate(profile: str, number: int, *, root: Path = DEFAULT_ROOT,
                    prepare_only: bool = False) -> Path:
    if number < 1:
        raise ValueError("Номер кандидата должен быть положительным")
    if profile not in PROFILES:
        raise ValueError(f"Неизвестный профиль вакансии: {profile}")
    prompts = build_prompts(profile)
    folder = root / PROFILES[profile]
    instruction_path = folder / "job_instruction.docx"
    vacancy_path = folder / "job_vacancy.txt"
    candidate_path = folder / f"отклик {number}.json"
    instruction = extract_docx_text(instruction_path)
    vacancy_text = load_vacancy_text(vacancy_path)
    candidate = load_candidate_json(candidate_path)
    if str(candidate.candidate_id) != str(number):
        raise ValueError("Номер кандидата в JSON не совпадает с номером выбранного файла")
    output_folder = folder / "questions"
    result: ClarificationResult | None = None
    if prepare_only:
        data = prepare_input(instruction, candidate, vacancy_text=vacancy_text)
        data["prompt_profile"] = prompts.profile
        data["domain"] = prompts.domain
        destination = output_folder / f"input_{number}.json"
        serialized = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False)
    else:
        result = generate_questions(instruction, candidate,
                                    vacancy_text=vacancy_text,
                                    prompt_profile=profile,
                                    on_progress=lambda message: print(f"[{profile}] {message}", flush=True))
        destination = output_folder / f"отклик {number}.json"
        serialized = result.model_dump_json(indent=2)
    output_folder.mkdir(parents=True, exist_ok=True)
    destination.write_text(serialized + "\n", encoding="utf-8")
    if prepare_only:
        preview = {"prompt_profile": prompts.profile, "domain": prompts.domain,
                   "generation": prompts.generation, "review": prompts.review}
        (output_folder / f"prompts_{number}.json").write_text(
            json.dumps(preview, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if result is not None:
        destination.with_suffix(".md").write_text(render_report(profile, result), encoding="utf-8")
    return destination


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile", choices=PROFILES, nargs="?")
    parser.add_argument("number", type=int, nargs="?", default=1)
    parser.add_argument("--all", action="store_true", help="По одному кандидату из каждой из пяти папок")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--prepare-only", action="store_true", help="Проверить вход без вызова YandexGPT")
    args = parser.parse_args(argv)
    if args.all and args.profile or not args.all and args.profile is None:
        parser.error("Укажите профиль либо --all")
    if args.number < 1:
        parser.error("Номер кандидата должен быть положительным")
    profiles = list(PROFILES) if args.all else [args.profile]
    failed = 0
    for profile in profiles:
        try:
            destination = check_candidate(profile, args.number, root=args.root, prepare_only=args.prepare_only)
            label = "Вход подготовлен без API" if args.prepare_only else "Отчёт сохранён"
            print(f"[{profile}] {label}: {destination}", flush=True)
        except (OSError, ValueError, QuestionGenerationError) as exc:
            failed += 1
            message = "Некорректный JSON кандидата" if isinstance(exc, ValidationError) else str(exc)
            print(f"[{profile}] Ошибка: {message}", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
