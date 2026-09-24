"""Проверка адреса провайдера: разрешённые схемы, хосты и защита от SSRF (§«по IP» задания).

Адрес endpoint задаёт администратор через окружение, но проверяется он как недоверенный вход:
запрещены схемы кроме https (http — только для явно доверенного локального сервиса compose),
петлевые, служебные и частные адреса, метаданные облака и порты вне разрешённого набора.
Отключать проверку TLS нельзя ни при каких настройках: для адреса-IP имя из сертификата
задаётся отдельным параметром SNI, а не игнорированием ошибки.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from urllib.parse import urlparse

ALLOWED_SCHEMES = ("https",)
LOCAL_SCHEMES = ("http", "https")
ALLOWED_PORTS = frozenset({443, 8000, 8080, 8443, 9443})
# Хосты внутренней сети compose, которым разрешён http: они не выходят за пределы стенда.
TRUSTED_LOCAL_HOSTS = frozenset({"local-llm", "localhost", "127.0.0.1"})
METADATA_HOSTS = frozenset({"169.254.169.254", "metadata.google.internal", "metadata"})


@dataclass(frozen=True, slots=True)
class EndpointVerdict:
    """Итог проверки адреса."""

    allowed: bool
    reason_ru: str = ""
    host: str = ""
    port: int = 0
    scheme: str = ""
    is_ip_literal: bool = False

    @property
    def needs_sni_hint(self) -> bool:
        """Для адреса-литерала IP нужен явный SNI, иначе проверка сертификата не пройдёт."""
        return self.allowed and self.is_ip_literal


def _is_forbidden_ip(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str:
    """Причина запрета для IP-адреса; пустая строка — адрес допустим."""
    if address.is_loopback:
        return "петлевой адрес"
    if address.is_link_local:
        return "link-local адрес"
    if address.is_private:
        return "адрес частной сети"
    if address.is_reserved or address.is_multicast or address.is_unspecified:
        return "служебный адрес"
    return ""


def check(url: str, *, trusted_local: bool = False) -> EndpointVerdict:
    """Проверяет адрес провайдера.

    `trusted_local=True` включается только для сервиса, который администратор сам поднял во
    внутренней сети (профиль compose `local-llm`): ему разрешены http и частный адрес. Это не
    общий обход фильтров — для всех остальных провайдеров ограничения остаются в силе.
    """
    if not url or not url.strip():
        return EndpointVerdict(False, "адрес не задан")
    parsed = urlparse(url.strip())
    scheme = (parsed.scheme or "").lower()
    host = (parsed.hostname or "").lower()
    allowed_schemes = LOCAL_SCHEMES if trusted_local else ALLOWED_SCHEMES
    if scheme not in allowed_schemes:
        return EndpointVerdict(False, f"схема {scheme or '—'} недопустима, требуется https")
    if not host:
        return EndpointVerdict(False, "в адресе нет хоста")
    if host in METADATA_HOSTS:
        return EndpointVerdict(False, "адрес сервиса метаданных облака запрещён")

    port = parsed.port or (443 if scheme == "https" else 80)
    if not trusted_local and port not in ALLOWED_PORTS:
        return EndpointVerdict(False, f"порт {port} вне разрешённого набора")

    is_ip = False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    else:
        is_ip = True
        reason = _is_forbidden_ip(address)
        if reason and not (trusted_local and (address.is_private or address.is_loopback)):
            return EndpointVerdict(False, f"{reason} запрещён")

    if trusted_local and not is_ip and host not in TRUSTED_LOCAL_HOSTS:
        return EndpointVerdict(False, f"хост {host} не входит в список доверенных локальных")

    return EndpointVerdict(True, "", host=host, port=port, scheme=scheme, is_ip_literal=is_ip)