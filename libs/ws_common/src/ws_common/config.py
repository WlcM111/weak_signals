"""Базовая конфигурация сервисов: переменные окружения с префиксом `WS_` (pydantic-settings).

Неверное значение → отказ запуска с сообщением `config.invalid: <VAR>: <причина>` (§15.4 ТЗ).
"""

from __future__ import annotations

from typing import Any, Literal

from psycopg.conninfo import make_conninfo
from pydantic import Field, ValidationError, ValidationInfo, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ConfigError(RuntimeError):
    """Конфигурация невалидна; запуск сервиса невозможен."""


class BaseServiceSettings(BaseSettings):
    """Общие для всех сервисов параметры (§9, словарь конфигурации)."""

    # env_ignore_empty: пустая переменная окружения (`WS_X=` из compose) означает «не задано» и не должна
    # перекрывать значение по умолчанию пустой строкой.
    model_config = SettingsConfigDict(
        env_prefix="WS_", extra="ignore", case_sensitive=False, env_ignore_empty=True
    )

    @field_validator("*", mode="before")
    @classmethod
    def _drop_inline_comment(cls, value: Any, info: ValidationInfo) -> Any:
        """Защита от `VAR=   # комментарий` в .env: Docker Compose передаёт «# комментарий» как значение.

        Такое значение никогда не бывает настоящим ключом, адресом или числом, поэтому оно
        заменяется значением поля по умолчанию — так же, как если бы переменная не была задана.
        """
        if isinstance(value, str) and value.lstrip().startswith("#"):
            try:
                field = cls.model_fields[info.field_name or ""]
                default = field.get_default(call_default_factory=True)
            except Exception:  # noqa: BLE001 - защитный код не должен мешать запуску сервиса
                return ""
            return "" if default is None else default
        return value

    service_name: str = Field(default="collector")
    env: Literal["prod", "test"] = "prod"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: Literal["json", "console"] = "json"
    http_port: int = Field(default=8081, ge=1024, le=65535)
    grpc_port: int = Field(default=50051, ge=1024, le=65535)
    grpc_max_message_mb: int = Field(default=16, ge=1, le=64)
    shutdown_grace_seconds: int = Field(default=20, ge=1, le=120)

    pg_host: str = "postgres"
    pg_port: int = Field(default=5432, ge=1, le=65535)
    pg_database: str = "weaksignals"
    pg_user: str = "ws_collector"
    pg_password: str = ""
    pg_pool_min: int = Field(default=2, ge=1, le=50)
    pg_pool_max: int = Field(default=10, ge=1, le=50)
    pg_statement_timeout_ms: int = Field(default=30_000, ge=1000, le=600_000)

    @property
    def pg_dsn(self) -> str:
        """DSN psycopg без секрета в тексте ошибок (пароль передаётся отдельным параметром)."""
        return make_conninfo(
            host=self.pg_host,
            port=self.pg_port,
            dbname=self.pg_database,
            user=self.pg_user,
            password=self.pg_password,
        )

def load_settings[S: BaseSettings](settings_cls: type[S]) -> S:
    """Читает настройки и превращает ошибку pydantic в понятный отказ запуска."""
    try:
        return settings_cls()
    except ValidationError as exc:  # отказ запуска вместо скрытых значений по умолчанию
        problems = "; ".join(
            f"WS_{'_'.join(str(part) for part in error['loc']).upper()}: {error['msg']}" for error in exc.errors()
        )
        raise ConfigError(f"config.invalid: {problems}") from exc
