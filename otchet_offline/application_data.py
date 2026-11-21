"""Поиск ФИО и способа связи в JSON-отклике без сохранения их значений."""

from __future__ import annotations

import re
import unicodedata


PERSON_FIELDS = ("candidate", "applicant", "personal_info", "personal", "person", "name", "full_name")
FULL_NAME_FIELDS = {"full_name", "fullname", "fio", "фио", "candidate_name", "applicant_name", "name"}
FIRST_NAME_FIELDS = {"first_name", "given_name", "first", "имя"}
LAST_NAME_FIELDS = {"last_name", "surname", "family_name", "last", "фамилия"}
NAME_WORD = re.compile(r"[^\W\d_]+(?:[-’'][^\W\d_]+)*")
NAME_PLACEHOLDERS = {"имя", "фамилия", "отчество", "не", "нет", "отсутствует", "скрыто", "анонимно", "я", "и", "кандидат", "соискатель",
                     "hidden", "unknown", "redacted", "withheld", "none", "null", "name", "candidate", "applicant"}
NAME_LABEL = re.compile(r"(?im)^[ \t]*(?:фио|полное имя|имя и фамилия|full name)[ \t]*[:—–-][ \t]*([^\r\n]+)")
NAME_INTRO = re.compile(r"\b(?:меня зовут|мо[её] имя|my name is)\s+([^,.!?:;\r\n]+)", re.IGNORECASE)

CONTACT_FIELDS = {
    **dict.fromkeys(("phone", "phones", "phone_number", "mobile", "mobile_phone", "cell", "work", "home", "telephone", "телефон"), "phone"),
    **dict.fromkeys(("email", "emails", "email_address", "e-mail", "почта"), "email"),
    **dict.fromkeys(("telegram", "tg", "whatsapp", "signal", "messenger", "телеграм"), "messenger"),
}
CONTACT_CONTAINERS = {"contact", "contacts", "contact_info", "contact_details", "messengers", "контакт", "контакты"}
CONTACT_VALUE_FIELDS = {"value", "address", "formatted", "number", "url", "link", "username", "handle"}
EMAIL = re.compile(r"(?<![\w.+-])[^\W_][\w.+-]*@[\w-]+(?:\.[\w-]+)+\b")
URL = re.compile(r"(?:https?://|www\.)[^\s<>]+", re.IGNORECASE)
PHONE = re.compile(r"(?<![\w+])(?:\+[1-9](?:[ ().-]*\d){9,14}|[78][ -]*\(?\d{3}\)?(?:[ -]*\d){7})(?!\d)")
PHONE_VALUE = re.compile(r"[+\d(). \-]+")
PHONE_LABEL = re.compile(r"(?im)^\s*(?:мобильный(?: телефон)?|телефон|номер телефона|phone|mobile)\s*[:—–-]\s*(\+?[\d ().-]+)")
HANDLE = re.compile(r"@?[a-zA-Z][a-zA-Z0-9_]{3,31}")
HANDLE_LABEL = re.compile(r"(?i)\b(?:telegram|телеграмм?|tg|тг|signal)\s*(?:[:—–-]\s*@?|@)([a-z][a-z0-9_]{3,31})\b")
MESSENGER_LINK = re.compile(
    r"(?i)(?<![\w.])(?:(?:t\.me|telegram\.me)/(?!share\b|joinchat\b|addstickers\b|proxy\b)[a-z][a-z0-9_]{3,31}|wa\.me/[1-9]\d{9,14})(?=$|[?\s.,);])"
)
CONTACT_LINE = re.compile(r"(?im)^\s*(?:контакты?|электронная почта|почта|e-?mail|телефон|phone|mobile|telegram|телеграмм?|whatsapp|signal)\s*[:—–-][^\r\n]*")


def _text(value: object) -> str:
    return unicodedata.normalize("NFKC", value).strip() if isinstance(value, str) else ""


def _name(value: object, minimum: int = 2) -> bool:
    words = _text(value).split()
    return minimum <= len(words) <= 6 and all(
        len(word) >= 2 and NAME_WORD.fullmatch(word) and word.casefold() not in NAME_PLACEHOLDERS
        for word in words
    )


def _phone(value: str) -> bool:
    if not PHONE_VALUE.fullmatch(value):
        return False
    digits = re.sub(r"\D", "", value)
    return 10 <= len(digits) <= 15 and len(set(digits)) > 1


def _in_text(value: str) -> bool:
    if MESSENGER_LINK.search(value):
        return True
    text = URL.sub("", value)
    return bool(EMAIL.search(text) or any(match[1].casefold() not in NAME_PLACEHOLDERS for match in HANDLE_LABEL.finditer(text))
                or any(_phone(match[0]) for match in PHONE.finditer(text))
                or any(_phone(match[1].strip()) for match in PHONE_LABEL.finditer(text)))


