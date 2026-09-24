"""Трекинг экспериментов: MLflow, если он установлен, иначе журнал запусков в JSON."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ws_common.logging import get_logger


def build_tracker(tracking_uri: str, reports_dir: Path):  # noqa: ANN201
    """MLflow при наличии пакета и заданном URI, иначе файловый журнал."""
    if tracking_uri:
        try:
            import mlflow  # noqa: PLC0415 - необязательная зависимость
        except ImportError:
            get_logger("ml.tracker").warning("mlflow.missing", tracking_uri=tracking_uri)
        else:
            return MlflowTracker(mlflow, tracking_uri)
    return JsonFileTracker(reports_dir / "runs.jsonl")


class MlflowTracker:
    """Реализация порта `ExperimentTracker` поверх MLflow."""

    def __init__(self, mlflow_module: Any, tracking_uri: str) -> None:
        self._mlflow = mlflow_module
        self._mlflow.set_tracking_uri(tracking_uri)
        self._mlflow.set_experiment("weak-signals")

    def start_run(self, name: str) -> None:
        """Начинает запуск."""
        self._mlflow.start_run(run_name=name)

    def log_params(self, params: dict[str, Any]) -> None:
        """Логирует параметры."""
        self._mlflow.log_params({key: str(value) for key, value in params.items()})

    def log_metrics(self, metrics: dict[str, float]) -> None:
        """Логирует метрики."""
        self._mlflow.log_metrics({key: float(value) for key, value in metrics.items()})

    def log_artifact(self, path: Path) -> None:
        """Прикладывает файл."""
        if Path(path).exists():
            self._mlflow.log_artifact(str(path))

    def end_run(self) -> None:
        """Завершает запуск."""
        self._mlflow.end_run()


class JsonFileTracker:
    """Журнал запусков в JSONL: воспроизводимость важнее наличия MLflow в контуре."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._record: dict[str, Any] = {}

    def start_run(self, name: str) -> None:
        """Начинает запись."""
        self._record = {
            "run": name,
            "started_at": datetime.now(UTC).isoformat(),
            "params": {},
            "metrics": {},
            "artifacts": [],
        }

    def log_params(self, params: dict[str, Any]) -> None:
        """Добавляет параметры."""
        self._record.setdefault("params", {}).update({key: str(value) for key, value in params.items()})

    def log_metrics(self, metrics: dict[str, float]) -> None:
        """Добавляет метрики."""
        self._record.setdefault("metrics", {}).update(
            {key: float(value) for key, value in metrics.items()}
        )

    def log_artifact(self, path: Path) -> None:
        """Добавляет путь артефакта."""
        self._record.setdefault("artifacts", []).append(str(path))

    def end_run(self) -> None:
        """Дописывает запись в журнал."""
        self._record["finished_at"] = datetime.now(UTC).isoformat()
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(self._record, ensure_ascii=False) + "\n")
        self._record = {}
