"""Corpus checks against all 1434 user-provided files and the FastAPI endpoint.

Run after parse_responses.py; external corpus is not required by unit tests.
"""
import argparse
import json
import re
from collections import Counter
from pathlib import Path

from fastapi.testclient import TestClient

from main import app
from response_parser import CandidateResponse, LEVELS
from vacancy_parser import decode_txt


CORPUS = [("Frontend developer", 99), ("Junior Python-разработчик", 430),
          ("Junior UX_UI Дизайнер", 212), ("Python-разработчик", 309),
          ("QA Engineer Manual", 384)]


def verify(root: Path, output: Path) -> dict:
    counts = Counter()
    folders = {}
    with TestClient(app) as client:
        for folder, expected_count in CORPUS:
            paths = [root / folder / f"отклик {i}.txt" for i in range(1, expected_count + 1)]
            for number, source in enumerate(paths, 1):
                saved = output / folder / f"{source.stem}.json"
                candidate = CandidateResponse.model_validate_json(saved.read_text(encoding="utf-8"))
                raw = source.read_bytes()
                assert candidate.source_text == decode_txt(raw), f"Changed source: {source}"
                assert candidate.candidate_id == number, f"Wrong candidate ID: {source}"
                sections = candidate.original_sections
                assert all(getattr(sections, name) is not None for name in type(sections).model_fields), source
                assert not any("Не найден раздел" in w for w in candidate.warnings), source
                assert sections.experience is not None, source
                assert sections.skills is not None, source
                assert sections.education is not None, source
                jobs_in_source = re.findall(r"(?m)^Организация\s+\d+\s*$", sections.experience)
                for marker in jobs_in_source:
                    assert any(marker.strip() in job.original for job in candidate.work_experience), f"Lost employer: {source}"
                # One export includes two extra employers in the free-text
                # description of one job; retain them there, without duplicating jobs.
                anonymous_jobs = sum(bool(re.fullmatch(r"Организация\s+\d+", job.organization or ""))
                                     for job in candidate.work_experience)
                counts["embedded_employer_mentions"] += len(jobs_in_source) - anonymous_jobs
                skill_lines = [s.strip() for s in sections.skills.splitlines() if s.strip()]
                expected_skills = [s for s in skill_lines if s not in LEVELS and s != "Не указано в доступной версии резюме."]
                assert [skill.original for skill in candidate.skills] == expected_skills, f"Lost skills: {source}"
                for job in candidate.work_experience:
                    assert job.original in sections.experience, source
                    assert job.organization is not None, source
                    assert job.organization in job.original, source
                    assert job.position is None or job.position in job.original, source
                for entry in candidate.education:
                    assert entry.original in sections.education, source
                    assert entry.institution is None or entry.institution in entry.original, source
                if "Не указано в доступной версии резюме." == sections.experience:
                    assert candidate.experience_total_months is None, source
                    assert not candidate.work_experience, source
                    counts["unknown_experience"] += 1
                response = client.post("/api/v1/parse/response", files={"file": (source.name, raw, "text/plain")})
                assert response.status_code == 200, f"API failed: {source}: {response.status_code}"
                assert response.json() == candidate.model_dump(), f"API differs from batch: {source}"
                counts["files"] += 1
                counts["api_success"] += 1
                counts["work_entries"] += len(candidate.work_experience)
                counts["skills"] += len(candidate.skills)
                counts["education_entries"] += len(candidate.education)
                counts["with_warnings"] += bool(candidate.warnings)
                counts["unknown_positions"] += sum(job.position is None for job in candidate.work_experience)
            folders[folder] = expected_count
    # Explicit regression examples with significantly different total/similar experience.
    frontend = json.loads((output / "Frontend developer" / "отклик 99.json").read_text(encoding="utf-8"))
    assert frontend["experience_total_months"] == 154
    assert frontend["similar_experience_months"] == 22
    python = json.loads((output / "Junior Python-разработчик" / "отклик 1.json").read_text(encoding="utf-8"))
    assert python["resume_title"] is None  # This source really has no 'Резюме:' line.
    assert python["education"][0]["year"] == 2031
    assert counts["files"] == sum(size for _, size in CORPUS)
    return {**dict(counts), "folders": folders}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("D:/Кадры"))
    parser.add_argument("--output", type=Path, default=Path("parsed_responses"))
    args = parser.parse_args()
    report = verify(args.root, args.output)
    (args.output / "_verification.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
