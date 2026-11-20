"""Локальное формирование PDF-отчёта по отклику."""

from __future__ import annotations

import os
import re
import tempfile
from collections.abc import Callable
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import LongTable, Paragraph, SimpleDocTemplate, Spacer, Table

from ranking import PRIORITY_LABEL, STATUS_INK, STATUS_LABEL, TRAFFIC, Assessment, InputError, Vacancy


def register_fonts(regular: Path | None, bold: Path | None) -> None:
    if regular is None:
        windows = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
        candidates = (
            (Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"), Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")),
            (Path("/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf"), Path("/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf")),
            (windows / "arial.ttf", windows / "arialbd.ttf"),
            (Path("/System/Library/Fonts/Supplemental/Arial.ttf"), Path("/System/Library/Fonts/Supplemental/Arial Bold.ttf")),
        )
        for regular_path, bold_path in candidates:
            if regular_path.is_file():
                regular = regular_path
                bold = bold or (bold_path if bold_path.is_file() else regular_path)
                break
    if regular is None:
        raise InputError("Укажите путь к TTF-шрифту с кириллицей через --font.")
    pdfmetrics.registerFont(TTFont("HR", str(regular)))
    pdfmetrics.registerFont(TTFont("HR-Bold", str(bold or regular)))
    pdfmetrics.registerFontFamily("HR", normal="HR", bold="HR-Bold")


def percentage(value: float | None) -> str:
    return "—" if value is None else f"{value:.1f} %".replace(".", ",")


def pdf_paragraph(text: str, size: float = 9.2, color: str = "#1A2941", bold: bool = False,
                  keep_with_next: bool = False) -> Paragraph:
    safe = escape(re.sub(r"[\x00-\x08\x0b-\x1f]", "", text))
    return Paragraph(safe, ParagraphStyle("Report", fontName="HR-Bold" if bold else "HR",
                                          fontSize=size, leading=size * 1.34, textColor=colors.HexColor(color),
                                          keepWithNext=keep_with_next))


def assessment_summary(assessment: Assessment) -> list[str]:
    weighted = sorted((item for item in assessment.findings if item.criterion.weight),
                      key=lambda item: item.criterion.priority != "required")
    confirmed = [item.criterion.name for item in weighted if item.status == "met"]
    missing = [item.criterion.name for item in weighted
               if item.criterion.priority == "required" and item.status != "met"]
    lines = []
    if confirmed:
        lines.append(f"По резюме подтверждены {', '.join(confirmed[:5])}.")
    if missing:
        remaining = f" и ещё {len(missing) - 5}" if len(missing) > 5 else ""
        lines.append(f"Обязательные требования к уточнению — {', '.join(missing[:5])}{remaining}.")
    return lines


