"""Check the user-provided files through the API and save JSON artifacts."""
import json
from pathlib import Path

from fastapi.testclient import TestClient

from main import app


def main() -> None:
    root = Path("D:/Кадры")
    cases = [
        ("Frontend developer", "frontend", "onsite", 1, "Vue.js", "Nuxt", 5),
        ("Junior Python-разработчик", "python", "hybrid", 1, "Python", None, 6),
        ("Junior UX_UI Дизайнер", "designer", "onsite", 0, "Figma", "Photoshop", 5),
        ("QA Engineer Manual", "qa_manual", "onsite", 1, "Postman", "Java", 5),
    ]
    output = Path(__file__).parent / "examples"
    output.mkdir(exist_ok=True)
    with TestClient(app) as client:
        for folder, name, work_format, experience, required, preferred, duties in cases:
            path = root / folder / "Вакансия.txt"
            response = client.post("/api/v1/parse/vacancy", files={"file": (path.name, path.read_bytes(), "text/plain")})
            assert response.status_code == 200, response.text
            result = response.json()
            assert result["work_format"] == work_format, result
            assert result["experience_min_years"] == experience, result
            assert required in result["required_skills"], result
            assert preferred is None or preferred in result["preferred_skills"], result
            assert len(result["responsibilities"]) == duties, result
            assert result["organization"] == 'АНО "Аналитический центр города Нижнего Новгорода"', result
            assert result["location"] == "Нижний Новгород", result
            assert result["education"] is None, result
            if name == "qa_manual":
                assert {"REST API", "Postman", "Swagger", "DevTools", "SQL"} <= set(result["required_skills"]), result
                assert {"Git", "Pixel Perfect", "Python", "Java"} <= set(result["preferred_skills"]), result
            assert result["source_text"] == path.read_text(encoding="utf-8-sig"), result
            (output / f"{name}.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"{name}: API 200, format={work_format}, experience={experience}, responsibilities={duties}")


if __name__ == "__main__":
    main()