def _contact_path(value: object, path: str, kind: str = "") -> str | None:
    if isinstance(value, list):
        return next((found for i, item in enumerate(value) if (found := _contact_path(item, f"{path}/{i}", kind))), None)
    if isinstance(value, dict):
        type_field = value.get("type")
        type_name = type_field.get("id") if isinstance(type_field, dict) else type_field
        kind = CONTACT_FIELDS.get(_text(type_name).casefold(), kind)
        for key, item in value.items():
            tag = key.casefold()
            if tag in CONTACT_FIELDS or tag in CONTACT_VALUE_FIELDS or tag in CONTACT_CONTAINERS:
                found = _contact_path(item, f"{path}/{key}", CONTACT_FIELDS.get(tag, kind))
                if found:
                    return found
        if kind == "phone" and all(key in value for key in ("country", "city", "number")):
            parts = [str(value[key]) if type(value[key]) is int else _text(value[key]) for key in ("country", "city", "number")]
            if all(parts) and _phone("".join(parts)):
                return path
        return None
    if type(value) is int and kind in ("phone", "messenger"):
        value = str(value)
    text = _text(value)
    if _in_text(text) or (kind in ("phone", "messenger") and _phone(text)):
        return path
    if kind == "messenger" and HANDLE.fullmatch(text) and text.lstrip("@").casefold() not in NAME_PLACEHOLDERS:
        return path
    return None


def find_application_data(resume: dict) -> tuple[tuple[str, ...], str | None]:
    """Вернуть пути к полному имени и первому обнаруженному способу связи."""
    sections = [("", resume)]
    sections.extend((f"/{key}", resume[key]) for key in PERSON_FIELDS if isinstance(resume.get(key), dict))
    name_paths = ()
    contact_path = None
    for base, section in sections:
        fields = {key.casefold(): (key, item) for key, item in section.items()}
        if not name_paths:
            name_paths = next(((f"{base}/{key}",) for tag, (key, item) in fields.items()
                               if tag in FULL_NAME_FIELDS and _name(item)), ())
            if not name_paths:
                first = next((key for tag, (key, item) in fields.items() if tag in FIRST_NAME_FIELDS and _name(item, 1)), None)
                last = next((key for tag, (key, item) in fields.items() if tag in LAST_NAME_FIELDS and _name(item, 1)), None)
                if first and last:
                    name_paths = (f"{base}/{first}", f"{base}/{last}")
        if not contact_path:
            contact_path = next((found for tag, (key, item) in fields.items()
                                 if (tag in CONTACT_FIELDS or tag in CONTACT_CONTAINERS)
                                 and (found := _contact_path(item, f"{base}/{key}", CONTACT_FIELDS.get(tag, "")))), None)

    header = resume.get("original_sections")
    text_fields = [(f"/{key}", _text(resume.get(key))) for key in ("cover_letter", "about_me")]
    if isinstance(header, dict):
        text_fields.extend((f"/original_sections/{key}", _text(header.get(key))) for key in ("header", "cover_letter", "about_me"))
    source = _text(resume.get("source_text"))
    if not name_paths:
        candidates = [*text_fields, ("/source_text", source)]
        name_paths = next(((path,) for path, value in candidates
                           if any(_name(match[1]) for match in NAME_LABEL.finditer(value))
                           or (path != "/source_text" and any(_name(match[1]) for match in NAME_INTRO.finditer(value)))), ())
    if not contact_path:
        contact_path = next((path for path, value in text_fields if _in_text(value)), None)
        if not contact_path and any(_in_text(match[0]) for match in CONTACT_LINE.finditer(source)):
            contact_path = "/source_text"
    return name_paths, contact_path


def report_identity(resume: dict) -> tuple[str | None, tuple[str, ...]]:
    """Return a display name and direct contacts for the PDF header."""
    name_paths, _ = find_application_data(resume)
    names = []
    for path in name_paths:
        value = resume
        for key in path.removeprefix("/").split("/"):
            value = value[key]
        text = _text(value)
        if key.casefold() in FULL_NAME_FIELDS | FIRST_NAME_FIELDS | LAST_NAME_FIELDS:
            names.append(text)
        else:
            match = next((match for pattern in (NAME_LABEL, NAME_INTRO) for match in pattern.finditer(text)
                          if _name(match[1])), None)
            if match:
                names.append(match[1].strip())

    structured = resume.get("contacts")
    sections = (resume, structured) if isinstance(structured, dict) else (resume,)
    direct = []
    for section in sections:
        for key, value in section.items():
            kind = CONTACT_FIELDS.get(key.casefold())
            contact = _text(value)
            if ((kind == "phone" and _phone(contact))
                    or (kind == "email" and EMAIL.fullmatch(contact))
                    or (kind == "messenger" and HANDLE.fullmatch(contact))):
                direct.append("@" + contact.lstrip("@") if kind == "messenger" else contact)
    text = URL.sub("", "\n".join(_text(resume.get(key)) for key in ("source_text", "cover_letter", "about_me")))
    direct.extend(match[0] for match in EMAIL.finditer(text))
    direct.extend(match[0] for match in PHONE.finditer(text) if _phone(match[0]))
    direct.extend("@" + match[1] for match in HANDLE_LABEL.finditer(text))
    return " ".join(names) or None, tuple(dict.fromkeys(direct))
