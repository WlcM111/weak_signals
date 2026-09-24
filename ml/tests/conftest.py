"""Пути пакетов для запуска тестов ML-конвейера."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for path in (
    ROOT / "ml" / "src",
    ROOT / "services" / "analyzer" / "src",
    ROOT / "libs" / "ws_common" / "src",
):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
