"""Deterministic parser for the supplied Russian candidate TXT exports.

No LLM, network calls, document instructions, ranking, or candidate comparison.
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from vacancy_parser import decode_txt


class Schema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Skill(Schema):
    name: str
    level: Literal["advanced", "intermediate", "basic"] | None = None
    original: str


LanguageLevel = Literal["native", "A1", "A2", "B1", "B2", "C1", "C2"]
CEFR_LEVELS: dict[str, LanguageLevel] = {"A1": "A1", "A2": "A2", "B1": "B1", "B2": "B2", "C1": "C1", "C2": "C2"}


class Language(Schema):
    name: str
    level: LanguageLevel | None = None
    description: str | None = None
    original: str


class WorkExperience(Schema):
    start_date: str | None = Field(default=None, pattern=r"^\d{4}-(?:0[1-9]|1[0-2])$")
    end_date: str | None = Field(default=None, pattern=r"^\d{4}-(?:0[1-9]|1[0-2])$")
    is_current: bool | None = None
    duration_months: int | None = Field(default=None, ge=0)
    organization: str | None = None
    position: str | None = None
    employer_details: list[str] = Field(default_factory=list)
    description: str | None = None
    original: str


class EducationEntry(Schema):
    # Some files merge institution and specialty into one line. Do not guess a split.
    institution: str | None = None
    details: list[str] = Field(default_factory=list)
    year: int | None = None
    level: str | None = None
    original: str


class AdditionalInformation(Schema):
    commute: str | None = None
    business_trips: str | None = None
    citizenship: str | None = None
    work_permit: str | None = None
    driving: str | None = None
    self_employment: str | None = None
    medical_book: str | None = None


class OriginalSections(Schema):
    header: str | None = None
    cover_letter: str | None = None
    experience: str | None = None
    skills: str | None = None
    languages: str | None = None
    about_me: str | None = None
    education: str | None = None
    additional: str | None = None


class CandidateResponse(Schema):
    candidate_id: int | None = Field(default=None, ge=1)
    candidate_name: str | None = Field(default=None, max_length=255)
    resume_title: str | None = None
    cover_letter: str | None = None
    experience_total_months: int | None = Field(default=None, ge=0)
    similar_experience_months: int | None = Field(default=None, ge=0)
    work_experience: list[WorkExperience] = Field(default_factory=list)
    skills: list[Skill] = Field(default_factory=list)
    languages: list[Language] = Field(default_factory=list)
    about_me: str | None = None
    education_level: str | None = None
    education: list[EducationEntry] = Field(default_factory=list)
    additional: AdditionalInformation = Field(default_factory=AdditionalInformation)
    original_sections: OriginalSections = Field(default_factory=OriginalSections)
    warnings: list[str] = Field(default_factory=list)
    source_text: str


HEADINGS = [
    ("Сопроводительное письмо", "cover_letter"), ("Опыт работы", "experience"),
    ("Навыки", "skills"), ("Языки", "languages"), ("Обо мне", "about_me"),
    ("Образование", "education"), ("Дополнительно", "additional"),
]
LEVELS = {"Продвинутый уровень": "advanced", "Средний уровень": "intermediate",
          "Базовый уровень": "basic", "Уровень не указан": None}
ADDITIONAL_KEYS = {
    "желательное время в пути до работы": "commute", "командировки": "business_trips",
    "гражданство": "citizenship", "разрешение на работу": "work_permit",
    "опыт вождения": "driving", "самозанятость": "self_employment", "медкнижка": "medical_book",
}
MONTHS = {name: i for i, name in enumerate(
    ["янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"], 1)}
DATE = r"(?:Янв|Фев|Мар|Апр|Май|Июн|Июл|Авг|Сен|Окт|Ноя|Дек)\.?\s+\d{4}"
PERIOD = re.compile(
    rf"(?m)^\s*(?P<start>{DATE})\s*[-–—]\s*(?P<end>{DATE}|сейчас|по настоящее время|настоящее время)\s*$", re.I)
DURATION = re.compile(r"(?:(\d+)\s*(?:лет|года?|г\.?)(?!\w))|(?:(\d+)\s*(?:месяц\w*|мес\.?)(?!\w))", re.I)
MISSING = re.compile(r"^(?:не указано(?: в доступной версии резюме)?|не указан|не указаны|нет данных|—|-)[.!]?$", re.I)

# Exact aliases only: unknown skills and versions remain as written.
ALIASES = {
    "Vue.js": ["vue", "vuejs", "vue.js", "vue js"],
    "Vuex": ["vuex", "vue x"], "Nuxt": ["nuxt", "nuxtjs", "nuxt.js"],
    "React": ["react", "reactjs", "react.js"],
    "Next.js": ["next", "nextjs", "next.js"],
    "Node.js": ["node", "nodejs", "node.js", "node js"],
    "NestJS": ["nestjs", "nest.js", "nest js"],
    "JavaScript": ["javascript", "java script", "js"],
    "TypeScript": ["typescript", "type script", "ts"],
    "HTML": ["html", "html5"], "CSS": ["css", "css3"],
    "Sass": ["sass"], "SCSS": ["scss"], "Pinia": ["pinia"],
    "Python": ["python", "программирование python"], "Java": ["java"],
    "pandas": ["pandas"], "NumPy": ["numpy"], "matplotlib": ["matplotlib"],
    "seaborn": ["seaborn"], "scikit-learn": ["scikit-learn", "sklearn", "scikit learn"],
    "PyTorch": ["pytorch"], "TensorFlow": ["tensorflow"],
    "PostgreSQL": ["postgresql", "postgres", "postgre sql"],
    "MySQL": ["mysql"], "SQLite": ["sqlite", "sqlite3"],
    "SQLAlchemy": ["sqlalchemy", "sql alchemy"],
    "FastAPI": ["fastapi", "fast api"], "Django": ["django", "django framework"],
    "Django REST Framework": ["django rest framework", "drf"],
    "Flask": ["flask"], "pytest": ["pytest"], "aiogram": ["aiogram"],
    "Git": ["git"], "GitHub": ["github"], "GitLab": ["gitlab"],
    "Docker": ["docker"], "Docker Compose": ["docker-compose", "docker compose"],
    "Linux": ["linux"], "Nginx": ["nginx"], "Redis": ["redis"],
    "REST API": ["rest", "rest api", "restapi", "restful api"],
    "HTTP": ["http"], "WebSocket": ["websocket", "websockets", "web-socket"],
    "GraphQL": ["graphql"], "CI/CD": ["ci/cd", "ci / cd"],
    "Postman": ["postman"], "Swagger": ["swagger"],
    "Apache Airflow": ["airflow", "apache airflow"],
    "Yandex DataLens": ["yandex.datalens", "yandex datalens", "datalens"],
    "Figma": ["figma"], "Adobe Photoshop": ["photoshop", "adobe photoshop"],
    "Adobe Illustrator": ["illustrator", "adobe illustrator"],
    "CorelDRAW": ["coreldraw", "corel draw"], "Adobe XD": ["adobe xd"],
    "Adobe InDesign": ["indesign", "adobe indesign"],
    "Adobe After Effects": ["after effects", "adobe after effects"],
    "Blender": ["blender"], "Sketch": ["sketch"], "Miro": ["miro"],
    "FigJam": ["figjam"], "Webflow": ["webflow"], "Tilda": ["tilda"],
    "Jira": ["jira", "atlassian jira"],
    "Confluence": ["confluence", "atlassian confluence"],
    "Apache JMeter": ["jmeter", "apache jmeter"],
    "Selenium": ["selenium"], "Playwright": ["playwright"], "Cypress": ["cypress"],
    "TestRail": ["testrail", "test rail"], "Test IT": ["test it", "testit"],
    "Kubernetes": ["kubernetes", "k8s"], "DBeaver": ["dbeaver"],
}
ALIAS_LOOKUP = {alias.casefold(): name for name, aliases in ALIASES.items() for alias in aliases}


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _nullable(text: str | None, *, cover: bool = False) -> str | None:
    if not text or MISSING.fullmatch(_clean(text)):
        return None
    if cover and _clean(text).startswith("Кандидат не прислал сопроводительное, потому что его пригласил ИИ-помощник."):
        return None
    return text.strip()


def _lines(text: str | None) -> list[str]:
    return [s.strip() for s in (text or "").splitlines() if s.strip()]


def parse_duration(text: str) -> int | None:
    """Convert only an explicit duration; missing experience is not zero."""
    if re.fullmatch(r"без опыта(?: работы)?|нет опыта(?: работы)?|опыт отсутствует", _clean(text), re.I):
        return 0
    found = list(DURATION.finditer(text))
    return sum(int(m[1] or 0) * 12 + int(m[2] or 0) for m in found) if found else None


def normalize_skill(text: str) -> str:
    text = _clean(text)
    # Keep version in the normalized value, e.g. Vue 3 -> Vue.js 3.
    versioned = re.fullmatch(r"(vue(?:\.?js)?|python)\s*(\d+(?:[./]\d+)*)", text, re.I)
    if versioned:
        return f"{ALIAS_LOOKUP.get(versioned[1].casefold(), versioned[1])} {versioned[2]}"
    return ALIAS_LOOKUP.get(text.casefold(), text)


def _split_sections(text: str, warnings: list[str]) -> OriginalSections:
    # Accept headings in export order. Earlier headings inside About Me stay prose.
    # Education has an extra anchor: 'Уровень' on the next nonempty line.
    lines = text.lstrip("\ufeff").splitlines(keepends=True)
    labels = [line.strip() for line in lines]
    following_lines = [""] * len(lines)
    following = ""
    for i in range(len(lines) - 1, -1, -1):
        following_lines[i] = following
        if labels[i]:
            following = labels[i]
    # Exported languages start with a native/other-language marker. Anchor the
    # skills list to that section, not to 'Навыки' inside a job description.
    language_indices = [i for i, label in enumerate(labels) if label == "Языки"
                        and (following_lines[i] in ("Родной", "Другие языки")
                             or MISSING.fullmatch(following_lines[i]))]
    language_index = language_indices[0] if language_indices else None
    skill_indices = [i for i, label in enumerate(labels) if label == "Навыки"
                     and (language_index is None or i < language_index)]
    skill_index = skill_indices[-1] if skill_indices else None
    sections = OriginalSections()
    current, first, expected = "header", 0, 0
    positions = []
    for index, line in enumerate(lines):
        label = line.strip()
        target = next((i for i in range(expected, len(HEADINGS)) if HEADINGS[i][0] == label), None)
        if target is None:
            continue
        following = following_lines[index]
        if label == "Навыки" and index != skill_index:
            continue
        if label == "Языки" and language_index is not None and index != language_index:
            continue
        if label == "Обо мне" and language_index is not None and index < language_index:
            continue
        if label == "Образование" and following != "Уровень":
            continue
        if label == "Дополнительно" and not any(
            following.casefold().startswith(key) for key in ADDITIONAL_KEYS
        ):
            continue
        if label == "Опыт работы" and not (following.startswith("Опыт работы:") or MISSING.fullmatch(following) or following in ("Без опыта", "Без опыта работы")):
            continue
        setattr(sections, current, "".join(lines[first:index]).strip() or None)
        positions.append(target)
        current, first, expected = HEADINGS[target][1], index + 1, target + 1
    setattr(sections, current, "".join(lines[first:]).strip() or None)
    for i, (label, _) in enumerate(HEADINGS):
        if i not in positions:
            warnings.append(f"Не найден раздел: {label}")
    return sections


def _skills(text: str | None) -> list[Skill]:
    result = []
    level = None
    for line in _lines(text):
        if line in LEVELS:
            level = LEVELS[line]
        elif not MISSING.fullmatch(line):
            result.append(Skill(name=normalize_skill(line), level=level, original=line))
    return result


def _languages(text: str | None) -> list[Language]:
    result = []
    native = False
    for line in _lines(text):
        if line == "Родной":
            native = True
            continue
        if line == "Другие языки":
            native = False
            continue
        if MISSING.fullmatch(line):
            continue
        parts = re.split(r"\s+[—–-]\s+", line, maxsplit=2)
        level = CEFR_LEVELS.get(parts[1]) if len(parts) > 1 else None
        result.append(Language(name=parts[0], level="native" if native else level,
                               description=parts[2] if len(parts) > 2 else None, original=line))
    return result


ROLE = re.compile(
    r"\b(?:разработчик|developer|программист|инженер|аналитик|администратор|менеджер|стаж[её]р|"
    r"техник\b|специалист|тестировщик|дизайнер|консультант|руководител|директор|оператор|"
    r"ассистент|учител|преподавател|начальник|исследовател|лаборант|intern|engineer|analyst|"
    r"scientist|devops|team\s*lead|тимлид|системный архитектор|верстальщик|студент|продавец|"
    r"диспетчер|контрол[её]р|дефектоскопист|заведующий|владелец|барбер|экономист|эксперт|"
    r"водител|помощник|делопроизводител|научный сотрудник|обучение в магистратуре|стропальщик|"
    r"DA/DS|банкир|стрелок|младшый сотрудник|педагог|бармен|официант|бариста|монтажник|слесарь|"
    r"торговый представитель|замерщик|мастер|работник|сотрудник|продюсер|вожатый|куратор|технолог\b|"
    r"IT Specialist|асутп|репетитор|кладовщик|логист|ревизор|документовед|assistant|designer|"
    r"specialist|tutor|economist|director|системотехник|практикант|full-stack|методист|глава клуба|"
    r"учебная практика|проектная занятость|медицинский физик|курсант|инспектор|доцент|ведущий занятий|"
    r"ответственный за документооборот|акт[её]р|сборщик|manager|product lead|бухгалтер|электромеханик|"
    r"QA\b|tester|design\b|UX/UI\b|UI/UX\b|programmer|backend\b|фотограф|художник|дизйнер|"
    r"[сc]пециалист|рук\.|фриланс|freelanc|предпринимател|coach|student|архитектор|"
    r"электрик|электромонт[её]р|электромонтажник|электрогазосварщик|градостроител|координатор|"
    r"переводчик|wissenschaftlicher mitarbeiter|инструктор|психолог|волонт[её]р|юрист|юрисконсульт|"
    r"монт[её]р|видеооператор|уборщик|воспитател|отделочник|наставник|редактор|моделировщик|"
    r"управляющ|хостес|кассир|ментор|иллюстратор|фельдшер|су-шеф|хореограф|контекстолог|"
    r"врач|аппаратчик|микробиолог|аудитор|гид\b|секретарь|агент|экномист|tech-support|"
    r"дизайн интерфейсов|дизайн мобильных приложений|проектировщик|lead\b|скриптолог|"
    r"administrator|старший смены|асессор|[мm]енеджер|[мm]едицинский представитель|"
    r"developter|светооператор|ревьюер|самозанятый)", re.I)


def _iso_date(text: str) -> str | None:
    match = re.fullmatch(r"([А-Яа-я]+)\.?\s+(\d{4})", text)
    if not match:
        return None
    month = MONTHS.get(match[1].casefold())
    return f"{match[2]}-{month:02d}" if month else None


def _work(text: str | None, warnings: list[str]) -> list[WorkExperience]:
    text = _nullable(text)
    if text is None:
        return []
    periods = list(PERIOD.finditer(text))
    result = []
    for i, match in enumerate(periods):
        end = periods[i + 1].start() if i + 1 < len(periods) else len(text)
        original = text[match.start():end].strip()
        body = _lines(text[match.end():end])
        months = None
        if body and DURATION.search(body[0]):
            months = parse_duration(body.pop(0))
        employer = body.pop(0) if body else None
        # Both anonymized and actual employer names occupy this fixed line.
        if employer is None:
            warnings.append(f"Не указана организация в месте работы {i + 1}")
        # Employer city, site and industry are optional and have no labels.
        # Only select a short, explicit occupation line; keep ambiguous lines intact.
        role_index = next((j for j, line in enumerate(body[:5])
                           if len(line) <= 160 and ROLE.search(line)
                           and not re.search(r"[!?;]$|^[-—•]|https?://", line)), None)
        position = body[role_index] if role_index is not None else None
        if role_index is None:
            warnings.append(f"Не выделена должность в месте работы {i + 1}; строки сохранены в description")
        details = body[:role_index] if role_index is not None else []
        description_lines = body[role_index + 1:] if role_index is not None else body
        end_date = _iso_date(match["end"])
        result.append(WorkExperience(start_date=_iso_date(match["start"]), end_date=end_date,
                                     is_current=end_date is None, duration_months=months,
                                     organization=employer, position=position,
                                     employer_details=details,
                                     description="\n".join(description_lines) or None, original=original))
    if not periods and _nullable(text) and not re.search(r"без опыта|нет опыта", text, re.I):
        warnings.append("Не распознаны периоды работы; исходный раздел сохранён")
    return result


def _education(text: str | None, warnings: list[str]) -> tuple[str | None, list[EducationEntry]]:
    raw_lines = (text or "").splitlines(keepends=True)
    meaningful = [i for i, line in enumerate(raw_lines) if line.strip()]
    if meaningful and raw_lines[meaningful[0]].strip() == "Уровень":
        meaningful.pop(0)
    if not meaningful:
        return None, []
    level_index = meaningful[0]
    level = _nullable(raw_lines[level_index].strip())
    # Same record layout with bullets inline or on separate lines:
    # institution \n details \n [•] year • education level.
    body = "".join(raw_lines[level_index + 1:])
    ending = re.compile(r"(?m)(?:^|[\s•])(?P<year>(?:19|20)\d{2})\s*•\s*(?P<level>[^\r\n]+)(?:\r?\n|$)")
    entries, start = [], 0
    for match in ending.finditer(body):
        prefix = body[start:match.start()].strip(" \r\n•")
        record_lines = _lines(prefix)
        entries.append(EducationEntry(institution=record_lines[0] if record_lines else None,
                                      details=record_lines[1:], year=int(match["year"]),
                                      level=match["level"].strip(), original=body[start:match.end()].strip()))
        start = match.end()
    tail = body[start:].strip(" \r\n•")
    if tail:
        entries.append(EducationEntry(original=tail))
        warnings.append("Не полностью распознана запись образования; текст сохранён в education.original")
    return level, entries


def _additional(text: str | None) -> AdditionalInformation:
    result = AdditionalInformation()
    current, values = None, []
    for line in _lines(text) + ["__END__:"]:
        # A new labeled field, even unknown, ends the previous value.
        match = re.match(r"^(.+?)\s*:\s*(.*)$", line)
        if match:
            if current:
                setattr(result, current, _nullable("\n".join(values)))
            current = ADDITIONAL_KEYS.get(_clean(match[1]).casefold())
            values = [match[2]] if match[2] else []
        elif current:
            values.append(line)
    return result


def parse_response(text: str) -> CandidateResponse:
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Файл отклика пуст")
    if "\x00" in text:
        raise ValueError("Файл содержит бинарные данные")
    result = CandidateResponse(source_text=text)
    sections = _split_sections(text, result.warnings)
    result.original_sections = sections
    header = sections.header or ""
    candidate = re.search(r"(?m)^Кандидат\s*№\s*(\d+)\s*$", header)
    if candidate:
        result.candidate_id = int(candidate[1])
    else:
        result.warnings.append("Не найден номер кандидата")
    title = re.search(r"(?m)^Резюме\s*:\s*([^\r\n]+)", header)
    result.resume_title = _nullable(title[1]) if title else None
    if result.resume_title is None:
        result.warnings.append("Не найдено название резюме")
    result.cover_letter = _nullable(sections.cover_letter, cover=True)
    result.about_me = _nullable(sections.about_me)
    for label, attr in [("Опыт работы", "experience_total_months"), ("Опыт в похожих должностях", "similar_experience_months")]:
        match = re.search(rf"(?m)^{label}\s*:\s*([^\r\n]+)", sections.experience or "")
        if match:
            duration = parse_duration(match[1])
            setattr(result, attr, duration)
            if duration is None and not MISSING.fullmatch(match[1].strip()):
                result.warnings.append(f"Не распознана длительность: {label}")
    if re.fullmatch(r"без опыта(?: работы)?|нет опыта(?: работы)?", sections.experience or "", re.I):
        result.experience_total_months = 0
    result.work_experience = _work(sections.experience, result.warnings)
    result.skills = _skills(sections.skills)
    result.languages = _languages(sections.languages)
    result.education_level, result.education = _education(sections.education, result.warnings)
    result.additional = _additional(sections.additional)
    return result


def parse_response_file(path: str | Path) -> CandidateResponse:
    path = Path(path)
    if path.suffix.lower() != ".txt":
        raise ValueError("Ожидается TXT-файл")
    return parse_response(decode_txt(path.read_bytes()))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    serialized = parse_response_file(args.file).model_dump_json(indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized, encoding="utf-8")
    else:
        print(serialized)
