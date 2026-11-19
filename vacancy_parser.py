"""Conservative, offline vacancy extraction. Document contents are data, never commands."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class OriginalRequirements(BaseModel):
    required: list[str] = Field(default_factory=list)
    preferred: list[str] = Field(default_factory=list)
    education: list[str] = Field(default_factory=list)
    experience: list[str] = Field(default_factory=list)


class Vacancy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str | None = None
    organization: str | None = None
    location: str | None = None
    work_format: Literal["onsite", "remote", "hybrid"] | None = None
    experience_min_years: float | None = Field(default=None, ge=0)
    education: str | None = None
    responsibilities: list[str] = Field(default_factory=list)
    required_skills: list[str] = Field(default_factory=list)
    preferred_skills: list[str] = Field(default_factory=list)
    additional_requirements: list[str] = Field(default_factory=list)
    original_requirements: OriginalRequirements = Field(default_factory=OriginalRequirements)
    source_text: str = ""


# Longest/specific aliases first. Version suffixes remain in original_requirements.
TECHNOLOGIES = {
    "JavaScript": r"JavaScript|JS",
    "TypeScript": r"TypeScript|TS",
    "Vue.js": r"Vue(?:\s*\.?\s*js)?",
    "Nuxt": r"Nuxt(?:\.?js)?",
    "React": r"React(?:\.?js)?",
    "Node.js": r"Node\.?js",
    "HTML": r"HTML5?", "CSS": r"CSS3?", "SASS": r"SASS|SCSS",
    "Pinia": r"Pinia", "Git": r"Git", "DevTools": r"DevTools",
    "Storybook": r"Storybook", "Python": r"Python",
    "SQL": r"SQL", "PostgreSQL": r"PostgreSQL|Postgres",
    "pandas": r"pandas", "matplotlib": r"matplotlib", "seaborn": r"seaborn",
    "Linux": r"Linux", "ETL": r"ETL", "Figma": r"Figma",
    "Photoshop": r"Photoshop", "Illustrator": r"Illustrator",
    "CorelDRAW": r"CorelDraw", "Docker": r"Docker",
    "FastAPI": r"FastAPI", "Django": r"Django",
    "Yandex DataLens": r"Yandex[. ]DataLens|DataLens",
    "Apache Airflow": r"(?:Apache\s+)?Airflow",
}
PATTERNS = [(name, re.compile(r"(?<![\w])(?:" + pattern + r")(?![\w])", re.I))
            for name, pattern in TECHNOLOGIES.items()]


def _append(items: list[str], value: str) -> None:
    if value and value not in items:
        items.append(value)


def _technologies(text: str) -> list[str]:
    matches = [(m.start(), name) for name, pattern in PATTERNS
               for m in pattern.finditer(text)]
    return list(dict.fromkeys(name for _, name in sorted(matches)))


def _section(line: str) -> str | None:
    label = line.strip().rstrip(":").casefold()
    if re.fullmatch(r"(?:обязанности|должностные обязанности|задачи|чем предстоит заниматься|мы ожидаем, что вы умеете работать с такими задачами|responsibilities)", label):
        return "responsibilities"
    if re.fullmatch(r"(?:требования|обязательные требования|что мы жд[её]м от тебя|что мы ожидаем|requirements|required skills)", label):
        return "required"
    if re.fullmatch(r"(?:будет преимуществом|будет плюсом|желательно|желательные навыки|preferred skills)", label):
        return "preferred"
    if label in ("ключевые навыки", "key skills"):
        return "keys"
    if label in ("образование", "требования к образованию"):
        return "education"
    if label in ("дополнительные требования", "additional requirements"):
        return "additional"
    if label == "о компании" or re.match(r"^(?:условия|мы предлагаем|где предстоит работать|контакты|benefits)(?:\b|$)", label):
        return "ignore"
    return None


def parse_vacancy(text: str) -> Vacancy:
    """Extract explicit facts; unknown prose is retained in source_text.

    Rule-based extraction cannot understand arbitrary prose. Unrecognized facts
    are left empty instead of inferred from a job title or employer context.
    """
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Файл вакансии пуст")
    if "\x00" in text:
        raise ValueError("Файл содержит бинарные данные")
    vacancy = Vacancy(source_text=text)
    lines = [re.sub(r"^\s*(?:[-*•●]\s+|\d+[.)]\s+)", "", line).strip()
             for line in text.lstrip("\ufeff").splitlines()]
    lines = [line for line in lines if line and line != "·"]
    section = None
    for index, line in enumerate(lines):
        heading = _section(line)
        if heading:
            section = heading
            continue
        field = re.match(r"^(название вакансии|должность|вакансия|title|организация|компания|работодатель|organization|город|местоположение|локация|location)\s*:\s*(.+)$", line, re.I)
        if field:
            key, value = field.groups()
            target = ("title" if key.casefold() in ("название вакансии", "должность", "вакансия", "title") else
                      "organization" if key.casefold() in ("организация", "компания", "работодатель", "organization") else "location")
            if getattr(vacancy, target) is None:
                setattr(vacancy, target, value)
            continue
        if index == 0 and section is None:
            vacancy.title = line
            continue
        # HeadHunter exports put the city directly after the salary line.
        if index > 0 and re.search(r"уровень дохода|₽|руб\.|зарплата", lines[index - 1], re.I) and re.fullmatch(r"[А-ЯЁA-Z][\w .()–-]+", line) and not re.search(r"опыт|занятость", line, re.I):
            vacancy.location = vacancy.location or line
        organization = re.search(r'(?:АНО|ООО|АО|ПАО|ИП)\s+["«][^"»]+["»]', line)
        if organization and vacancy.organization is None:
            vacancy.organization = organization.group()
        if re.search(r"(?:опыт|experience)", line, re.I):
            match = re.search(r"(?:от\s*)?(\d+(?:[.,]\d+)?)\s*(?:[–—-]\s*\d+\s*)?(лет|год\w*|месяц\w*|years?|months?)", line, re.I)
            if match:
                years = float(match[1].replace(",", ".")) / (12 if re.match(r"месяц|month", match[2], re.I) else 1)
                vacancy.experience_min_years = max(vacancy.experience_min_years or 0, years)
                _append(vacancy.original_requirements.experience, line)
            elif re.search(r"опыт\s+не\s+требуется|без опыта|no experience", line, re.I):
                if vacancy.experience_min_years is None:
                    vacancy.experience_min_years = 0
                _append(vacancy.original_requirements.experience, line)
        if section == "responsibilities":
            _append(vacancy.responsibilities, line)
        elif section in ("required", "preferred", "keys"):
            preferred = section == "preferred" or bool(re.search(r"желательно|будет плюсом|будет преимуществом", line, re.I))
            originals = vacancy.original_requirements.preferred if preferred else vacancy.original_requirements.required
            _append(originals, line)
            skills = _technologies(line)
            destination = vacancy.preferred_skills if preferred else vacancy.required_skills
            for skill in skills:
                _append(destination, skill)
            # Preserve non-technology professional skills and soft requirements.
            if not skills or re.search(r"портфолио|готовность|внимательность|желание|английск|понимание HTTP", line, re.I):
                _append(vacancy.preferred_skills if preferred else vacancy.additional_requirements, line)
        elif section == "additional":
            _append(vacancy.additional_requirements, line)
        if section == "education" or (section in ("required", "preferred", None) and re.search(r"(?:высшее|среднее|профильное|техническое)\s+образование|степень бакалавра|degree", line, re.I)):
            _append(vacancy.original_requirements.education, line)
    vacancy.education = "; ".join(vacancy.original_requirements.education) or None
    # Look only in explicit format/conditions lines, never in required experience.
    format_lines = [line for line in lines if re.search(r"формат|на месте работодателя|работа в офисе|удал[её]нн|гибрид|remote|onsite|hybrid", line, re.I)]
    formats = " ".join(format_lines)
    if re.search(r"гибрид|hybrid", formats, re.I):
        vacancy.work_format = "hybrid"
    elif re.search(r"удал[её]нн|remote", formats, re.I):
        vacancy.work_format = "remote"
    elif re.search(r"на месте работодателя|работа в офисе|офисный формат|onsite", formats, re.I):
        vacancy.work_format = "onsite"
    # An explicit mandatory mention takes precedence over a preferred one.
    vacancy.preferred_skills = [s for s in vacancy.preferred_skills if s not in vacancy.required_skills]
    return vacancy


def decode_txt(data: bytes) -> str:
    """UTF-8 (with BOM), UTF-16 with BOM, or Windows-1251."""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1251")


def parse_vacancy_file(path: str | Path) -> Vacancy:
    path = Path(path)
    if path.suffix.lower() != ".txt":
        raise ValueError("Ожидается TXT-файл")
    return parse_vacancy(decode_txt(path.read_bytes()))


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", type=Path)
    args = parser.parse_args()
    print(parse_vacancy_file(args.file).model_dump_json(indent=2))
