"""Batch conversion of numbered candidate response files into separate JSON files."""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path

from response_parser import CandidateResponse, parse_response_file


def parse_directories(inputs: list[Path], output: Path) -> dict:
    files = set()
    for folder in inputs:
        if not folder.is_dir():
            raise ValueError(f"Не найдена папка: {folder}")
        files.update(p.resolve() for p in folder.iterdir()
                     if p.is_file() and re.fullmatch(r"отклик\s+\d+\.txt", p.name, re.I))
    if not files:
        raise ValueError("В указанных папках не найдены файлы 'отклик N.txt'")
    files = sorted(files, key=lambda p: (str(p.parent), int(re.search(r"\d+", p.stem)[0])))
    # Candidate numbers restart in each vacancy folder; never flatten them.
    destinations = [output / p.parent.name / f"{p.stem}.json" for p in files]
    if len({str(p.resolve()).casefold() for p in destinations}) != len(files):
        raise ValueError("Имена входных папок совпадают; выберите разные каталоги результата")
    report = {"files_total": len(files), "parsed": 0, "failed": 0,
              "with_warnings": 0, "warning_counts": {}, "folders": {}, "files": []}
    warnings = Counter()
    for path, destination in zip(files, destinations):
        group = report["folders"].setdefault(path.parent.name, {"total": 0, "parsed": 0, "failed": 0})
        group["total"] += 1
        item = {"source": str(path), "output": str(destination), "status": "error"}
        try:
            candidate = parse_response_file(path)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(candidate.model_dump_json(indent=2), encoding="utf-8")
            item.update(status="ok", candidate_id=candidate.candidate_id,
                        work_entries=len(candidate.work_experience), skills=len(candidate.skills),
                        languages=len(candidate.languages), education_entries=len(candidate.education),
                        warnings=candidate.warnings)
            report["parsed"] += 1
            group["parsed"] += 1
            if candidate.warnings:
                report["with_warnings"] += 1
                warnings.update(candidate.warnings)
        except (OSError, ValueError) as exc:
            report["failed"] += 1
            group["failed"] += 1
            item["error"] = str(exc)
        report["files"].append(item)
    report["warning_counts"] = dict(warnings)
    output.mkdir(parents=True, exist_ok=True)
    (output / "_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (output / "_schema.json").write_text(json.dumps(CandidateResponse.model_json_schema(), ensure_ascii=False, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folders", type=Path, nargs="+")
    parser.add_argument("--output", type=Path, default=Path("parsed_responses"))
    args = parser.parse_args()
    try:
        report = parse_directories(args.folders, args.output)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"{exc}\n")
    print(json.dumps({key: value for key, value in report.items() if key != "files"}, ensure_ascii=False, indent=2))
    raise SystemExit(1 if report["failed"] else 0)
