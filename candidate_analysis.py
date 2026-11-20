"""Offline parse_resume, extract_vacancy_skills and match from the MVP contract."""
from __future__ import annotations

import re
from collections.abc import Mapping

from pydantic import JsonValue

from contract_models import Contacts, DraftQuestion, MatchResult, MissingField, ResumeData, ResumeSkill, SkillMatch
from response_parser import ALIASES, normalize_skill, parse_response
from vacancy_parser import parse_vacancy


GREEN_THRESHOLD = 0.7
LEVEL_NAMES = {"advanced": "продвинутом", "intermediate": "среднем", "basic": "базовом"}


def canonical_skill(value: str) -> str:
    name = normalize_skill(value.strip())
    version = re.fullmatch(r"(Vue\.js|Python)\s*\d+(?:[./]\d+)*", name, re.I)
    return normalize_skill(version[1]) if version else name


def extract_contacts(text: str) -> Contacts:
    email = re.search(r"(?<![\w.+-])([\w.+-]+@[\w-]+(?:\.[\w-]+)+)", text)
    phone = None
    for value in re.findall(r"(?<!\w)(?:\+7|8)[\d\s()–—-]{9,22}\d", text):
        if len(re.sub(r"\D", "", value)) == 11:
            phone = value.strip()
            break
    telegram = re.search(r"(?:https?://)?t\.me/([A-Za-z][\w]{3,})", text)
    if telegram is None:
        telegram = re.search(r"(?i)(?<!\w)(?:telegram|телеграм|tg)\s*:\s*@?([A-Za-z][\w]{3,})", text)
    links = list(dict.fromkeys(value.rstrip(".,;)") for value in re.findall(r"https?://[^\s<>]+", text)))
    return Contacts(phone=phone, email=email[1] if email else None,
                    telegram="@" + telegram[1] if telegram else None,
                    other=[value for value in links if "t.me/" not in value])


def _name(text: str) -> str | None:
    found = re.search(r"(?im)^(?:ФИО|Фамилия\s+Имя(?:\s+Отчество)?|candidate_name|full_name|name)\s*:\s*([^\r\n]+)", text)
    return found[1].strip() if found and found[1].strip() else None


def _resume(source: Mapping[str, JsonValue]) -> ResumeData:
    text = source.get("source_text")
    text = text if isinstance(text, str) else ""
    skills = []
    source_skills = source.get("skills")
    for value in source_skills if isinstance(source_skills, list) else []:
        if isinstance(value, str):
            skills.append(ResumeSkill(name=canonical_skill(value)))
        elif isinstance(value, dict):
            skill_name = value.get("name")
            if isinstance(skill_name, str):
                skills.append(ResumeSkill.model_validate({"name": canonical_skill(skill_name), "level": value.get("level")}))
    name = source.get("candidate_name") or source.get("full_name") or _name(text)
    contacts = source.get("contacts")
    months = source.get("experience_total_months", source.get("experience_months"))
    result = ResumeData.model_validate({"candidate_name": name if isinstance(name, str) and name.strip() else None,
        "contacts": contacts if isinstance(contacts, dict) else extract_contacts(text).model_dump(),
        "title": source.get("resume_title", source.get("title")),
        "experience_months": months, "skills": skills,
        "education": source.get("education_level") or (source.get("education") if isinstance(source.get("education"), str) else None),
        "about": source.get("about_me", source.get("about"))})
    result._source_text = text
    similar = source.get("similar_experience_months")
    result._similar_experience_months = similar if isinstance(similar, int) and not isinstance(similar, bool) else None
    return result


def resume_from_json(source: Mapping[str, JsonValue]) -> ResumeData:
    """Use existing structured JSON without rerunning Resume Parser."""
    return _resume(source)


def parse_resume(text: str) -> ResumeData:
    return _resume(parse_response(text).model_dump(mode="json"))


def extract_vacancy_skills(vacancy_text: str) -> list[str]:
    vacancy = parse_vacancy(vacancy_text)
    values: list[str] = []
    # Unknown technology tags must not disappear just because they lack an alias.
    keys = False
    for line in vacancy_text.splitlines():
        label = line.strip().rstrip(":").casefold()
        if label in ("ключевые навыки", "key skills"):
            keys = True
            continue
        if label in ("условия", "о компании", "контакты", "обязанности", "требования", "где предстоит работать"):
            keys = False
        if keys:
            values.extend(part.strip(" •;,-") for part in re.split(r"[,;]", line) if part.strip(" •;,-"))
    # Explicit tags are the vacancy's own concise skill list. Requirements are a
    # fallback for exports without tags, not a second source of duplicate criteria.
    if not values:
        values = vacancy.required_skills + vacancy.preferred_skills
        for line in vacancy.original_requirements.required + vacancy.original_requirements.preferred:
            if re.fullmatch(r"[A-Za-z0-9.+#_/-]+(?:\s+[A-Za-z0-9.+#_/-]+){0,3}[.;]?", line):
                values.append(line.rstrip(".;"))
    result = []
    seen = set()
    for value in values:
        name = canonical_skill(value)
        if name and name.casefold() not in seen:
            seen.add(name.casefold())
            result.append(name)
    return result


