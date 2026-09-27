"""Файл прогона по темам, а не по порядку файлов: самый новый own-topics-*.json, темы которого совпадают с файлом тем.

Запуск: python3 tools/quality/pick_run.py ml/reports/rubric/case_topics.txt  → печатает путь или завершается с кодом 1.
"""

from __future__ import annotations

import glob
import json
import os
import sys
from pathlib import Path


def pick(topics_file: str, folder: str = ".") -> str | None:
    wanted = [line.strip() for line in Path(topics_file).read_text(encoding="utf-8").splitlines() if line.strip()]
    for path in sorted(glob.glob(os.path.join(folder, "own-topics-*.json")), key=os.path.getmtime, reverse=True):
        try:
            topics = [block.get("topic", "") for block in json.loads(Path(path).read_text(encoding="utf-8"))]
        except (OSError, ValueError):
            continue
        if topics == wanted:
            return path
    return None


if __name__ == "__main__":
    found = pick(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else ".")
    if found is None:
        print(f"нет прогона с темами из {sys.argv[1]}", file=sys.stderr)
        sys.exit(1)
    print(found)
