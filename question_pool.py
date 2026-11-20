"""One YandexGPT call per vacancy; local selection for each candidate."""
from __future__ import annotations

import hashlib
import json
import random
from pathlib import Path
from typing import Protocol

from pydantic import Field

from candidate_analysis import canonical_skill, match
from contract_models import ContractModel, PoolQuestion, ResumeData
from question_generator import YandexGPTClient, YandexGPTSettings

PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "question_pool.txt"


class QuestionPool(ContractModel):
    questions: list[PoolQuestion] = Field(min_length=1)


class PoolBuilder(Protocol):
    def __call__(self, vacancy_text: str, vacancy_skills: list[str]) -> list[PoolQuestion]: ...


def pool_hash(vacancy_text: str, skills: list[str]) -> str:
    data = [vacancy_text, skills, PROMPT_PATH.read_text(encoding="utf-8")]
    return hashlib.sha256(json.dumps(data, ensure_ascii=False).encode("utf-8")).hexdigest()


def validate_pool(pool: list[PoolQuestion], skills: list[str]) -> list[PoolQuestion]:
    groups: dict[str, set[str]] = {name: set() for name in skills}
    for question in pool:
        if question.skill not in groups or not question.expected_answer.strip() or not question.question.strip():
            raise ValueError("Пул содержит неизвестный навык или пустой вопрос/ответ")
        if question.follow_up_hint is not None or question.repeat_of is not None:
            raise ValueError("Поля будущих функций должны быть null")
        groups[question.skill].add(question.question.strip().casefold())
    if any(len(questions) < 2 for questions in groups.values()):
        raise ValueError("Нужны как минимум два разных вопроса на каждый навык")
    return pool


def build_question_pool(vacancy_text: str, vacancy_skills: list[str]) -> list[PoolQuestion]:
    if not vacancy_skills:
        return []
    settings = YandexGPTSettings.from_env().model_copy(update={
        "timeout_seconds": 120, "max_tokens": 8000, "http_retries": 0})
    with YandexGPTClient(settings) as client:
        raw = client.complete(PROMPT_PATH.read_text(encoding="utf-8"),
                              {"vacancy_text": vacancy_text, "vacancy_skills": vacancy_skills}, QuestionPool)
    return validate_pool(QuestionPool.model_validate_json(raw).questions, vacancy_skills)


def pick_questions(pool: list[PoolQuestion], resume: ResumeData,
                   ai_text_level: str | None) -> list[PoolQuestion]:
    """Choose once during preparation; stored reports keep the selection stable."""
    skills = list(dict.fromkeys(question.skill for question in pool))
    found = {row.skill for row in match(resume, skills).skills if row.relevance != "red"}
    count = 2 if ai_text_level == "high" else 1
    selected = []
    for skill in skills:
        if canonical_skill(skill) in found:
            options = [question for question in pool if question.skill == skill]
            selected.extend(random.SystemRandom().sample(options, min(count, len(options))))
    return selected
