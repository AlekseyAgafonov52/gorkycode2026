"""Load vacancy text and select only the duties section of a job instruction."""
from __future__ import annotations

import json
import math
import re
from pathlib import Path


DUTIES_HEADING = re.compile(r"^(?:\d+[.)]?\s*)?(?:(?:должностные|функциональные|основные)\s+)?обязанности:?$", re.I)
OTHER_HEADING = re.compile(
    r"^(?:\d+[.)]?\s*)?(?:общие положения|основные критерии оценки деятельности|"
    r"права|ответственность|замещение|квалификационные требования|требования к квалификации|"
    r"взаимоотношения(?: по должности)?|условия работы|заключительные положения|"
    r"лист ознакомления|лист регистрации изменений):?$", re.I)


def extract_instruction_duties(text: str) -> str:
    """Skip TOC entries and stop at the next recognized section; never use full DI."""
    lines = text.splitlines(keepends=True)
    headings = [re.sub(r"\s+", " ", line).strip() for line in lines]
    candidates = []
    for start, heading in enumerate(headings):
        if not DUTIES_HEADING.fullmatch(heading):
            continue
        end = next((i for i in range(start + 1, len(lines)) if OTHER_HEADING.fullmatch(headings[i])), len(lines))
        if any(line.strip() for line in lines[start + 1:end]):
            candidates.append("".join(lines[start:end]).strip())
    if not candidates:
        raise ValueError("В ДИ не найден непустой раздел «Обязанности»; полный документ не используется")
    if len(candidates) != 1:
        raise ValueError("В ДИ найдено несколько разделов обязанностей; уточните структуру документа")
    return candidates[0]


def _decode_txt(data: bytes) -> str:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1251")


def load_vacancy_text(path: str | Path) -> str:
    """Use TXT or original source_text from a parsed vacancy JSON without duplicates."""
    path = Path(path)
    if path.suffix.lower() == ".txt":
        text = _decode_txt(path.read_bytes())
    elif path.suffix.lower() == ".json":
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (ValueError, UnicodeError) as exc:
            raise ValueError("Некорректный JSON вакансии") from exc
        if not isinstance(data, dict):
            raise ValueError("JSON вакансии должен быть объектом")
        text = data.get("source_text")
        if text is None or text == "":
            # Preserve supplied values; labels do not invent qualification facts.
            parts = []
            for key, label in [("title", "Должность"), ("education", "Образование"),
                               ("responsibilities", "Обязанности"), ("required_skills", "Обязательные навыки"),
                               ("preferred_skills", "Желательные навыки"),
                               ("additional_requirements", "Дополнительные требования")]:
                value = data.get(key)
                if value is None or value == [] or value == "":
                    continue
                if isinstance(value, str):
                    parts.append(f"{label}:\n{value}")
                elif isinstance(value, list) and all(isinstance(item, str) for item in value):
                    parts.append(label + ":\n" + "\n".join(value))
                else:
                    raise ValueError(f"Неверный тип поля вакансии: {key}")
            if data.get("experience_min_years") is not None:
                years = data["experience_min_years"]
                if isinstance(years, bool) or not isinstance(years, (int, float)) or not math.isfinite(years) or years < 0:
                    raise ValueError("Неверный минимальный стаж в JSON вакансии")
                parts.append(f"Требуемый опыт, от лет: {years}")
            text = "\n\n".join(parts)
    else:
        raise ValueError("Вакансия должна быть TXT-файлом или JSON из парсера вакансий")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Вакансия не содержит текста")
    if "\x00" in text:
        raise ValueError("Вакансия содержит бинарные данные")
    return text.strip()
