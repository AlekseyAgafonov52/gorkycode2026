"""Candidate documents from stored results; no model calls or resume truncation."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from contract_models import ContractReport

MAX_DOCUMENT_BYTES = 50 * 1024 * 1024
DELIVERY_STAGES = frozenset({"card", "report", "resume"})


class MaterialsNotReadyError(ValueError):
    def __init__(self):
        super().__init__("Материалы пока недоступны. Нужны передача HR, заполненные рекомендации, исходное резюме и готовый отчёт.")


@dataclass(frozen=True)
class Document:
    filename: str
    content: bytes
    mime_type: str

    def __post_init__(self):
        if not self.content or len(self.content) > MAX_DOCUMENT_BYTES:
            raise MaterialsNotReadyError()


def report_text(report: ContractReport, vacancy_title: str) -> str:
    lines = ["Отчёт по отклику", f"ID: {report.id}", f"Вакансия: {vacancy_title}",
             f"Кандидат: {report.candidate_name or 'ФИО не указано'}",
             f"Оценка: {report.match_label}", f"Совпадение навыков: {report.match_share:.0%}", "", "Итог", *report.summary]
    contacts = report.contacts
    for label, value in (("Телефон", contacts.phone), ("Email", contacts.email), ("Telegram", contacts.telegram)):
        if value:
            lines.append(f"{label}: {value}")
    lines.extend(contacts.other)
    if recommendation := report.recruiter_recommendation:
        lines.extend(["", "Рекомендации рекрутера", f"Сильные стороны: {recommendation.strengths}",
            f"Слабые места: {recommendation.weaknesses}"])
        if recommendation.interview_focus:
            lines.append(f"Проверить на собеседовании: {recommendation.interview_focus}")
    lines.extend(["", "Навыки и вопросы"])
    levels = {"advanced": "Продвинутый", "intermediate": "Средний", "basic": "Базовый"}
    for skill in report.skills:
        lines.extend(["", f"{skill.skill} · {levels.get(skill.resume_level or '', 'Уровень не указан')}", skill.explanation])
        for question in skill.questions:
            lines.append(f"Вопрос: {question.question}")
            if question.expected_answer:
                lines.append(f"Ориентир ответа: {question.expected_answer}")
            if question.follow_up_hint:
                lines.append(f"Уточнение: {question.follow_up_hint}")
    if report.missing_data:
        lines.extend(["", "Недостающие сведения", *(item.text for item in report.missing_data)])
    return "\n".join(lines) + "\n"


def pdf_document(path: Path, filename: str) -> Document:
    try:
        if path.stat().st_size > MAX_DOCUMENT_BYTES:
            raise MaterialsNotReadyError()
        return Document(filename, path.read_bytes(), "application/pdf")
    except OSError:
        raise MaterialsNotReadyError() from None