def _mentioned(skill: str, text: str) -> bool:
    aliases = [skill] + ALIASES.get(skill, [])
    if skill == "Vue.js":
        aliases += ["Vue 2", "Vue 3"]
    return any(re.search(r"(?<!\w)" + re.escape(alias) + r"(?!\w)", text, re.I) for alias in aliases)


def _experience(months: int | None) -> str:
    if months is None:
        return "не указан"
    years, remainder = divmod(months, 12)
    return f"{years} г. {remainder} мес."


def match(resume: ResumeData, vacancy_skills: list[str]) -> MatchResult:
    levels = {None: 0, "basic": 1, "intermediate": 2, "advanced": 3}
    declared = {}
    for skill in resume.skills:
        key = canonical_skill(skill.name).casefold()
        if key not in declared or levels[skill.level] > levels[declared[key].level]:
            declared[key] = skill
    skills = []
    names = list(dict.fromkeys(canonical_skill(name) for name in vacancy_skills))
    for name in names:
        explicit = declared.get(name.casefold())
        level = explicit.level if explicit else None
        if explicit:
            relevance = "yellow" if level == "basic" else "green"
            explanation = (f"Указан в навыках на {LEVEL_NAMES[level]} уровне." if level
                           else "Указан в навыках; уровень не указан.")
        elif _mentioned(name, resume._source_text + "\n" + (resume.about or "") + "\n" + (resume.title or "")):
            relevance, explanation = "yellow", "Упомянут в тексте резюме, но не выделен в списке навыков."
        else:
            relevance, explanation = "red", "В резюме не упомянут."
        questions = [DraftQuestion(kind="missing", question=f"Работали ли вы с {name}? Расскажите, где и как.")] if relevance == "red" else []
        skills.append(SkillMatch.model_validate({"skill": name, "resume_level": level, "relevance": relevance,
                                                "explanation": explanation, "questions": questions}))
    share = sum(skill.relevance != "red" for skill in skills) / len(skills) if skills else 0.0
    contacts = resume.contacts
    status = "red" if not (resume.candidate_name or "").strip() or not any((contacts.phone, contacts.email, contacts.telegram)) else (
        "green" if share >= GREEN_THRESHOLD else "yellow")
    missing = [skill.skill for skill in skills if skill.relevance == "red"]
    first = (f"В резюме нет {len(missing)} из {len(skills)} навыков вакансии: {', '.join(missing)}."
             if missing else f"В резюме найдены все навыки вакансии: {len(skills)} из {len(skills)}.")
    second = f"Стаж: {_experience(resume.experience_months)}; в похожих должностях: {_experience(resume._similar_experience_months)}."
    advanced = list(dict.fromkeys(skill.name for skill in resume.skills if skill.level == "advanced"))
    third = "Продвинутый уровень: " + ", ".join(advanced) + "." if advanced else (
        f"Образование: {resume.education}." if resume.education else "Продвинутый уровень навыков и образование не указаны.")
    missing_data = []
    if not resume.candidate_name:
        missing_data.append(MissingField(field="candidate_name", text="Уточните, пожалуйста, ваши фамилию, имя и отчество."))
    for field, prompt in (("phone", "Уточните, пожалуйста, номер телефона для связи."),
                          ("email", "Уточните, пожалуйста, адрес электронной почты."),
                          ("telegram", "Уточните, пожалуйста, контакт в Telegram.")):
        if not getattr(contacts, field):
            missing_data.append(MissingField(field=field, text=prompt))
    if resume.experience_months is None:
        missing_data.append(MissingField(field="experience_months", text="Какой у вас общий стаж работы?"))
    skills.sort(key=lambda skill: {"red": 0, "yellow": 1, "green": 2}[skill.relevance])
    return MatchResult.model_validate({"match_status": status, "match_share": share, "summary": [first, second, third],
                                       "skills": skills, "missing_data": missing_data})