def make_pdf(path: Path, vacancy: Vacancy, assessment: Assessment, technical_questions: list[dict] | None = None) -> None:
    ink = "#1A2941"
    muted = "#47566E"
    border = "#D9E2EC"
    label, accent, tint = TRAFFIC[assessment.color]

    p = pdf_paragraph

    doc = SimpleDocTemplate(str(path), pagesize=A4, leftMargin=38, rightMargin=38, topMargin=29, bottomMargin=38,
                            title=f"Отчёт по отклику {assessment.application_id} · {vacancy.title}", author="HR")
    width = doc.width - 12  # Внутренние отступы рамки Platypus.
    identity = " · ".join(filter(None, (vacancy.organization, vacancy.location)))
    story = [p(f"Отчёт по отклику № {assessment.application_id}", 20, bold=True),
             Spacer(1, 4), p(vacancy.title, 12, bold=True)]
    if identity:
        story.append(p(identity, 8.6, muted))
    if assessment.resume_title:
        story.extend([Spacer(1, 7), p(f"Резюме «{assessment.resume_title}»", 8.8, muted)])

    summary = Table([
        [p(label, 10, accent, bold=True), p("Подтверждено", 8.4, muted), p("По проверенным", 8.4, muted), p("Покрытие", 8.4, muted)],
        [p(assessment.reason, 8.8, accent), p(percentage(assessment.confirmed), 14, bold=True),
         p(percentage(assessment.score), 14, bold=True), p(percentage(assessment.coverage), 14, bold=True)],
    ], colWidths=[width * 0.49, width * 0.17, width * 0.17, width * 0.17], style=[
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor(tint)),
        ("BACKGROUND", (1, 0), (-1, -1), colors.HexColor("#F0F4F9")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, 0), 9),
        ("BOTTOMPADDING", (0, -1), (-1, -1), 9),
        ("LINEBEFORE", (0, 0), (0, -1), 3, colors.HexColor(accent)),
    ])
    story.extend([Spacer(1, 12), summary, Spacer(1, 12)])

    question_order = {"application": 0, "condition": 1, "profile": 2, "required": 3, "preferred": 4, "interview": 5}
    pending = sorted((item for item in assessment.findings if item.question),
                     key=lambda item: (question_order[item.criterion.priority], item.status != "mismatch"))
    mandatory = dict.fromkeys(item.question for item in pending if item.criterion.priority in
                              ("application", "condition", "profile", "required"))
    story.append(p("Обязательные уточнения", 11.5, bold=True, keep_with_next=True))
    if mandatory:
        for index, question in enumerate(mandatory, 1):
            story.extend([Spacer(1, 3), p(f"{index}. {question}", 9)])
        story.append(Spacer(1, 12))
    else:
        story.extend([p("По доступным данным обязательных уточнений нет.", 9, muted), Spacer(1, 12)])

    if lines := assessment_summary(assessment):
        story.append(p("Краткая оценка", 11.5, bold=True, keep_with_next=True))
        for line in lines:
            story.append(p(line, 9))
        story.append(Spacer(1, 10))

    optional = dict.fromkeys(item.question for item in pending if item.question not in mandatory)
    if optional:
        story.append(p("Дополнительные уточнения", 11.5, bold=True, keep_with_next=True))
        for question in optional:
            story.append(p(question, 9))
        story.append(Spacer(1, 10))

    story.extend([p("Проверка требований", 11.5, bold=True), Spacer(1, 4)])
    rows = [[p("Критерий", 8.6, "#FFFFFF", True), p("Результат", 8.6, "#FFFFFF", True),
             p("Основание по данным отклика", 8.6, "#FFFFFF", True)]]
    for item in assessment.findings:
        criterion = item.criterion
        priority = PRIORITY_LABEL[criterion.priority]
        if criterion.weight:
            priority += f" · вес {criterion.weight}"
        details = [p(item.detail, 8.2)]
        max_sources = 3 if criterion.kind in ("experience", "application_data") else 2
        for source in item.evidence[:max_sources]:
            details.extend([Spacer(1, 2), p(f"«{source.text}»", 7.8, muted), p(source.path, 6.7, muted)])
        if item.related:
            details.extend([Spacer(1, 2), p("Связанный фрагмент для проверки", 7.8, muted, True),
                            p(f"«{item.related.text}»", 7.8, muted), p(item.related.path, 6.7, muted)])
        status_label = STATUS_LABEL[item.status]
        if criterion.kind == "application_data" and item.status == "met":
            status_label = "Указаны"
        rows.append([[p(criterion.name, 8.5, bold=True), p(priority, 7, muted)],
                     p(status_label, 8, STATUS_INK[item.status], True), details])
    story.append(LongTable(rows, colWidths=[width * 0.30, width * 0.17, width * 0.53], repeatRows=1, style=[
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor(ink)),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F6F8FB")]),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 7),
        ("RIGHTPADDING", (0, 0), (-1, -1), 7),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LINEBELOW", (0, -1), (-1, -1), 0.5, colors.HexColor(border)),
    ]))
    if technical_questions:
        story.extend([Spacer(1, 14),
                      p("Технические вопросы для интервью", 12, bold=True, keep_with_next=True),
                      Spacer(1, 6)])
        for index, item in enumerate(technical_questions, 1):
            story.extend([
                Spacer(1, 8),
                p(f"{index}. {item['question']}", 10, bold=True, keep_with_next=True),
                Spacer(1, 3),
                p(f"Навык: {item['skill']}", 8, muted),
                Spacer(1, 5),
                p("Эталонный ответ", 8.6, muted, bold=True, keep_with_next=True),
                p(item["reference_answer"], 9),
                Spacer(1, 5),
                p("Альтернативные формулировки", 8.6, muted, bold=True, keep_with_next=True),
            ])
            for variant in item["alternative_phrasings"]:
                story.extend([Spacer(1, 2), p("• " + variant, 8.6, muted)])
        story.append(Spacer(1, 10))

    story.extend([Spacer(1, 8), p("Подтверждено — набранные баллы от полного веса требований. "
                                 "По проверенным — баллы от веса оценённых критериев. Покрытие — доля оценённых "
                                 "критериев по весу. ФИО и контакты проверяются отдельно от оценки вакансии. "
                                 "Решение по отклику принимает HR.", 8, muted)])

    def footer(canvas, document):
        canvas.saveState()
        canvas.setStrokeColor(colors.HexColor(border))
        canvas.line(44, 31, A4[0] - 44, 31)
        canvas.setFont("HR", 7.3)
        canvas.setFillColor(colors.HexColor(muted))
        canvas.drawString(44, 20, f"Отклик № {assessment.application_id}")
        canvas.drawRightString(A4[0] - 44, 20, str(document.page))
        canvas.restoreState()

    doc.build(story, onFirstPage=footer, onLaterPages=footer)


