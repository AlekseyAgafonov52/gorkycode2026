"""Create a clearly labelled copy of organizer responses 1–3 for contract demos."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from check_candidate import PROFILES


def create_demo(root: Path, output: Path) -> Path:
    source = root / "parsed_responses" / PROFILES["frontend"]
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise ValueError("Для демо нужна пустая папка, чтобы не перезаписать существующие файлы")
    vacancy = (source / "job_vacancy.txt").read_bytes()
    (output / "job_vacancy.txt").write_bytes(vacancy)
    (output / "DEMO.txt").write_text(
        "Демонстрационные данные: копии откликов 1–3. В 1 и 3 добавлены вымышленные ФИО и email example.com.\n"
        "Исходные файлы организаторов не менялись. Отклик 2 оставлен без изменений.\n", encoding="utf-8")
    for number in (1, 2, 3):
        data = json.loads((source / f"отклик {number}.json").read_text(encoding="utf-8-sig"))
        if number != 2:
            data["candidate_name"] = f"Демонстрационный Кандидат {number}"
            data["contacts"] = {"phone": None, "email": f"demo{number}@example.com", "telegram": None, "other": []}
            data["source_text"] = ("ДЕМОНСТРАЦИОННЫЕ ДАННЫЕ\n"
                f"ФИО: {data['candidate_name']}\nEmail: demo{number}@example.com\n" + data["source_text"])
        (output / f"отклик {number}.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        (output / f"отклик {number}.txt").write_bytes(data["source_text"].encode("utf-8"))
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(create_demo(args.root, args.output))


if __name__ == "__main__":
    main()
