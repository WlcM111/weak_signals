"""Пути пакетов для тестов orchestrator."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
for path in (
    ROOT / "services" / "orchestrator" / "src",
    ROOT / "libs" / "ws_common" / "src",
    ROOT / "libs" / "ws_contracts" / "src",
):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
