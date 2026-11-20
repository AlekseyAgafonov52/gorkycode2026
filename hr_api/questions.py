"""Question generation only on explicit jobs; the candidate input is skills only."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Protocol
from uuid import UUID

from pydantic import Field, JsonValue

from question_generator import QuestionGenerationError, YandexGPTClient, YandexGPTSettings
from storage.schemas import QuestionCreate, Schema


PROMPTS = Path(__file__).resolve().parent.parent / "prompts" / "hr_api"


class RequiredCriterion(Schema):
    id: UUID
    name: str
    description: str | None


class QuestionContext(Schema):
    response_id: UUID
    vacancy_title: str
    skills: list[JsonValue]
    required_criteria: list[RequiredCriterion]
    matching: dict[str, JsonValue] | None
    missing_data: list[JsonValue]


class GeneratedQuestions(Schema):
    questions: list[QuestionCreate] = Field(max_length=5)


class QuestionReview(Schema):
    approved: bool
    issues: list[str]


class QuestionProvider(Protocol):
    def generate(self, context: QuestionContext) -> list[QuestionCreate]: ...


class YandexQuestionProvider:
    def __init__(self, client: YandexGPTClient | None = None):
        self.client = client

    def generate(self, context: QuestionContext) -> list[QuestionCreate]:
        if not context.required_criteria:
            return []
        if self.client is None:
            with YandexGPTClient(YandexGPTSettings.from_env()) as client:
                return YandexQuestionProvider(client).generate(context)
        generation = (PROMPTS / "generation.txt").read_text(encoding="utf-8-sig").strip()
        review = (PROMPTS / "review.txt").read_text(encoding="utf-8-sig").strip()
        if not generation or not review:
            raise QuestionGenerationError("Промпт генерации или проверки пуст")
        allowed = {criterion.id for criterion in context.required_criteria}
        feedback = []
        for _ in range(2):
            data = context.model_dump(mode="json")
            raw = self.client.complete(generation, {**data, "validation_feedback": feedback}, GeneratedQuestions)
            try:
                draft = GeneratedQuestions.model_validate_json(raw)
                questions = draft.questions
                if any(question.criterion_id not in allowed for question in questions):
                    raise ValueError("Вопрос должен относиться только к переданному required-критерию")
                normalized = [re.sub(r"\W+", "", q.question.casefold()) for q in questions]
                if len(normalized) != len(set(normalized)):
                    raise ValueError("Вопросы повторяются")
            except ValueError:
                feedback = ["Верни JSON по схеме, только required-критерии и разные вопросы"]
                continue
            if not questions:
                return []
            reviewed = self.client.complete(review, {**data, "questions": draft.model_dump(mode="json")["questions"]}, QuestionReview)
            try:
                decision = QuestionReview.model_validate_json(reviewed)
            except ValueError:
                feedback = ["Проверка не вернула корректный JSON"]
                continue
            if decision.approved and not decision.issues:
                for position, question in enumerate(questions):
                    question.position = position
                return questions
            feedback = decision.issues or ["Проверка отклонила вопросы"]
        raise QuestionGenerationError("Не удалось получить вопросы, прошедшие проверку")
