"""Import all organizer datasets and attach their original or locally rendered PDFs."""
import argparse
import io
import json
import os
import re
import unicodedata
from collections import defaultdict
from html import escape
from pathlib import Path
from tempfile import NamedTemporaryFile
from uuid import NAMESPACE_URL, uuid5

import pymupdf

from candidate_analysis import canonical_skill
from check_candidate import PROFILES
from contract_models import PoolQuestion
from department_bot.materials import report_text
from hr_api.pdf_reports import INDEX_NAME, PdfEntry, PdfIndex, source_digest
from vacancy_parser import parse_vacancy_file

ARCHIVES = {"frontend": "frontend", "junior-python": "python", "designer": "designer"}


def atomic_write(path, content):
    if path.is_file() and path.read_bytes() == content:
        return
    with NamedTemporaryFile(dir=path.parent, delete=False) as tmp:
        temp = Path(tmp.name)
    try:
        temp.write_bytes(content)
        temp.chmod(0o644)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def offline_builder(banks):
    questions = defaultdict(dict)
    for path in sorted(banks.glob("*.json")):
        for item in json.loads(path.read_text(encoding="utf-8-sig"))["questions"]:
            skill = canonical_skill(item["skill"]).casefold()
            questions[skill][item["question"]] = item["reference_answer"]

    def build(vacancy_text, skills):
        result = []
        for skill in skills:
            pairs = dict(questions[skill.casefold()])
            if len(pairs) < 2:
                pairs.update({
                    f"В какой задаче вы применяли {skill} и за что отвечали лично?":
                        "Названы задача, личный вклад кандидата и проверяемый результат.",
                    f"Как вы проверяли результат работы с {skill} и какие ограничения обнаружили?":
                        "Приведён конкретный способ проверки и пример ограничения из практики.",
                })
            result.extend(PoolQuestion(skill=skill, question=q, expected_answer=a) for q, a in pairs.items())
        return result

    return build


def render_pdf(report, title):
    body = "".join(f"<p>{escape(line)}</p>" for line in report_text(report, title).splitlines() if line)
    story = pymupdf.Story(html=body, user_css="p { font-size: 11pt; margin: 0 0 5pt; }")
    output = io.BytesIO()
    writer = pymupdf.DocumentWriter(output)
    page = pymupdf.paper_rect("a4")
    story.write(writer, lambda n, filled: (page, page + (40, 42, -40, -42), None))
    writer.close()
    with pymupdf.open(stream=output.getvalue(), filetype="pdf") as document:
        return document.tobytes(garbage=4, deflate=True, no_new_id=True)


def source_files(folder):
    sources = {}
    for path in folder.glob("отклик *.json"):
        number = path.stem.split()[-1]
        source = json.loads(path.read_text(encoding="utf-8-sig"))
        if not re.fullmatch(r"[1-9][0-9]*", number) or str(source["candidate_id"]) != number or number in sources:
            raise ValueError(f"Номер отклика и исходный JSON расходятся: {path}")
        sources[number] = path
    if not sources:
        raise ValueError(f"Не найдены JSON-отклики: {folder}")
    return sources


def archive_files(offline, key, sources):
    label = ARCHIVES.get(key)
    if label is None:
        return {}
    folder = offline / f"reports_{label}"
    ranking = json.loads((folder / "ranking.json").read_text(encoding="utf-8-sig"))
    files = {}
    for item in ranking["applications"]:
        number = str(item["application_id"])
        if number not in sources or number in files:
            raise ValueError(f"Некорректная привязка PDF: {key}/{number}")
        # Distributed files were renamed after ranking.json was exported.
        path = folder / f"Report_{label}_{number}.pdf"
        if not path.is_file():
            name = item["report"]
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*\.pdf", name):
                raise ValueError(f"Недопустимое имя PDF в {folder}")
            path = folder / name
        with path.open("rb") as handle:
            if handle.read(5) != b"%PDF-":
                raise ValueError(f"Некорректный PDF: {path}")
        files[number] = path
    extra = set(folder.glob("*.pdf")) - set(files.values())
    if extra:
        raise ValueError(f"В папке {folder} есть PDF без привязки к отклику")
    return files


def main():
    from sqlalchemy import select
    from sqlalchemy.orm import joinedload
    from hr_api.import_data import import_folder
    from hr_api.preparation import ReportPreparer
    from hr_api.service import HRService
    from storage.database import make_engine, make_session_factory
    from storage.models import Response

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", type=Path, required=True)
    parser.add_argument("--banks", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1] / "parsed_responses"
    folders = {unicodedata.normalize("NFC", p.name): p for p in root.iterdir() if p.is_dir()}
    engine = make_engine()
    sessions = make_session_factory(engine)
    preparer = ReportPreparer(sessions, offline_builder(args.banks))
    service = HRService(sessions)
    total = 0
    try:
        for key, folder_name in PROFILES.items():
            folder = folders[unicodedata.normalize("NFC", folder_name)]
            sources = source_files(folder)
            originals = archive_files(args.offline, key, sources)
            title = parse_vacancy_file(folder / "job_vacancy.txt").title
            print(f"{title}: импорт {len(sources)} откликов…", flush=True)
            import_folder(sessions, folder, key)
            vacancy = uuid5(NAMESPACE_URL, "hr-platform/vacancies/" + key)
            preparer.prepare_vacancy(vacancy)
            directory = args.offline / "reports" / str(vacancy)
            directory.mkdir(exist_ok=True, parents=True)
            entries = []
            with sessions() as session:
                query = select(Response).where(Response.vacancy_id == vacancy).options(joinedload(Response.report))
                rows = {row.id: row for row in session.scalars(query)}
                for number, source in sources.items():
                    identifier = uuid5(NAMESPACE_URL, f"hr-platform/responses/{key}/{number}")
                    row = rows[identifier]
                    if number in originals:
                        content = originals[number].read_bytes()
                        digest = source_digest(json.loads(source.read_text(encoding="utf-8-sig")))
                        name = originals[number].name
                    else:
                        report = service._report(row, include_pdf=False)
                        content = render_pdf(report, title)
                        digest = source_digest(row.source_json)
                        name = f"Report_{key}_{number}.pdf"
                    atomic_write(directory / name, content)
                    entries.append(PdfEntry(candidate_id=number, filename=name, source_sha256=digest))
            index = PdfIndex(source_key=key, vacancy_title=title, entries=entries)
            atomic_write(directory / INDEX_NAME, index.model_dump_json(indent=2).encode("utf-8"))
            print(f"{title}: подключено {len(entries)} PDF, из них исходных {len(originals)}.", flush=True)
            total += len(entries)
    finally:
        engine.dispose()
    print(f"Всего подключено PDF: {total}.", flush=True)


if __name__ == "__main__":
    main()
