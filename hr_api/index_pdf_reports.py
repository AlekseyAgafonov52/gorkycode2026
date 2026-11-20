"""Build a PDF attachment index from existing offline ranking and source JSONs."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .pdf_reports import DEFAULT_REPORTS, INDEX_NAME, PdfEntry, PdfIndex, source_digest


def build_index(key: str, reports: Path, applications: Path) -> PdfIndex:
    ranking = json.loads((reports / "ranking.json").read_text(encoding="utf-8-sig"))
    sources = {}
    for path in applications.glob("*.json"):
        source = json.loads(path.read_text(encoding="utf-8-sig"))
        candidate_id = str(source.get("candidate_id", ""))
        if candidate_id in sources:
            raise ValueError("Номер кандидата повторяется в исходных откликах")
        sources[candidate_id] = source
    entries = []
    for item in ranking["applications"]:
        candidate_id = str(item["application_id"])
        if candidate_id not in sources:
            raise ValueError("В рейтинге найден кандидат без исходного JSON")
        entry = PdfEntry(candidate_id=candidate_id, filename=item["report"],
                         source_sha256=source_digest(sources[candidate_id]))
        path = (reports / entry.filename).resolve()
        if not path.is_relative_to(reports.resolve()):
            raise ValueError("PDF должен находиться в папке отчётов")
        with path.open("rb") as pdf:
            if pdf.read(5) != b"%PDF-":
                raise ValueError("Файл не является PDF")
        entries.append(entry)
    return PdfIndex(source_key=key, vacancy_title=ranking["vacancy_title"], entries=entries)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--key", required=True, help="Тот же постоянный ключ, что в hr_api.import_data")
    parser.add_argument("--reports", type=Path, default=DEFAULT_REPORTS)
    parser.add_argument("--applications", type=Path, default=DEFAULT_REPORTS.parent / "applications")
    args = parser.parse_args()
    index = build_index(args.key, args.reports, args.applications)
    path = args.reports / INDEX_NAME
    temporary = path.with_suffix(".tmp")
    temporary.write_text(index.model_dump_json(indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
    print(f"PDF indexed: {len(index.entries)}; source key: {index.source_key}")


if __name__ == "__main__":
    main()