def write_atomic(path: Path, render: Callable[[Path], None]) -> None:
    with tempfile.NamedTemporaryFile(dir=path.parent, suffix=path.suffix, delete=False) as temporary:
        temporary_path = Path(temporary.name)
    try:
        render(temporary_path)
        temporary_path.replace(path)
    finally:
        temporary_path.unlink(missing_ok=True)


def write_pdf(path: Path, vacancy: Vacancy, assessment: Assessment, technical_questions: list[dict] | None = None) -> None:
    write_atomic(path, lambda temporary: make_pdf(temporary, vacancy, assessment, technical_questions))


# RESTORED_OFFLINE_TECHNICAL_QUESTIONS_V1
def select_technical_questions(bank: dict, title: str, candidate_id: str) -> list[dict]:
    """Два разных навыка, воспроизводимый выбор без чтения резюме."""
    import hashlib

    if not isinstance(bank, dict) or bank.get("vacancy_title") != title:
        raise ValueError("Банк технических вопросов не соответствует вакансии")
    items = bank.get("questions")
    if not isinstance(items, list):
        raise ValueError("В банке нет списка questions")
    skills: dict[str, list[dict]] = {}
    for question in items:
        if not isinstance(question, dict):
            raise ValueError("Некорректный вопрос в банке")
        skill = question.get("skill")
        text = question.get("question")
        answer = question.get("reference_answer")
        alternatives = question.get("alternative_phrasings")
        if (not isinstance(skill, str) or not skill.strip()
                or not isinstance(text, str) or not text.strip()
                or not isinstance(answer, str) or not answer.strip()
                or not isinstance(alternatives, list) or len(alternatives) != 2
                or any(not isinstance(x, str) or not x.strip() for x in alternatives)):
            raise ValueError("Неполный вопрос: нужны навык, текст, две формулировки и эталонный ответ")
        skills.setdefault(skill.strip().casefold(), []).append(question)
    if len(skills) < 2:
        raise ValueError("В банке должно быть не меньше двух разных навыков")

    seed = title + "\x00" + candidate_id
    def order(value: str) -> bytes:
        return hashlib.sha256((seed + "\x00" + value).encode("utf-8")).digest()

    chosen = []
    for skill in sorted(skills, key=lambda x: order("skill:" + x))[:2]:
        chosen.append(min(skills[skill], key=lambda q: order("question:" + skill + ":"
                             + str(q.get("id", "")) + ":" + q["question"])))
    return chosen
