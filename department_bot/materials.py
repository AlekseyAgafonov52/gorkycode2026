"""Candidate documents from stored results; no model calls or resume truncation."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

import pymupdf

from contract_models import ContractReport

MAX_DOCUMENT_BYTES = 50 * 1024 * 1024
DELIVERY_STAGES = frozenset({"packet"})


class MaterialsNotReadyError(ValueError):
    def __init__(self):
        super().__init__("Материалы пока недоступны. Нужны передача HR, исходное резюме и готовый отчёт.")


@dataclass(frozen=True)
class Document:
    filename: str
    content: bytes
    mime_type: str

    def __post_init__(self):
        if not self.content or len(self.content) > MAX_DOCUMENT_BYTES:
            raise MaterialsNotReadyError()


def report_text(report: ContractReport, vacancy_title: str) -> str:
    lines = ["Отчёт по отклику", f"Вакансия: {vacancy_title}",
             f"Кандидат: {report.candidate_name or 'ФИО не указано'}",
             f"Оценка: {report.match_label}", f"Совпадение навыков: {report.match_share:.0%}", "", "Итог", *report.summary]
    contacts = report.contacts
    for label, value in (("Телефон", contacts.phone), ("Email", contacts.email), ("Telegram", contacts.telegram)):
        if value:
            lines.append(f"{label}: {value}")
    lines.extend(contacts.other)
    if recommendation := report.recruiter_recommendation:
        notes = [("Сильные стороны", recommendation.strengths), ("Слабые места", recommendation.weaknesses),
                 ("Проверить на собеседовании", recommendation.interview_focus), ("Заметки", recommendation.notes)]
        filled = [f"{label}: {value}" for label, value in notes if value]
        if filled:
            lines.extend(["", "Рекомендации рекрутера", *filled])
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


def resume_text(source: str) -> str:
    # Remove the export's identifier header, preserving the candidate's narrative.
    header = re.compile(r"^(?:Кандидат\s*[№#]\s*\d+|(?:ID\s+кандидата|candidate[_ ]id)\s*[:=]\s*[\w-]+)[ \t]*$", re.IGNORECASE)
    return "".join(line for number, line in enumerate(source.splitlines(keepends=True))
                   if number >= 20 or not header.fullmatch(line.rstrip("\r\n")))


def pdf_document(path: Path, filename: str) -> Document:
    try:
        if path.stat().st_size > MAX_DOCUMENT_BYTES:
            raise MaterialsNotReadyError()
        with pymupdf.open(path) as pdf:
            # Exported PDFs have the response number in their heading and footer.
            pattern = re.compile(r"(?:Отч[её]т\s+по\s+отклику|Отклик|Candidate\s+ID|Response)\s*[:№#]?\s*\d+", re.IGNORECASE)
            for page in pdf:
                changed = False
                text = page.get_text("text")
                if not isinstance(text, str):
                    raise MaterialsNotReadyError()
                for match in pattern.finditer(text):
                    suffix = re.search(r"[:№#]?\s*\d+$", match.group())
                    assert suffix is not None
                    for rectangle in page.search_for(match.group()):
                        targets = [rectangle] if match.group().lower().startswith("candidate") else page.search_for(suffix.group().strip(), clip=rectangle + (-1, -1, 1, 1))
                        for identifier in targets:
                            page.add_redact_annot(identifier, fill=(1, 1, 1))
                            changed = True
                if changed:
                    page.apply_redactions(images=0, graphics=0)
            pdf.set_metadata({**(pdf.metadata or {}), "title": "Отчёт по отклику", "subject": "", "keywords": ""})
            pdf.del_xml_metadata()
            pdf.set_toc([])
            return Document(filename, pdf.tobytes(garbage=4, deflate=True), "application/pdf")
    except (OSError, RuntimeError, ValueError):
        raise MaterialsNotReadyError() from None
