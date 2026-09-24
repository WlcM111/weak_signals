"""Держатель активной модели: единая точка доступа use cases к загруженным артефактам.

Модель загружается при старте и может быть перезагружена командой `register-model`, поэтому
доступ защищён блокировкой: gRPC-сервер синхронный и обслуживает запросы в пуле потоков.
"""

from __future__ import annotations

import threading

from analyzer.application.dto import ModelBundle
from analyzer.domain.errors import ModelNotLoaded


class ActiveModelHolder:
    """Потокобезопасное хранилище загруженной модели."""

    def __init__(self, bundle: ModelBundle | None = None) -> None:
        self._lock = threading.RLock()
        self._bundle = bundle

    def set(self, bundle: ModelBundle) -> None:
        """Заменяет активную модель."""
        with self._lock:
            self._bundle = bundle

    def get(self) -> ModelBundle:
        """Активная модель; FAILED_PRECONDITION `MODEL_NOT_LOADED`, если её нет."""
        with self._lock:
            if self._bundle is None:
                raise ModelNotLoaded("активная модель не загружена: сервис не готов к скорингу")
            return self._bundle

    @property
    def is_loaded(self) -> bool:
        """Готов ли сервис (используется `/readyz`)."""
        with self._lock:
            return self._bundle is not None
