#!/usr/bin/env python3
"""Bounded API diagnostics for weak-signals. Python 3.10+, no host dependencies.

Run from the project root: python3 weak_signals_api_doctor.py --isolate
No project code, configuration, database rows or credentials are modified.
"""
from __future__ import annotations

import argparse
import ast
import atexit
import copy
import datetime as dt
import email.utils
import hashlib
import http.client
import http.server
import importlib.metadata
import ipaddress
import json
import os
import pathlib
import platform
import queue
import re
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
import uuid
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from typing import Any

VERSION = "1.0.0"
MAX_BODY = 6 * 1024 * 1024
REPORT_PREFIX = "api_diagnostics"
PROXY_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy", "no_proxy")
TLS_ENV_KEYS = ("SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE")
SECRET_RE = re.compile(r"(?i)(key|token|secret|password|credential|authorization|cookie)")
ALLOWED_HEADERS = {"date", "server", "via", "x-cache", "x-cache-hits", "x-served-by", "age", "retry-after", "content-type", "content-length", "content-encoding", "location", "etag", "last-modified", "cache-control", "cf-ray", "cf-mitigated", "x-request-id", "request-id", "x-envoy-upstream-service-time", "x-openai-public-ip"}
FIELDS = "title,abstract,url,year,publicationDate,externalIds,citationCount,venue,publicationTypes"
GAPS = {"arxiv": 3.5, "gdelt": 6.0, "semantic_scholar": 2.5, "github": 7.0, "zenodo": 3.5, "openalex": 1.2, "wikipedia": 1.2, "hackernews": 1.2, "rospatent": 3.0, "rss": 5.0, "huggingface": 1.2, "gigachat": 2.0, "yandexgpt": 2.0, "hh": 3.0, "patentsview": 3.0}
RECOVERY_NAME = ".ws_api_doctor_recovery.json"
LOCK_NAME = ".ws_api_doctor.lock"
ROOT = pathlib.Path.cwd()


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def safe_url(url: str) -> str:
    try:
        p = urllib.parse.urlsplit(url)
        host = p.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        netloc = host + (f":{p.port}" if p.port else "")
        pairs = urllib.parse.parse_qsl(p.query, keep_blank_values=True)
        query = urllib.parse.urlencode([(k, "[REDACTED]" if SECRET_RE.search(k) or k.lower().startswith("x-amz-") else v) for k, v in pairs])
        return urllib.parse.urlunsplit((p.scheme, netloc, p.path, query, ""))
    except (ValueError, TypeError):
        return "[INVALID_URL]"


class Redactor:
    def __init__(self, values: dict[str, str] | None = None) -> None:
        self.secrets: set[str] = set()
        self.extend(values or {})

    def extend(self, values: dict[str, str]) -> None:
        for key, value in values.items():
            if value and SECRET_RE.search(key):
                self.secrets.add(value)
                self.secrets.add(urllib.parse.quote(value, safe=""))
                self.secrets.add(urllib.parse.quote_plus(value))
            if value and key in PROXY_KEYS:
                parsed = urllib.parse.urlsplit(value)
                for part in (parsed.username, parsed.password):
                    if part:
                        self.secrets.add(part)

    def text(self, value: str) -> str:
        for secret in sorted(self.secrets, key=len, reverse=True):
            if len(secret) >= 4:
                value = value.replace(secret, "[REDACTED]")
        value = re.sub(r"(?i)\b(?:Bearer|Basic|Api-Key)\s+[A-Za-z0-9_+./=:-]+", "[AUTH_REDACTED]", value)
        value = re.sub(r"(?i)([?&](?:api_key|access_token|token|key|signature|x-amz-[\w-]+)=)[^\s&#\"']+", r"\1[REDACTED]", value)
        value = re.sub(r"https?://[^\s/@]+:[^\s/@]+@", "https://[REDACTED]@", value)
        value = re.sub(r'(?i)("(?:access_token|refresh_token|api_key|password|secret)"\s*:\s*")[^"]*(")', r'\1[REDACTED]\2', value)
        value = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "?", value)
        return value

    def clean(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {k: ("[REDACTED]" if SECRET_RE.search(k) and isinstance(v, str) and k not in {"auth_mode"} else self.clean(v)) for k, v in value.items()}
        if isinstance(value, list):
            return [self.clean(v) for v in value]
        if isinstance(value, str):
            return self.text(value)
        return value


def read_dotenv(path: pathlib.Path) -> tuple[dict[str, str], list[str]]:
    """Non-executing fallback; Docker Compose is authoritative when available."""
    values: dict[str, str] = {}
    notes: list[str] = []
    if not path.is_file():
        return values, [f"{path.name}: отсутствует"]
    for line_no, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.match(r"(?:export\s+)?([A-Za-z_][A-Za-z_0-9]*)\s*=\s*(.*)$", line)
        if not match:
            notes.append(f"{path.name}:{line_no}: сложная/многострочная запись; нужен Compose")
            continue
        key, raw = match.groups()
        if key in values:
            notes.append(f"{path.name}:{line_no}: повтор переменной {key}")
        if raw[:1] in {"'", '"'}:
            quote = raw[0]
            end = raw.rfind(quote)
            if end == 0:
                notes.append(f"{path.name}:{line_no}: многострочное значение не разобрано")
                continue
            value = raw[1:end]
        else:
            value = re.split(r"\s+#", raw, maxsplit=1)[0].strip()
        if "$" in value:
            notes.append(f"{path.name}:{line_no}: интерполяция {key} проверяется через Compose")
        values[key] = value
    return values, notes


def split_feeds(value: str) -> list[str]:
    if not value.strip():
        return []
    try:
        parsed = json.loads(value)
        if isinstance(parsed, list):
            return [str(x).strip() for x in parsed if str(x).startswith(("https://", "http://"))]
    except ValueError:
        pass
    return [x.strip() for x in re.split(r"[,;\n]", value) if x.strip().startswith(("https://", "http://"))]


def retry_delay(headers: dict[str, str], now: float | None = None) -> float | None:
    now = time.time() if now is None else now
    value = headers.get("retry-after", "").strip()
    try:
        return max(0.0, float(value)) if value else None
    except ValueError:
        try:
            parsed = email.utils.parsedate_to_datetime(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=dt.timezone.utc)
            return max(0.0, parsed.timestamp() - now)
        except (ValueError, TypeError, OverflowError):
            return None


def rate_limited(result: dict[str, Any]) -> bool:
    headers = result.get("headers", {})
    body = result.get("sample", "").lower()
    return result.get("status") == 429 or (
        result.get("status") == 403 and (headers.get("x-ratelimit-remaining") == "0" or "rate limit" in body)
    ) or ("please limit requests" in body)


def inspect_body(body: bytes, expected: str, status: int, truncated: bool) -> dict[str, Any]:
    info: dict[str, Any] = {"bytes": len(body), "sha256": digest(body), "truncated": truncated}
    if status == 304:
        return {**info, "validation": "NOT_MODIFIED", "valid": True}
    if not body:
        return {**info, "validation": "EMPTY_BODY", "valid": False}
    value: Any = None
    try:
        value = json.loads(body)
        info["json_type"] = type(value).__name__
        if isinstance(value, dict):
            info["json_keys"] = sorted(value)[:35]
    except (ValueError, UnicodeError):
        pass
    if expected in {"rss", "atom"}:
        if b"<!DOCTYPE" in body.upper() or b"<!ENTITY" in body.upper():
            info.update(valid=False, validation="XML_DTD_REJECTED")
        else:
            try:
                root = ET.fromstring(body)
                root_name = root.tag.rsplit("}", 1)[-1].lower()
                count = sum(1 for node in root.iter() if node.tag.rsplit("}", 1)[-1] in {"item", "entry"})
                valid = root_name in {"rss", "feed", "rdf"}
                info.update(valid=valid, validation="VALID_FEED" if valid else "NOT_A_FEED", root=root_name, entries=count)
                if expected == "atom" and any("api/errors" in (node.text or "") for node in root.iter()):
                    info.update(valid=False, validation="ARXIV_ERROR_FEED")
                if root_name == "rdf":
                    info["project_parser_warning"] = "RSS 1.0/RDF не поддержан текущим parse_feed проекта"
                info["feed_refresh_hints"] = {node.tag.rsplit("}", 1)[-1]: (node.text or "")[:100] for node in root.iter() if node.tag.rsplit("}", 1)[-1] in {"ttl", "updatePeriod", "updateFrequency"}}
            except ET.ParseError:
                info.update(valid=False, validation="INVALID_XML")
    elif expected == "text":
        info.update(valid=status == 200, validation="TEXT")
    elif expected == "robots":
        parser = urllib.robotparser.RobotFileParser()
        parser.parse(body.decode("utf-8", "replace").splitlines())
        rate = parser.request_rate("weak-signals")
        info.update(valid=status == 200, validation="ROBOTS_RESPONSE", crawl_delay_s=parser.crawl_delay("weak-signals"), request_rate={"requests": rate.requests, "seconds": rate.seconds} if rate else None)
    else:
        valid = isinstance(value, dict)
        key = {"openalex": "results", "semantic": "data", "gdelt": "articles", "github": "items", "hackernews": "hits", "rospatent": "hits", "wikipedia_views": "items", "wikipedia_action": "query", "models": "data", "chat": "choices"}.get(expected)
        if key:
            valid = valid and key in value
            if valid and isinstance(value[key], list):
                info["entries"] = len(value[key])
        if expected == "zenodo":
            valid = valid and isinstance(value.get("hits"), dict) and isinstance(value["hits"].get("hits"), list)
            if valid:
                info["entries"] = len(value["hits"]["hits"])
        if expected == "wikipedia_summary":
            valid = valid and "title" in value and "extract" in value
        if isinstance(value, dict) and any(x in value for x in ("error", "errors")):
            valid = False
        if expected == "egress" and isinstance(value, dict):
            info["observed_public_ip"] = value.get("ip")
        if expected == "statuspage" and isinstance(value, dict):
            info["provider_status"] = value.get("status")
            info["provider_incidents"] = [{k: incident.get(k) for k in ("name", "status", "created_at", "updated_at")} for incident in value.get("incidents", [])]
        info.update(valid=valid, validation="VALID_JSON_SCHEMA" if valid else "UNEXPECTED_PAYLOAD")
    if not info.get("valid") or status >= 400 or expected in {"text", "robots"}:
        info["sample"] = body[:1600].decode("utf-8", "replace")
    return info


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


def ssl_context(job: dict[str, Any]) -> ssl.SSLContext:
    context = ssl.create_default_context(cafile=job.get("ca_file") or None)
    if job.get("tls") == "1.2":
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.maximum_version = ssl.TLSVersion.TLSv1_2
    elif job.get("tls") == "1.3":
        context.minimum_version = ssl.TLSVersion.TLSv1_3
        context.maximum_version = ssl.TLSVersion.TLSv1_3
    return context


def public_target(url: str, local: bool = False) -> list[str]:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Invalid URL scheme or embedded credentials")
    if local:
        return []
    addresses = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
    for item in addresses:
        if not ipaddress.ip_address(item[4][0]).is_global:
            raise ValueError("Public endpoint resolved to a non-public IP; probe skipped")
    return sorted({item[4][0] for item in addresses})


def perform_one(job: dict[str, Any]) -> tuple[int, dict[str, str], bytes, dict[str, Any]]:
    url, headers = job["url"], dict(job.get("headers", {}))
    engine = job.get("engine", "urllib")
    method = job.get("method", "GET")
    data = job.get("body")
    body = data.encode() if isinstance(data, str) else None
    timeout = float(job.get("timeout", 50))
    details: dict[str, Any] = {}
    if engine == "httpx":
        try:
            import httpx
        except ImportError as exc:
            raise RuntimeError("ENGINE_UNAVAILABLE:httpx") from exc
        options: dict[str, Any] = {"verify": ssl_context(job) if job.get("tls") or job.get("ca_file") else True, "trust_env": job.get("trust_env", True), "http2": job.get("http2", False), "follow_redirects": False, "timeout": httpx.Timeout(timeout, connect=min(timeout, 20))}
        if job.get("family"):
            raise RuntimeError("UNSUPPORTED:family restriction uses curl")
        with httpx.Client(**options) as client, client.stream(method, url, headers=headers, content=body) as response:
            stream = response.extensions.get("network_stream")
            if stream is not None:
                try:
                    ssl_object = stream.get_extra_info("ssl_object")
                    if ssl_object:
                        details["tls_version"] = ssl_object.version()
                        details["cipher"] = ssl_object.cipher()
                    details["peer"] = stream.get_extra_info("server_addr")
                except (OSError, AttributeError):
                    # Metadata is optional; a closed HTTP/1.0 socket must not
                    # turn a successfully received response into a network failure.
                    details["peer_metadata_unavailable"] = True
            chunks, size = [], 0
            for chunk in response.iter_bytes():
                chunks.append(chunk[: MAX_BODY + 1 - size])
                size += len(chunks[-1])
                if size > MAX_BODY:
                    break
            details["http_version"] = response.http_version
            return response.status_code, dict(response.headers), b"".join(chunks), details
    if engine == "curl":
        if not shutil.which("curl"):
            raise RuntimeError("ENGINE_UNAVAILABLE:curl")
        def quote(value: str) -> str:
            return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\r", "").replace("\n", "") + '"'
        with tempfile.TemporaryDirectory(prefix="ws-api-curl-") as directory:
            hp, bp = pathlib.Path(directory) / "headers", pathlib.Path(directory) / "body"
            config = ["silent", "show-error", "compressed", 'proto = "=http,https"', f"url = {quote(url)}", f"request = {quote(method)}", f"max-time = {timeout}", f"connect-timeout = {min(timeout,20)}", f"max-filesize = {MAX_BODY}", f"dump-header = {quote(str(hp))}", f"output = {quote(str(bp))}", 'write-out = "%{http_code}|%{remote_ip}|%{http_version}|%{time_namelookup}|%{time_connect}|%{time_appconnect}|%{time_starttransfer}|%{time_total}|%{ssl_verify_result}"']
            config.extend(f"header = {quote(k + ': ' + v)}" for k, v in headers.items())
            if data is not None:
                request_path = pathlib.Path(directory) / "request"
                request_path.write_bytes(body or b"")
                config.append(f"data-binary = {quote('@' + str(request_path))}")
            if not job.get("trust_env", True):
                config += ['noproxy = "*"']
            if job.get("family") in {4, 6}:
                config.append("ipv4" if job["family"] == 4 else "ipv6")
            if job.get("tls"):
                config += ["tlsv1.2" if job["tls"] == "1.2" else "tlsv1.3", f"tls-max = {job['tls']}"]
            config.append("http2" if job.get("http2") else "http1.1")
            if job.get("ca_file"):
                config.append(f"cacert = {quote(job['ca_file'])}")
            if job.get("resolve"):
                p = urllib.parse.urlsplit(url)
                ip = job["resolve"]
                if ":" in ip:
                    ip = f"[{ip}]"
                config.append(f"resolve = {quote(f'{p.hostname}:{p.port or 443}:{ip}')}")
            proc = subprocess.run(["curl", "-q", "--config", "-"], input="\n".join(config), text=True, capture_output=True, timeout=timeout + 5)
            parts = proc.stdout.strip().split("|")
            details["curl_exit"] = proc.returncode
            details["curl_metrics"] = dict(zip(("status", "remote_ip", "http_version", "dns_s", "connect_s", "tls_s", "ttfb_s", "total_s", "verify_result"), parts))
            if proc.stderr:
                details["transport_error"] = proc.stderr[:1000]
            raw_headers = hp.read_text(errors="replace") if hp.exists() else ""
            blocks = re.split(r"\r?\n\r?\n", raw_headers)
            block = next((x for x in reversed(blocks) if x.startswith("HTTP/")), "")
            response_headers = {k.strip().lower(): v.strip() for row in block.splitlines()[1:] if ":" in row for k, v in [row.split(":", 1)]}
            status = int(parts[0]) if parts and parts[0].isdigit() else 0
            return status, response_headers, bp.read_bytes()[: MAX_BODY + 1] if bp.exists() else b"", details
    if job.get("family") or job.get("http2"):
        raise RuntimeError("UNSUPPORTED:urllib variant")
    handlers: list[Any] = [NoRedirect(), urllib.request.HTTPSHandler(context=ssl_context(job))]
    if not job.get("trust_env", True):
        handlers.append(urllib.request.ProxyHandler({}))
    opener = urllib.request.build_opener(*handlers)
    request = urllib.request.Request(url, headers=headers, data=body, method=method)
    try:
        response = opener.open(request, timeout=timeout)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        details["http_version"] = {10: "HTTP/1.0", 11: "HTTP/1.1"}.get(getattr(response, "version", None), "unknown")
        try:
            sock = response.fp.raw._sock
            details["peer"] = sock.getpeername()
            if isinstance(sock, ssl.SSLSocket):
                details["tls_version"] = sock.version()
                details["cipher"] = sock.cipher()
        except (AttributeError, OSError):
            pass
        return response.code, {k.lower(): v for k, v in response.headers.items()}, response.read(MAX_BODY + 1), details


def request(job: dict[str, Any]) -> dict[str, Any]:
    started = time.monotonic()
    stamp = utc_now()
    result: dict[str, Any] = {"started_at": stamp, "url": safe_url(job["url"]), "engine": job.get("engine", "urllib"), "status": 0, "headers": {}}
    working = copy.deepcopy(job)
    hops: list[dict[str, Any]] = []
    try:
        for _ in range(6):
            try:
                result["locally_resolved_addresses"] = public_target(working["url"], bool(working.get("allow_local")))
            except socket.gaierror as dns_error:
                # HTTP(S) proxies may resolve public names remotely. A failed local
                # lookup must not suppress a request that can work through a proxy.
                parts = urllib.parse.urlsplit(working["url"])
                proxies = urllib.request.getproxies() if working.get("trust_env", True) else {}
                uses_proxy = bool(proxies.get(parts.scheme) or proxies.get("all")) and not urllib.request.proxy_bypass(parts.hostname or "")
                if not uses_proxy and not working.get("resolve"):
                    raise
                result["locally_resolved_addresses"] = []
                result["local_dns_error"] = str(dns_error)
                result["dns_resolution_note"] = "HTTP transport may resolve through the configured proxy"
            status, headers, body, details = perform_one(working)
            result.update(status=status, headers={k.lower(): v for k, v in headers.items() if k.lower() in ALLOWED_HEADERS or "ratelimit" in k.lower() or k.lower().startswith("x-rate")}, **details)
            if "location" in result["headers"]:
                result["headers"]["location"] = safe_url(urllib.parse.urljoin(working["url"], headers["location"]))
            hops.append({"url": safe_url(working["url"]), "status": status, "location": result["headers"].get("location"), "server": headers.get("server")})
            if status not in {301, 302, 303, 307, 308} or not headers.get("location"):
                result.update(inspect_body(body[:MAX_BODY], job.get("expected", "json"), status, len(body) > MAX_BODY))
                break
            destination = urllib.parse.urljoin(working["url"], headers["location"])
            before, after = urllib.parse.urlsplit(working["url"]), urllib.parse.urlsplit(destination)
            if before.scheme == "https" and after.scheme != "https":
                result.update(valid=False, validation="HTTPS_DOWNGRADE_REJECTED")
                break
            if (before.scheme, before.netloc) != (after.scheme, after.netloc):
                working["headers"] = {k: v for k, v in working.get("headers", {}).items() if not SECRET_RE.search(k)}
                working.pop("resolve", None)
                safe_pairs = [(k,v) for k,v in urllib.parse.parse_qsl(after.query, keep_blank_values=True) if not SECRET_RE.search(k)]
                destination = urllib.parse.urlunsplit((after.scheme, after.netloc, after.path, urllib.parse.urlencode(safe_pairs), ""))
            if status == 303 or (status in {301, 302} and working.get("method") == "POST"):
                working["method"] = "GET"
                working.pop("body", None)
            working["url"] = destination
            # Redirects are real requests too; avoid unpaced chains.
            time.sleep(float(job.get("redirect_gap", 1.2)))
        else:
            result.update(valid=False, validation="TOO_MANY_REDIRECTS")
    except Exception as error:
        result.update(valid=False, error_type=type(error).__name__, error=str(error)[:1400])
    result.update(hops=hops, elapsed_s=round(time.monotonic() - started, 4))
    return result


def environment_info() -> dict[str, Any]:
    versions: dict[str, str] = {}
    for package in ("httpx", "httpcore", "certifi", "h2", "gigachat", "huggingface-hub", "anyio"):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            pass
    info: dict[str, Any] = {"python": platform.python_version(), "platform": platform.platform(), "machine": platform.machine(), "openssl": ssl.OPENSSL_VERSION, "packages": versions, "proxy_present": {key: bool(os.environ.get(key)) for key in PROXY_KEYS}, "ca_environment": {key: {"present": bool(os.environ.get(key)), "exists": pathlib.Path(os.environ[key]).exists() if os.environ.get(key) else False} for key in TLS_ENV_KEYS}, "ca_defaults": ssl.get_default_verify_paths()._asdict(), "utc": utc_now()}
    if pathlib.Path("/etc/os-release").exists():
        info["os_release"] = pathlib.Path("/etc/os-release").read_text()[:1500]
    if pathlib.Path("/etc/resolv.conf").exists():
        info["resolver"] = pathlib.Path("/etc/resolv.conf").read_text()[:1500]
    if shutil.which("curl"):
        info["curl"] = subprocess.run(["curl", "-q", "--version"], text=True, capture_output=True, timeout=5).stdout[:1200]
    return info


def worker() -> None:
    for line in sys.stdin:
        try:
            message = json.loads(line)
            operation = message.get("operation", "request")
            if operation == "init":
                for key in (*PROXY_KEYS, *TLS_ENV_KEYS):
                    if key in message.get("environment", {}):
                        os.environ[key] = message["environment"][key]
                response = environment_info()
            elif operation == "dns":
                addresses = socket.getaddrinfo(message["host"], message.get("port", 443), type=socket.SOCK_STREAM)
                response = {"addresses": sorted({item[4][0] for item in addresses})}
            elif operation == "request":
                response = request(message)
            elif operation == "project_readonly":
                import psycopg
                from collector.config import CollectorSettings
                settings = CollectorSettings()
                with psycopg.connect(settings.pg_dsn, connect_timeout=6, options="-c default_transaction_read_only=on -c statement_timeout=5000") as connection:
                    with connection.cursor() as cursor:
                        cursor.execute("SELECT source_key,rate_limit_rps::text,enabled,requires_api_key FROM collector.source_catalog ORDER BY source_key")
                        catalog = [{"source": row[0], "rate_rps": row[1], "enabled": row[2], "requires_key": row[3]} for row in cursor.fetchall()]
                        cursor.execute("SELECT status,count(*) FROM collector.collections GROUP BY status")
                        collection_counts = dict(cursor.fetchall())
                response = {"catalog": catalog, "collection_counts": collection_counts, "settings": {"workers": settings.collector_workers, "replicas": settings.collector_replicas, "http_retries": settings.collector_http_retries, "source_override": settings.source_enabled_override, "source_policy": settings.collector_source_policy}}
            elif operation == "oauth":
                job = dict(message["job"])
                try:
                    public_target(job["url"])
                except socket.gaierror:
                    if not urllib.request.getproxies():
                        raise
                status, headers, body, details = perform_one(job)
                response = {"status": status, "headers": {k: v for k, v in headers.items() if k in ALLOWED_HEADERS}, **details}
                payload = json.loads(body)
                if status == 200 and isinstance(payload, dict) and payload.get("access_token"):
                    # Returned through a private pipe; controller never records this field.
                    response["private_access_value"] = payload["access_token"]
                    response["valid"] = True
                else:
                    response.update(valid=False, sample=body[:1000].decode("utf-8", "replace"))
            else:
                response = {"error": "Unknown worker operation", "valid": False}
        except Exception as error:
            response = {"error_type": type(error).__name__, "error": str(error)[:1400], "valid": False}
        print(json.dumps(response, ensure_ascii=False), flush=True)


class Worker:
    def __init__(self, name: str, command: list[str], environment: dict[str, str], log: Any, docker_name: str = "") -> None:
        self.name, self.docker_name, self.log = name, docker_name, log
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
        self.messages: queue.Queue[str | None] = queue.Queue()
        self.stderr: list[str] = []
        def read_stdout() -> None:
            assert self.process.stdout is not None
            for line in self.process.stdout:
                self.messages.put(line)
            self.messages.put(None)
        def read_stderr() -> None:
            assert self.process.stderr is not None
            for line in self.process.stderr:
                self.stderr.append(line[:2000])
                self.stderr[:] = self.stderr[-10:]
        threading.Thread(target=read_stdout, daemon=True).start()
        threading.Thread(target=read_stderr, daemon=True).start()
        self.info = self.call({"operation": "init", "environment": {k: v for k, v in environment.items() if k in (*PROXY_KEYS, *TLS_ENV_KEYS)}}, 35)

    def call(self, message: dict[str, Any], timeout: float = 80) -> dict[str, Any]:
        try:
            if self.process.poll() is not None:
                raise RuntimeError("worker exited; " + "".join(self.stderr))
            assert self.process.stdin is not None
            self.process.stdin.write(json.dumps(message) + "\n")
            self.process.stdin.flush()
            began = time.monotonic()
            while True:
                remaining = timeout - (time.monotonic() - began)
                if remaining <= 0:
                    raise TimeoutError("Worker deadline exceeded")
                try:
                    value = self.messages.get(timeout=min(10, remaining))
                    if value is None:
                        raise RuntimeError("worker closed; " + "".join(self.stderr))
                    return json.loads(value)
                except queue.Empty:
                    self.log(f"Ожидание {self.name}: {int(time.monotonic()-began)} с")
        except (OSError, ValueError, RuntimeError, TimeoutError) as error:
            self.close()
            return {"valid": False, "error_type": type(error).__name__, "error": str(error)[:1600], "status": 0}

    def close(self) -> None:
        if self.process.poll() is None:
            try:
                if self.process.stdin:
                    self.process.stdin.close()
                self.process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                self.process.terminate()
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.process.kill()
        if self.docker_name:
            subprocess.run(["docker", "rm", "-f", self.docker_name], capture_output=True, timeout=20)


@dataclass
class Endpoint:
    name: str
    source: str
    url: str
    expected: str
    headers: dict[str, str] = field(default_factory=dict)
    method: str = "GET"
    body: str | None = None
    credentials_present: bool = False
    state: str = "ACTIVE"


def endpoint_inventory(env: dict[str, str], root: pathlib.Path) -> tuple[list[Endpoint], list[dict[str, Any]]]:
    now = dt.datetime.now(dt.timezone.utc)
    since = (now - dt.timedelta(days=730)).date().isoformat()
    encode = urllib.parse.urlencode
    endpoints = [
        Endpoint("arxiv", "arxiv", "https://export.arxiv.org/api/query?" + encode({"search_query": 'all:"robotics"', "start": 0, "max_results": 2, "sortBy": "submittedDate", "sortOrder": "descending"}), "atom", {"Accept": "application/atom+xml, application/xml;q=0.9"}),
        Endpoint("gdelt_recent", "gdelt", "https://api.gdeltproject.org/api/v2/doc/doc?" + encode({"query": '"robotics" (pilot OR funding OR startup OR contract OR launch OR partnership OR deployment) sourcelang:english', "mode": "artlist", "format": "json", "maxrecords": 10, "timespan": "24h"}), "gdelt"),
        Endpoint("semantic_bulk", "semantic_scholar", "https://api.semanticscholar.org/graph/v1/paper/search/bulk?" + encode({"query": "robotics", "fields": FIELDS, "sort": "publicationDate:desc", "publicationDateOrYear": since + ":"}), "semantic"),
        Endpoint("semantic_relevance", "semantic_scholar", "https://api.semanticscholar.org/graph/v1/paper/search?" + encode({"query": "robotics", "fields": FIELDS, "limit": 2, "publicationDateOrYear": since + ":"}), "semantic"),
        Endpoint("openalex", "openalex", "https://api.openalex.org/works?" + encode({"search": "robotics", "per-page": 2, "filter": "from_publication_date:" + since, "select": "id,title,doi,publication_date,type,abstract_inverted_index"}), "openalex"),
        Endpoint("github_search", "github", "https://api.github.com/search/repositories?" + encode({"q": "robotics", "per_page": 2}), "github", {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}),
        Endpoint("zenodo", "zenodo", "https://zenodo.org/api/records?" + encode({"q": "robotics", "type": "publication", "subtype": "preprint", "size": 2, "sort": "mostrecent"}), "zenodo", {"Accept": "application/json"}),
        Endpoint("hackernews", "hackernews", "https://hn.algolia.com/api/v1/search?" + encode({"query": "robotics", "tags": "story", "hitsPerPage": 2}), "hackernews"),
        Endpoint("rospatent", "rospatent", "https://searchplatform.rospatent.gov.ru/patsearch/v0.2/search", "rospatent", {"Content-Type": "application/json"}, "POST", json.dumps({"qn": "робототехника", "limit": 1, "filter": {"date_published": {"range": {"gte": since[:4] + "0101"}}}})),
        Endpoint("huggingface_api", "huggingface", "https://huggingface.co/api/models/" + env.get("WS_EMBEDDING_MODEL", "intfloat/multilingual-e5-base"), "json"),
        Endpoint("huggingface_resolve", "huggingface", "https://huggingface.co/" + env.get("WS_EMBEDDING_MODEL", "intfloat/multilingual-e5-base") + "/resolve/main/config.json", "json"),
        Endpoint("hh_disabled", "hh", "https://api.hh.ru/vacancies?text=Golang&per_page=1", "github", state="STUB_DISABLED"),
        Endpoint("patentsview_disabled", "patentsview", "https://search.patentsview.org/api/v1/patent/?" + encode({"q": json.dumps({"_text_any": {"patent_title": "robotics"}}), "o": json.dumps({"size": 1})}), "json", state="STUB_DISABLED"),
    ]
    for lang, title in (("en", "Robotics"), ("ru", "Робототехника")):
        quoted = urllib.parse.quote(title, safe="")
        endpoints += [Endpoint(f"wikipedia_{lang}_summary", "wikipedia", f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/{quoted}", "wikipedia_summary"), Endpoint(f"wikipedia_{lang}_revisions", "wikipedia", f"https://{lang}.wikipedia.org/w/api.php?" + encode({"action": "query", "prop": "revisions", "titles": title, "rvprop": "timestamp", "rvdir": "newer", "rvlimit": 1, "format": "json", "maxlag": 5}), "wikipedia_action")]
        start, end = now - dt.timedelta(days=8), now - dt.timedelta(days=2)
        endpoints.append(Endpoint(f"wikipedia_{lang}_pageviews", "wikipedia", f"https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/{lang}.wikipedia/all-access/user/{quoted}/daily/{start:%Y%m%d}00/{end:%Y%m%d}00", "wikipedia_views"))
    keys = {"semantic_scholar": ("WS_SEMANTIC_SCHOLAR_API_KEY", "x-api-key", ""), "github": ("WS_GITHUB_TOKEN", "Authorization", "Bearer "), "rospatent": ("WS_ROSPATENT_TOKEN", "Authorization", "Bearer "), "patentsview": ("WS_PATENTSVIEW_API_KEY", "X-Api-Key", ""), "huggingface": ("HF_TOKEN", "Authorization", "Bearer ")}
    for endpoint in endpoints:
        if endpoint.source == "openalex" and env.get("WS_OPENALEX_API_KEY"):
            endpoint.url += "&" + encode({"api_key": env["WS_OPENALEX_API_KEY"]})
            endpoint.credentials_present = True
        if endpoint.source in keys:
            key, header, prefix = keys[endpoint.source]
            if env.get(key):
                endpoint.headers[header] = prefix + env[key]
                endpoint.credentials_present = True
    active = split_feeds(env.get("WS_COLLECTOR_RSS_FEEDS", ""))
    candidates: list[str] = []
    for path in sorted((root / "tools/quality").glob("check_*feeds.py")):
        try:
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.startswith(("https://", "http://")) and "\n" not in node.value:
                    candidates.append(node.value)
        except (OSError, SyntaxError):
            continue
    feed_inventory = []
    for index, url in enumerate(dict.fromkeys(active + candidates), 1):
        state = "CONFIGURED" if url in active else "CANDIDATE_IN_REPO"
        endpoints.append(Endpoint(f"rss_{index:02d}", "rss", url, "rss", state=state))
        feed_inventory.append({"id": f"rss_{index:02d}", "url": safe_url(url), "state": state})
    return endpoints, feed_inventory


def run_command(command: list[str], timeout: float = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, text=True, capture_output=True, timeout=timeout)


def atomic_json(path: pathlib.Path, data: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


class Doctor:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.root = pathlib.Path.cwd()
        self.stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
        self.base = self.root / f"{REPORT_PREFIX}_{self.stamp}"
        self.redactor = Redactor()
        self.log_file = self.base.with_suffix(".log").open("x", encoding="utf-8")
        os.chmod(self.base.with_suffix(".log"), 0o600)
        self.report: dict[str, Any] = {"version": VERSION, "started_at": utc_now(), "scope": "Availability from this machine only; no provider-wide SLA or proof of internal WAF rules", "notes": [], "environments": {}, "probes": [], "feed_inventory": [], "code_evidence": [], "commands": [], "isolation": {"requested": args.isolate, "stopped": [], "restored": []}}
        self.workers: list[Worker] = []
        self.environment: dict[str, str] = {}
        self.service_env: dict[str, dict[str, str]] = {}
        self.containers: list[dict[str, Any]] = []
        self.next_at: dict[str, float] = {}
        self.block_until: dict[str, float] = {}
        self.throttle_counts: dict[str, int] = {}
        self.request_counts: dict[str, int] = {}
        self.stopped: list[str] = []
        self.network = "bridge"
        self.docker_available = False
        self.deadline = time.monotonic() + 90 * 60

    def log(self, message: str) -> None:
        line = self.redactor.text(f"[{dt.datetime.now().strftime('%H:%M:%S')}] {message}")
        print(line, flush=True)
        self.log_file.write(line + "\n")
        self.log_file.flush()

    def note(self, message: str) -> None:
        self.report["notes"].append(self.redactor.text(message))
        self.log(message)

    def checkpoint(self) -> None:
        atomic_json(self.base.with_suffix(".json"), self.redactor.clean(self.report))

    def setup(self) -> None:
        relevant = (
            "compose.yaml", "uv.lock", "services/collector/Dockerfile",
            "services/collector/src/collector/main.py",
            "services/collector/src/collector/domain/source_policy.py",
            "services/collector/src/collector/adapters/outbound/http_client.py",
            "services/collector/src/collector/adapters/outbound/rate_limiter.py",
            "services/collector/src/collector/adapters/outbound/sources/base.py",
            "services/collector/src/collector/adapters/outbound/sources/rss.py",
            "services/collector/src/collector/adapters/outbound/sources/arxiv.py",
            "services/collector/src/collector/adapters/outbound/sources/gdelt.py",
        )
        for relative in relevant:
            item = self.root / relative
            if item.is_file():
                self.report["code_evidence"].append({"path": relative, "sha256": digest(item.read_bytes())})
        values, notes = read_dotenv(self.root / ".env")
        self.environment = values
        self.environment.update({k: v for k, v in os.environ.items() if k.startswith("WS_") or k in (*PROXY_KEYS, *TLS_ENV_KEYS, "HF_TOKEN")})
        self.redactor.extend(self.environment)
        self.report["notes"].extend(notes)
        if shutil.which("docker") and not self.args.no_docker:
            try:
                result = run_command(["docker", "info", "--format", "{{json .ServerVersion}}"], 15)
                self.docker_available = result.returncode == 0
                self.report["docker_version"] = result.stdout.strip() if self.docker_available else "unavailable"
            except (OSError, subprocess.TimeoutExpired):
                pass
        if self.docker_available:
            config = run_command(["docker", "compose", "config", "--format", "json"], 40)
            if config.returncode == 0:
                obj = json.loads(config.stdout)
                self.project = obj.get("name", "weak-signals")
                for name, service in obj.get("services", {}).items():
                    effective = {k: str(v) for k, v in service.get("environment", {}).items() if v is not None}
                    self.service_env[name] = effective
                    self.redactor.extend(effective)
                self.environment.update(self.service_env.get("collector", {}))
            else:
                self.project = "weak-signals"
                self.note("Compose config недоступен; используется безопасный ограниченный разбор .env. " + config.stderr[:400])
            ps = run_command(["docker", "ps", "-a", "--filter", f"label=com.docker.compose.project={self.project}", "--format", "{{.ID}}"])
            if ps.returncode == 0 and ps.stdout.strip():
                inspected = run_command(["docker", "inspect", *ps.stdout.split()])
                for container in json.loads(inspected.stdout or "[]"):
                    labels = container.get("Config", {}).get("Labels", {})
                    service = labels.get("com.docker.compose.service", "")
                    self.containers.append(container)
                    runtime_env = dict(row.split("=", 1) for row in container.get("Config", {}).get("Env", []) if "=" in row)
                    self.redactor.extend(runtime_env)
                    if service in {"collector", "insight", "analyzer"}:
                        previous = self.service_env.get(service, {})
                        differences = [key for key in set(previous) | set(runtime_env) if key.startswith("WS_") and previous.get(key) != runtime_env.get(key)]
                        self.report.setdefault("runtime_config_differences", {})[service] = differences
                        self.service_env[service] = runtime_env
                    if service == "collector":
                        self.environment.update(runtime_env)
                        nets = list(container.get("NetworkSettings", {}).get("Networks", {}))
                        if nets:
                            self.network = nets[0]
                self.report["project_services"] = [{"service": c["Config"].get("Labels", {}).get("com.docker.compose.service"), "running": c["State"]["Running"], "paused": c["State"].get("Paused", False), "image_id": c["Image"]} for c in self.containers]
        else:
            self.note("Docker недоступен/отключён: контейнерная матрица будет явно отмечена как непроверенная.")
        self.redactor.extend(self.environment)
        self.report["credential_presence"] = {k: {"present": bool(v), "has_surrounding_whitespace": bool(v and v != v.strip()), "looks_like_placeholder": bool(v and (v.startswith("#") or v.lower() in {"change-me", "your-api-key"}))} for k, v in self.environment.items() if k.startswith("WS_") and SECRET_RE.search(k)}
        self.report["contact_email_placeholder"] = not self.environment.get("WS_CONTACT_EMAIL") or self.environment.get("WS_CONTACT_EMAIL", "").endswith(("@example.org", "@example.com"))
        for key, value in list(self.environment.items()):
            if key.startswith("WS_") and value.lstrip().startswith("#"):
                self.environment[key] = ""
                self.note(f"{key}: значение-комментарий, используется пустое значение как в BaseServiceSettings")
        self.report["diagnostic_network"] = self.network
        for relative, needle in [("services/collector/src/collector/adapters/outbound/http_client.py", "RETRY_BASE_DELAY"), ("services/collector/src/collector/adapters/outbound/rate_limiter.py", "CAPACITY_MULTIPLIER"), ("services/collector/src/collector/adapters/outbound/rate_limiter.py", "async def penalize"), ("services/collector/migrations/0001_init.sql", "0.400"), ("services/collector/src/collector/adapters/outbound/sources/rss.py", "rss.feed_failed"), ("services/collector/Dockerfile", "FROM python:")]:
            path = self.root / relative
            if path.exists():
                content = path.read_bytes()
                self.report["code_evidence"].append({"file": relative, "sha256": digest(content), "matches": [{"line": i, "text": line.strip()} for i, line in enumerate(content.decode(errors="replace").splitlines(), 1) if needle in line]})

    def build_images(self) -> dict[str, str]:
        if not self.docker_available or self.args.no_build:
            return {}
        pins = {"httpx": "0.28.1", "httpcore": "1.0.9", "h11": "0.16.0", "anyio": "4.10.0", "idna": "3.10", "certifi": "2025.8.3", "sniffio": "1.3.1", "typing-extensions": "4.15.0"}
        lock = self.root / "uv.lock"
        if lock.exists():
            for block in lock.read_text().split("[[package]]"):
                name = re.search(r'^name = "([^"]+)"', block, re.M)
                version = re.search(r'^version = "([0-9A-Za-z.+_-]+)"', block, re.M)
                if name and version and name[1] in pins:
                    pins[name[1]] = version[1]
        pins.update({"h2": "4.3.0", "hpack": "4.1.0", "hyperframe": "6.1.0"})
        self.report["matrix_python_packages"] = pins
        images = {}
        with tempfile.TemporaryDirectory(prefix="ws-api-build-") as directory:
            path = pathlib.Path(directory)
            (path / "requirements.txt").write_text("\n".join(f"{k}=={v}" for k, v in pins.items()) + "\n")
            for distro in ("bookworm", "trixie"):
                image = f"weak-signals-api-doctor:{distro}-v1"
                (path / "Dockerfile").write_text(f"FROM python:3.12-slim-{distro}\nRUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates && rm -rf /var/lib/apt/lists/*\nCOPY requirements.txt /tmp/requirements.txt\nRUN python -m pip install --no-cache-dir --disable-pip-version-check -r /tmp/requirements.txt\nENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1\n")
                self.log(f"Подготовка диагностического образа {distro}; код проекта в сборку не передаётся")
                command = ["docker", "build", "--pull", "--progress=plain", "-t", image, str(path)]
                with tempfile.TemporaryFile(mode="w+t") as output:
                    proc = subprocess.Popen(command, stdout=output, stderr=subprocess.STDOUT, text=True)
                    began = time.monotonic()
                    while proc.poll() is None:
                        if time.monotonic() - began > 900:
                            proc.terminate()
                            self.note(f"Сборка {distro}: превышен предел 15 минут")
                            break
                        self.log(f"Сборка {distro}: выполняется, {int(time.monotonic()-began)} с")
                        try:
                            proc.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            continue
                    try:
                        proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()
                    output.seek(0)
                    build_output = output.read()
                    self.report["commands"].append({"operation": "build_" + distro, "returncode": proc.returncode, "output_tail": self.redactor.text(build_output[-6000:])})
                if proc.returncode == 0:
                    images[distro] = image
                else:
                    self.note(f"Образ {distro} не собран; матрица будет неполной, подробности в JSON")
        return images

    def isolate(self) -> None:
        if not self.args.isolate or not self.docker_available:
            self.note("Фоновый трафик не остановлен. Причины 429 могут быть смешаны с другими процессами/клиентами IP.")
            return
        selected = [c for c in self.containers if c["State"]["Running"] and not c["State"].get("Paused") and c["Config"].get("Labels", {}).get("com.docker.compose.service") in {"collector", "insight"}]
        for container in selected:
            if container["Config"].get("Labels", {}).get("com.docker.compose.service") != "collector":
                continue
            # Read only: actual SQL catalog may differ from migration defaults.
            code = pathlib.Path(__file__).read_text()
            payload = code + "\n"
            result = subprocess.run(["docker", "exec", "-i", container["Id"], "python", "-c", "import sys,json,types; text=sys.stdin.read(); mod=types.ModuleType('api_doctor_lib'); sys.modules[mod.__name__]=mod; exec(compile(text,'api_doctor','exec'),mod.__dict__); import io; sys.stdin=io.StringIO(json.dumps({'operation':'project_readonly'})+'\\n'); mod.worker()"], input=payload, text=True, capture_output=True, timeout=25)
            if result.returncode == 0:
                try:
                    self.report["actual_project_state"] = self.redactor.clean(json.loads(result.stdout))
                except ValueError:
                    self.note("Каталог БД не удалось разобрать; миграции не считаются фактической конфигурацией")
            else:
                self.note("Read-only проверка каталога БД недоступна; фактические значения лимитов не подтверждены")
        for container in selected:
            ident = container["Id"]
            self.stopped.append(ident)
            atomic_json(self.root / RECOVERY_NAME, {"project": self.project, "containers": self.stopped, "created_at": utc_now()})
            name = container["Config"]["Labels"]["com.docker.compose.service"]
            self.log(f"Временная штатная остановка {name}; восстановление записано")
            result = run_command(["docker", "stop", "--time", "35", ident], 50)
            self.report["isolation"]["stopped"].append({"service": name, "id": ident[:12], "success": result.returncode == 0})
            if result.returncode:
                raise RuntimeError("Не удалось изолировать проект; тесты не начинаются")
        self.log("Пауза 65 с для завершения недавних запросов; это не доказательство сброса серверной квоты")
        self.wait_seconds(65)

    def restore(self) -> None:
        if not self.stopped:
            return
        remaining = []
        for ident in self.stopped:
            try:
                result = run_command(["docker", "start", ident], 35)
                ok = result.returncode == 0
            except (OSError, subprocess.TimeoutExpired):
                ok = False
            self.report["isolation"]["restored"].append({"id": ident[:12], "success": ok})
            self.log(f"Восстановление контейнера {ident[:12]}: {'OK' if ok else 'ОШИБКА; используй --restore'}")
            if not ok:
                remaining.append(ident)
        self.stopped = remaining
        if remaining:
            atomic_json(self.root / RECOVERY_NAME, {"project": self.project, "containers": remaining})
        else:
            (self.root / RECOVERY_NAME).unlink(missing_ok=True)

    def wait_seconds(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            remaining = end - time.monotonic()
            if remaining > 10:
                self.log(f"Пауза: осталось {int(remaining)} с")
            time.sleep(min(10, remaining))

    def add_worker(self, name: str, image: str | None = None, env: dict[str, str] | None = None, container: dict[str, Any] | None = None, network: str | None = None) -> Worker | None:
        docker_name = ""
        if image:
            docker_name = "ws-api-doctor-" + uuid.uuid4().hex[:10]
            command = ["docker", "run", "--rm", "-i", "--name", docker_name, "--label", "weak-signals.api-doctor=true", "--network", network or self.network, "--read-only", "--tmpfs", "/tmp:rw,nosuid,nodev,size=64m", "--mount", f"type=bind,source={pathlib.Path(__file__).resolve()},target=/api_doctor.py,readonly", "--entrypoint", "python"]
            if container:
                command += ["--volumes-from", container["Id"] + ":ro"]
            command += [image, "-B", "/api_doctor.py", "--worker"]
        else:
            command = [sys.executable, "-B", str(pathlib.Path(__file__).resolve()), "--worker"]
        effective_env = env if env is not None else self.service_env.get("collector", self.environment)
        worker_obj = Worker(name, command, effective_env, self.log, docker_name)
        if worker_obj.info.get("error"):
            self.report["environments"][name] = self.redactor.clean(worker_obj.info)
            self.note(f"Среда {name} не запущена: " + str(worker_obj.info.get("error")))
            worker_obj.close()
            return None
        self.workers.append(worker_obj)
        self.report["environments"][name] = worker_obj.info
        if image:
            inspected = run_command(["docker", "image", "inspect", image, "--format", "{{json .}}"])
            if inspected.returncode == 0:
                meta = json.loads(inspected.stdout)
                self.report["environments"][name]["image"] = {"id": meta.get("Id"), "digests": meta.get("RepoDigests", []), "architecture": meta.get("Architecture")}
        self.log(f"Среда {name}: Python {worker_obj.info.get('python')}; {worker_obj.info.get('openssl')}")
        return worker_obj

    def probe(self, target: Worker, endpoint: Endpoint, variant: str = "baseline", **options: Any) -> dict[str, Any]:
        host = urllib.parse.urlsplit(endpoint.url).hostname or ""
        group = endpoint.source if endpoint.source != "rss" else "rss:" + host.removeprefix("www.")
        if host.endswith("arxiv.org"):
            group = "arxiv"
        record: dict[str, Any] = {"environment": target.name, "endpoint": endpoint.name, "source": endpoint.source, "state": endpoint.state, "variant": variant, "auth_mode": "configured" if endpoint.credentials_present else "anonymous"}
        if time.monotonic() > self.deadline:
            record.update(skipped="GLOBAL_90_MINUTE_BUDGET", status=0, valid=False)
        elif target.process.poll() is not None:
            record.update(skipped="WORKER_UNAVAILABLE", status=0, valid=False)
        elif self.throttle_counts.get(group, 0) >= 2:
            record.update(skipped="CIRCUIT_OPEN_AFTER_TWO_THROTTLES", status=0, valid=False)
        elif self.request_counts.get(group, 0) >= (60 if endpoint.source != "rss" else 120):
            record.update(skipped="DIAGNOSTIC_REQUEST_BUDGET", status=0, valid=False)
        else:
            wait = max(self.next_at.get(group, 0), self.block_until.get(group, 0)) - time.time()
            if wait > 180:
                record.update(skipped="SERVER_COOLDOWN_LONGER_THAN_WAIT_BUDGET", resume_after_utc=dt.datetime.fromtimestamp(time.time()+wait, dt.timezone.utc).isoformat(), status=0, valid=False)
            else:
                if wait > 0:
                    self.wait_seconds(wait)
                ua = "weak-signals/1.0"
                contact = self.environment.get("WS_CONTACT_EMAIL", "")
                if contact:
                    ua += f" (mailto:{contact})"
                engine = options.pop("engine", "httpx" if "httpx" in target.info.get("packages", {}) else "urllib")
                job = {"url": endpoint.url, "headers": {"User-Agent": ua, **endpoint.headers}, "method": endpoint.method, "body": endpoint.body, "expected": endpoint.expected, "engine": engine, "timeout": 50, "redirect_gap": GAPS.get(endpoint.source, 2), **options}
                if job.pop("rss_accept", False):
                    job["headers"]["Accept"] = "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.8"
                if job.pop("explicit_ua", False):
                    job["headers"]["User-Agent"] = ua.replace("weak-signals/1.0", "weak-signals-api-doctor/1.0")
                extra = job.pop("extra_headers", {})
                job["headers"].update(extra)
                self.log(f"{target.name} | {endpoint.name} | {variant} | {engine}")
                began = time.time()
                response = target.call(job, float(job["timeout"]) + 60)
                record.update(response, options={k: v for k, v in options.items() if k != "extra_headers"})
                if endpoint.expected == "robots":
                    crawl = response.get("crawl_delay_s")
                    rate = response.get("request_rate")
                    delay_hint = max(float(crawl or 0), float(rate["seconds"])/float(rate["requests"]) if rate and rate.get("requests") else 0)
                    if delay_hint:
                        self.report.setdefault("robots_delays", {})[group] = delay_hint
                self.next_at[group] = time.time() + GAPS.get(endpoint.source, 2)
                if self.report.get("robots_delays", {}).get(group):
                    self.next_at[group] = max(self.next_at[group], time.time() + self.report["robots_delays"][group])
                self.request_counts[group] = self.request_counts.get(group, 0) + max(1, len(response.get("hops", [])))
                if rate_limited(response):
                    self.throttle_counts[group] = self.throttle_counts.get(group, 0) + 1
                    delay = retry_delay(response.get("headers", {}))
                    if response.get("headers", {}).get("x-ratelimit-remaining") == "0":
                        try:
                            reset_value = float(response["headers"]["x-ratelimit-reset"])
                            # OpenAlex documents delta seconds; GitHub documents Unix time.
                            reset_delay = reset_value if endpoint.source == "openalex" else reset_value - time.time()
                            delay = max(delay or 0, reset_delay)
                        except (KeyError, ValueError):
                            pass
                    if endpoint.source == "huggingface":
                        advertised = response.get("headers", {}).get("ratelimit", "")
                        seconds_match = re.search(r"(?:^|;)\s*t=(\d+)", advertised)
                        if seconds_match:
                            delay = max(delay or 0, float(seconds_match[1]))
                    pause = max(delay or 0, 120 if endpoint.source in {"gdelt", "arxiv"} else 65)
                    self.block_until[group] = time.time() + pause
                    record["cooldown_s"] = pause
                elif response.get("status", 0) >= 500:
                    self.block_until[group] = time.time() + max(15, retry_delay(response.get("headers", {})) or 0)
                record["controller_elapsed_s"] = round(time.time()-began, 3)
                self.log(f"  HTTP {record.get('status',0)} | {record.get('validation',record.get('error_type','?'))} | {record.get('elapsed_s',0)} с")
        if record.get("skipped"):
            self.log(f"{target.name} | {endpoint.name} | пропуск: {record['skipped']}")
        cleaned = self.redactor.clean(record)
        self.report["probes"].append(cleaned)
        self.checkpoint()
        return cleaned

    def matrix(self, endpoints: list[Endpoint], targets: list[Worker]) -> None:
        baseline: dict[tuple[str, str], dict[str, Any]] = {}
        # Interleave environments per endpoint to reduce time-of-day confounding.
        for endpoint in endpoints:
            for target in targets:
                result = self.probe(target, endpoint)
                baseline[(target.name, endpoint.name)] = result
        core = [x for x in endpoints if x.name in {"arxiv", "gdelt_recent", "semantic_bulk", "semantic_relevance"}]
        for endpoint in core:
            for target in reversed(targets):
                self.probe(target, endpoint, "repeat_reverse_order")
        for target in targets:
            for endpoint in endpoints:
                initial = baseline[(target.name, endpoint.name)]
                if initial.get("skipped"):
                    continue
                failed = not initial.get("valid") or initial.get("status") != 200
                investigate = failed or endpoint in core
                if not investigate:
                    continue
                engine = "curl" if target.info.get("curl") else "urllib"
                self.probe(target, endpoint, "other_http_client", engine=engine)
                if engine == "curl":
                    self.probe(target, endpoint, "ipv4", engine="curl", family=4)
                    self.probe(target, endpoint, "ipv6", engine="curl", family=6)
                    if endpoint in core:
                        self.probe(target, endpoint, "tls12", engine="curl", tls="1.2")
                        self.probe(target, endpoint, "tls13", engine="curl", tls="1.3")
                        if "HTTP2" in target.info.get("curl", ""):
                            self.probe(target, endpoint, "http2_requested", engine="curl", http2=True)
                if endpoint in core and "httpx" in target.info.get("packages", {}):
                    self.probe(target, endpoint, "httpx_tls12", engine="httpx", tls="1.2")
                    self.probe(target, endpoint, "httpx_tls13", engine="httpx", tls="1.3")
                if any(target.info.get("proxy_present", {}).values()):
                    self.probe(target, endpoint, "without_environment_proxy", engine=engine, trust_env=False)
                if endpoint.source == "rss":
                    self.probe(target, endpoint, "rss_accept_header", rss_accept=True)
                    self.probe(target, endpoint, "explicit_diagnostic_user_agent", explicit_ua=True)
                if failed and engine == "curl" and initial.get("status", 0) == 0:
                    host = urllib.parse.urlsplit(endpoint.url).hostname or ""
                    dns = target.call({"operation": "dns", "host": host}, 20)
                    self.report.setdefault("dns", {}).setdefault(target.name, {})[host] = dns
                    ips = [x for x in dns.get("addresses", []) if ":" not in x]
                    if ips:
                        self.probe(target, endpoint, "pin_current_dns_address", engine="curl", resolve=ips[0], family=4, trust_env=False)
        if targets:
            primary = next((x for x in targets if x.name == "project_collector"), targets[0])
            for endpoint in endpoints:
                if endpoint.source == "rss":
                    initial = baseline[(primary.name, endpoint.name)]
                    headers = initial.get("headers", {})
                    conditional = {}
                    if headers.get("etag"):
                        conditional["If-None-Match"] = headers["etag"]
                    elif headers.get("last-modified"):
                        conditional["If-Modified-Since"] = headers["last-modified"]
                    if conditional and initial.get("valid"):
                        self.probe(primary, endpoint, "conditional_get", extra_headers=conditional)
                if endpoint.credentials_present and endpoint.source in {"semantic_scholar", "openalex", "github"}:
                    anonymous = copy.deepcopy(endpoint)
                    anonymous.credentials_present = False
                    anonymous.headers = {k: v for k, v in anonymous.headers.items() if not SECRET_RE.search(k)}
                    parsed = urllib.parse.urlsplit(anonymous.url)
                    query = [(k,v) for k,v in urllib.parse.parse_qsl(parsed.query) if not SECRET_RE.search(k)]
                    anonymous.url = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urllib.parse.urlencode(query), ""))
                    self.probe(primary, anonymous, "auth_control_anonymous")
            self.query_controls(primary, endpoints)

    def query_controls(self, target: Worker, endpoints: list[Endpoint]) -> None:
        gdelt = next(x for x in endpoints if x.name == "gdelt_recent")
        simple = copy.deepcopy(gdelt)
        simple.url = "https://api.gdeltproject.org/api/v2/doc/doc?" + urllib.parse.urlencode({"query": "robotics", "mode": "artlist", "format": "json", "maxrecords": 10, "timespan": "24h"})
        self.probe(target, simple, "simple_query")
        old = copy.deepcopy(simple)
        now = dt.datetime.now(dt.timezone.utc)
        query = {"query": "robotics", "mode": "artlist", "format": "json", "maxrecords": 10, "startdatetime": (now-dt.timedelta(days=420)).strftime("%Y%m%d%H%M%S"), "enddatetime": (now-dt.timedelta(days=400)).strftime("%Y%m%d%H%M%S")}
        old.url = "https://api.gdeltproject.org/api/v2/doc/doc?" + urllib.parse.urlencode(query)
        self.probe(target, old, "historical_window")
        semantic = next(x for x in endpoints if x.name == "semantic_bulk")
        smaller = copy.deepcopy(semantic)
        parsed = urllib.parse.urlsplit(smaller.url)
        pairs = dict(urllib.parse.parse_qsl(parsed.query))
        pairs["fields"] = "title"
        smaller.url = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urllib.parse.urlencode(pairs), ""))
        self.probe(target, smaller, "minimal_fields")

    def huggingface_runtime_checks(self) -> None:
        container = next((c for c in self.containers if c["Config"].get("Labels", {}).get("com.docker.compose.service") == "analyzer"), None)
        if not container:
            self.note("Hugging Face: фактический образ analyzer не найден; остаются базовые сетевые проверки других сред")
            return
        env = self.service_env.get("analyzer", self.environment)
        self.redactor.extend(env)
        self.report["huggingface_runtime"] = {"offline_configured": env.get("HF_HUB_OFFLINE", "0"), "model": env.get("WS_EMBEDDING_MODEL", "intfloat/multilingual-e5-base"), "cache_path": env.get("HF_HOME", ""), "token_present": bool(env.get("HF_TOKEN"))}
        target = self.add_worker("project_analyzer", container["Image"], env, container)
        if target:
            for endpoint in endpoint_inventory(env, self.root)[0]:
                if endpoint.source == "huggingface":
                    self.probe(target, endpoint, "actual_analyzer_image")

    def llm_checks(self, fallback: Worker | None) -> None:
        env = self.service_env.get("insight", self.environment)
        container = next((c for c in self.containers if c["Config"].get("Labels", {}).get("com.docker.compose.service") == "insight"), None)
        target = self.add_worker("project_insight", container["Image"], env, container) if container else fallback
        if target is None:
            self.note("LLM: нет среды для проверки")
            return
        self.redactor.extend(env)
        engine = "httpx" if "httpx" in target.info.get("packages", {}) else "urllib"
        gigachat = env.get("WS_GIGACHAT_CREDENTIALS", "")
        if gigachat:
            ca = env.get("WS_GIGACHAT_CA_BUNDLE", "") if target.name == "project_insight" else ""
            self.log("GigaChat: проверка TLS, OAuth и списка моделей")
            job = {"url": "https://ngw.devices.sberbank.ru:9443/api/v2/oauth", "headers": {"Authorization": "Basic " + gigachat, "RqUID": str(uuid.uuid4()), "Content-Type": "application/x-www-form-urlencoded"}, "method": "POST", "body": urllib.parse.urlencode({"scope": env.get("WS_GIGACHAT_SCOPE", "GIGACHAT_API_PERS")}), "engine": engine, "ca_file": ca, "timeout": 40}
            response = target.call({"operation": "oauth", "job": job}, 60)
            access = response.pop("private_access_value", "")
            if access:
                self.redactor.extend({"gigachat_access_token": access})
            self.report["probes"].append(self.redactor.clean({"environment": target.name, "endpoint": "gigachat_oauth", "source": "gigachat", "variant": "configured_ca", **response}))
            if access:
                configured_base = env.get("GIGACHAT_BASE_URL", "https://gigachat.devices.sberbank.ru/api/v1").rstrip("/")
                if urllib.parse.urlsplit(configured_base).hostname not in {"gigachat.devices.sberbank.ru", "api.giga.chat"}:
                    self.note("GigaChat: нестандартный base URL SDK; Bearer-токен туда не отправлен")
                    configured_base = "https://api.giga.chat/v1"
                endpoint = Endpoint("gigachat_models", "gigachat", configured_base + "/models", "models", {"Authorization": "Bearer " + access}, credentials_present=True)
                configured_result = self.probe(target, endpoint, "sdk_configured_base", ca_file=ca)
                alternative_base = "https://api.giga.chat/v1" if "gigachat.devices.sberbank.ru" in configured_base else "https://gigachat.devices.sberbank.ru/api/v1"
                alternative = copy.deepcopy(endpoint)
                alternative.name = "gigachat_models_alternative_domain"
                alternative.url = alternative_base + "/models"
                alternative_result = self.probe(target, alternative, "official_domain_migration_control", ca_file=ca)
                selected_base = configured_base
                if (configured_result.get("status") != 200 or not configured_result.get("valid")) and alternative_result.get("status") == 200 and alternative_result.get("valid"):
                    selected_base = alternative_base
                    self.note("GigaChat: models работает на альтернативном официальном домене; короткая генерация будет проверена там. Это не меняет конфигурацию проекта.")
                if not self.args.skip_llm_generation:
                    endpoint.name, endpoint.url, endpoint.expected, endpoint.method = "gigachat_chat", selected_base + "/chat/completions", "chat", "POST"
                    endpoint.headers["Content-Type"] = "application/json"
                    endpoint.body = json.dumps({"model": env.get("WS_GIGACHAT_MODEL", "GigaChat-2-Max"), "messages": [{"role": "user", "content": "Reply OK."}], "max_tokens": 8, "temperature": 0.1})
                    self.probe(target, endpoint, "tiny_generation", ca_file=ca, timeout=90)
        else:
            self.note("GigaChat: ключ отсутствует; OAuth и генерация не проверены")
        yandex_key, folder = env.get("WS_YANDEX_API_KEY", ""), env.get("WS_YANDEX_FOLDER_ID", "")
        if yandex_key and folder:
            base = env.get("WS_YANDEX_BASE_URL", "https://llm.api.cloud.yandex.net/v1").rstrip("/")
            if urllib.parse.urlsplit(base).hostname != "llm.api.cloud.yandex.net":
                self.note("Yandex: нестандартный API host; ключ не отправлен, требуется отдельная проверка настройки")
            elif not self.args.skip_llm_generation:
                endpoint = Endpoint("yandexgpt_chat", "yandexgpt", base + "/chat/completions", "chat", {"Authorization": "Api-Key " + yandex_key, "Content-Type": "application/json"}, "POST", json.dumps({"model": f"gpt://{folder}/{env.get('WS_YANDEX_MODEL','yandexgpt/latest')}", "messages": [{"role": "user", "content": "Reply OK."}], "max_tokens": 8, "temperature": 0.1}), True)
                self.probe(target, endpoint, "tiny_generation", timeout=90)
        else:
            self.note("YandexGPT: ключ или folder id отсутствует; авторизованная генерация не проверена")
        if env.get("WS_LLM_LOCAL_ENABLED", "").lower() in {"1", "true", "yes"}:
            base = env.get("WS_LOCAL_LLM_BASE_URL", "http://local-llm:8000/v1").rstrip("/")
            self.probe(target, Endpoint("local_llm_models", "local_llm", base + "/models", "models"), "configured_local_service", allow_local=True)

    def finalize(self, interrupted: bool = False) -> None:
        self.report["finished_at"] = utc_now()
        self.report["interrupted"] = interrupted
        self.report["request_counts"] = self.request_counts
        self.report["throttle_counts"] = self.throttle_counts
        probes = self.report["probes"]
        self.report["totals"] = {"records": len(probes), "http_200_valid": sum(p.get("status") == 200 and p.get("valid", False) for p in probes), "throttled": sum(rate_limited(p) for p in probes), "skipped": sum(bool(p.get("skipped")) for p in probes)}
        findings = []
        for endpoint in sorted({p.get("endpoint", "unknown") for p in probes}):
            rows = [p for p in probes if p.get("endpoint") == endpoint and not p.get("skipped")]
            if not rows:
                continue
            success = [p for p in rows if p.get("valid") and p.get("status") in {200, 304}]
            limited = [p for p in rows if rate_limited(p)]
            if limited:
                findings.append(f"{endpoint}: ограничение подтверждено в {len(limited)} запросах; внутренний алгоритм квоты по одному HTTP-коду не определяется.")
            bad_payload = [p for p in rows if p.get("status") == 200 and not p.get("valid")]
            if bad_payload:
                findings.append(f"{endpoint}: получен HTTP 200 с неподходящим содержимым; это не успешный сбор данных.")
            baseline_ok = {p["environment"] for p in success if p.get("variant") in {"baseline", "repeat_reverse_order"}}
            baseline_bad = {p["environment"] for p in rows if p.get("variant") in {"baseline", "repeat_reverse_order"} and (not p.get("valid") or p.get("status") != 200)}
            if baseline_ok and baseline_bad:
                findings.append(f"{endpoint}: успешные среды {', '.join(sorted(baseline_ok))}; среды с отказами {', '.join(sorted(baseline_bad))}. Сопоставь время, IP, TLS и повторный проход: связь с ОС пока не равна доказанной причине.")
            if any(p.get("status") == 401 for p in rows):
                findings.append(f"{endpoint}: 401 подтверждает отказ авторизации; действительность ключа и права требуют отдельной проверки.")
            if any(p.get("root") == "rdf" for p in rows):
                findings.append(f"{endpoint}: валидная RSS 1.0/RDF; текущий RSS-парсер проекта этот формат не поддерживает.")
        self.report["findings"] = findings
        self.checkpoint()
        lines = ["# Диагностика weak-signals API", "", f"Начало: {self.report['started_at']}. Завершение: {self.report['finished_at']}.", "", "Это наблюдения с конкретного компьютера и IP. 200 не доказывает постоянную доступность; 429 не раскрывает внутреннюю причину квоты. Сравнение ОС не изолирует OpenSSL от остальных различий образов.", "", "## Покрытие", "", "| Среда | Python | TLS |", "|---|---|---|"]
        for name, info in self.report["environments"].items():
            lines.append(f"| {name} | {info.get('python','не запущена')} | {info.get('openssl','не проверено')} |")
        lines += ["", "## Выводы по наблюдениям", ""]
        lines += ["- " + finding for finding in findings] or ["Нет достаточных наблюдений для автоматического вывода."]
        lines += ["", "## Результаты", "", "| Среда | Ресурс | Вариант | HTTP | Проверка содержимого / причина пропуска |", "|---|---|---|---:|---|"]
        for p in probes:
            detail = str(p.get("skipped") or p.get("validation") or p.get("error_type") or p.get("error") or "см. JSON").replace("|", "/").replace("\n", " ")
            lines.append(f"| {p.get('environment')} | {p.get('endpoint')} | {p.get('variant','baseline')} | {p.get('status',0)} | {detail} |")
        lines += ["", "## Как интерпретировать", "", "- 429 / GitHub 403 с rate-limit: сервер ограничил запрос. Общий IP, ключ, окно квоты и нагрузку провайдера различает только совокупность данных или поддержка провайдера.", "- CERTIFICATE_VERIFY_FAILED: цепочка доверия/CA/перехват TLS. Проверка сертификатов во всех тестах включена.", "- Ошибка IPv6 при успешном IPv4: проблема именно доступного IPv6-маршрута либо DNS; не доказательство блокировки API.", "- Повторяемая разница Bookworm/Trixie: зависимость от окружения. Один HTTP 406 не доказывает ошибку TLS-рукопожатия: HTTP уже получен.", "- 200 + INVALID_XML / NOT_A_FEED / UNEXPECTED_PAYLOAD: транспорт работает, но адаптеру пришёл неподходящий ответ.", "- SKIPPED: проверка не выполнена, это не отказ сервиса.", "- Tiny generation выполняется только с найденными ключами; списание небольшого числа токенов возможно.", "", "## Замечания и непроверенные условия", ""]
        lines += ["- " + str(note).replace("\n", " ") for note in self.report["notes"]]
        lines += ["", "## Что отправить для разбора", "", "Прикрепи api_diagnostics_latest.zip. Внутри только очищенные JSON, Markdown и лог этого запуска. .env, исходники и Docker inspect в архив не включаются. IP-адреса, версии, имена сервисов и публичные URL сохранены как диагностические данные.", ""]
        self.base.with_suffix(".md").write_text(self.redactor.text("\n".join(lines)), encoding="utf-8")
        self.log("Готово: " + str(self.base.with_suffix(".json").name))
        self.log("Архив для отправки: api_diagnostics_latest.zip")
        self.log_file.flush()
        path = self.root / "api_diagnostics_latest.zip"
        temporary = path.with_suffix(".tmp.zip")
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
            for suffix in (".json", ".md", ".log"):
                item = self.base.with_suffix(suffix)
                archive.write(item, item.name)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)

    def run(self) -> int:
        interrupted = False
        exit_code = 0
        try:
            self.setup()
            endpoints, feeds = endpoint_inventory(self.environment, self.root)
            self.report["feed_inventory"] = feeds
            self.report["endpoint_inventory"] = [{"name": e.name, "source": e.source, "url": safe_url(e.url), "state": e.state, "credentials_present": e.credentials_present} for e in endpoints]
            if self.args.plan:
                self.note("PLAN: сетевые запросы, сборка образов и остановка сервисов не выполнялись")
                return 0
            images = self.build_images()
            self.isolate()
            targets = []
            host = self.add_worker("host", env=dict(os.environ))
            if host:
                targets.append(host)
            collector = next((c for c in self.containers if c["Config"].get("Labels", {}).get("com.docker.compose.service") == "collector"), None)
            if collector:
                real = self.add_worker("project_collector", collector["Image"], self.service_env.get("collector", self.environment), collector)
                if real:
                    targets.append(real)
            for distro, image in images.items():
                target = self.add_worker(distro, image)
                if target:
                    targets.append(target)
            for target in targets:
                # No location service or geolocation dependency; this only records observed egress.
                egress = Endpoint("egress_ip", "network_metadata", "https://api.ipify.org?format=json", "egress")
                self.probe(target, egress, "observed_egress", engine="curl" if target.info.get("curl") else "urllib")
            if host:
                self.probe(host, Endpoint("semantic_official_status", "statuspage", "https://status.api.semanticscholar.org/api/v2/summary.json", "statuspage"), "official_status_page")
                self.probe(host, Endpoint("arxiv_official_status", "statuspage", "https://status.arxiv.org/", "text"), "official_status_page")
                origins = list(dict.fromkeys(urllib.parse.urlunsplit((urllib.parse.urlsplit(e.url).scheme, urllib.parse.urlsplit(e.url).netloc, "", "", "")) for e in endpoints if e.source == "rss"))
                for index, origin in enumerate(origins, 1):
                    self.probe(host, Endpoint(f"rss_robots_{index:02d}", "rss", origin + "/robots.txt", "robots"), "robots_policy_hints")
            if self.args.quick:
                endpoints = [e for e in endpoints if e.name in {"arxiv", "gdelt_recent", "semantic_bulk", "openalex", "github_search", "zenodo", "hackernews"} or e.state == "CONFIGURED"]
                for e in endpoints:
                    for target in targets:
                        self.probe(target, e)
            else:
                self.matrix(endpoints, targets)
                # Network control changes network only, keeping the same Bookworm image.
                if images.get("bookworm") and self.network != "bridge":
                    bridge = self.add_worker("bookworm_default_bridge", images["bookworm"], network="bridge")
                    if bridge:
                        for e in endpoints:
                            if e.name in {"arxiv", "gdelt_recent", "semantic_bulk"}:
                                self.probe(bridge, e, "network_control")
                self.huggingface_runtime_checks()
                self.llm_checks(host)
            if not targets:
                raise RuntimeError("Ни одна среда не доступна")
        except KeyboardInterrupt:
            interrupted, exit_code = True, 130
            self.note("Прервано пользователем; сохранение частичного отчёта и восстановление сервисов")
        except Exception as error:
            interrupted, exit_code = True, 1
            self.note(f"Ошибка диагностики: {type(error).__name__}: {error}")
        finally:
            for target in self.workers:
                try:
                    target.close()
                except Exception as error:
                    self.note("Очистка диагностического контейнера: " + type(error).__name__)
            self.restore()
            self.finalize(interrupted)
            self.log_file.close()
        return exit_code


def restore_only(root: pathlib.Path) -> int:
    lock = root / LOCK_NAME
    if lock.exists():
        try:
            active_pid = int(lock.read_text())
            os.kill(active_pid, 0)
        except (ValueError, ProcessLookupError):
            pass
        else:
            print("Диагностика ещё работает. Дождись её завершения или останови Ctrl+C; параллельное восстановление запрещено.")
            return 2
    path = root / RECOVERY_NAME
    if not path.exists():
        print("Восстанавливать нечего: файл восстановления отсутствует.")
        return 0
    data = json.loads(path.read_text())
    failed = []
    for ident in data.get("containers", []):
        if not re.fullmatch(r"[a-f0-9]{12,64}", ident):
            raise ValueError("Invalid recovery container id")
        check = run_command(["docker", "inspect", ident])
        if check.returncode:
            failed.append(ident)
            continue
        container = json.loads(check.stdout)[0]
        labels = container.get("Config", {}).get("Labels", {})
        if labels.get("com.docker.compose.project") != data.get("project") or labels.get("com.docker.compose.service") not in {"collector", "insight"}:
            raise ValueError("Recovery project/service mismatch")
        result = run_command(["docker", "start", ident], 35)
        print(f"Восстановление {labels['com.docker.compose.service']}: {'OK' if result.returncode == 0 else 'ERROR'}")
        if result.returncode:
            failed.append(ident)
    if failed:
        data["containers"] = failed
        atomic_json(path, data)
        return 1
    path.unlink()
    return 0


class UnitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.seen: list[dict[str, str]] = []
        seen = cls.seen
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                pass
            def do_GET(self) -> None:
                seen.append({"path": self.path, "authorization": self.headers.get("Authorization", "")})
                if self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", f"http://localhost:{self.server.server_port}/feed")
                    self.end_headers()
                    return
                if self.path == "/throttle":
                    self.send_response(429)
                    self.send_header("Retry-After", "200")
                    self.end_headers()
                    self.wfile.write(b'{"error":"too many requests"}')
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/rss+xml")
                self.send_header("ETag", '"test"')
                self.end_headers()
                self.wfile.write(b"<rss><channel><item><title>OK</title></item></channel></rss>")
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def local_job(self, path: str) -> dict[str, Any]:
        return {"url": f"http://127.0.0.1:{self.server.server_port}{path}", "allow_local": True, "trust_env": False, "expected": "rss", "redirect_gap": 0, "timeout": 3}

    def test_http_fixture(self) -> None:
        result = request(self.local_job("/feed"))
        self.assertEqual(result["status"], 200)
        self.assertEqual(result["entries"], 1)

    def test_http_throttle_is_not_retried(self) -> None:
        before = len(self.seen)
        result = request(self.local_job("/throttle"))
        self.assertEqual(result["status"], 429)
        self.assertEqual(retry_delay(result["headers"]), 200)
        self.assertEqual(len(self.seen)-before, 1)

    def test_redirect_removes_authorization(self) -> None:
        job = self.local_job("/redirect")
        job["headers"] = {"Authorization": "Bearer private-value"}
        result = request(job)
        self.assertTrue(result["valid"])
        self.assertEqual(len(result["hops"]), 2)
        self.assertEqual(self.seen[-1]["authorization"], "")

    def test_curl_fixture(self) -> None:
        if not shutil.which("curl"):
            self.skipTest("curl is unavailable")
        job = self.local_job("/feed")
        job["engine"] = "curl"
        result = request(job)
        self.assertEqual(result["status"], 200)
        self.assertTrue(result["valid"])

    def test_httpx_fixture(self) -> None:
        try:
            import httpx  # noqa: F401
        except ImportError:
            self.skipTest("httpx is unavailable on the host; container build installs it")
        job = self.local_job("/feed")
        job["engine"] = "httpx"
        result = request(job)
        self.assertEqual(result["status"], 200)
        self.assertTrue(result["valid"])

    def test_remote_proxy_dns(self) -> None:
        # Failure of local DNS does not prove failure of remote proxy DNS.
        with mock.patch(__name__ + ".public_target", side_effect=socket.gaierror("local lookup failed")), mock.patch.object(urllib.request, "getproxies", return_value={"https": "http://proxy.test:8080"}), mock.patch.object(urllib.request, "proxy_bypass", return_value=False), mock.patch(__name__ + ".perform_one", return_value=(200, {}, b'{"data":[]}', {})) as transport:
            result = request({"url": "https://api.test/example", "expected": "semantic"})
        self.assertTrue(result["valid"])
        self.assertEqual(transport.call_count, 1)
        self.assertIn("local_dns_error", result)

    def test_worker_protocol(self) -> None:
        target = Worker("selftest", [sys.executable, "-B", str(pathlib.Path(__file__).resolve()), "--worker"], {}, lambda x: None)
        try:
            result = target.call(self.local_job("/feed"), 10)
            self.assertTrue(result["valid"])
        finally:
            target.close()

    def test_url_redaction(self) -> None:
        self.assertNotIn("secret", safe_url("https://a.test/x?api_key=secret&q=robotics"))

    def test_sensitive_json(self) -> None:
        redactor = Redactor({"WS_API_KEY": "very secret value"})
        result = json.dumps(redactor.clean({"error": "very secret value", "authorization": "token"}))
        self.assertNotIn("very secret", result)
        self.assertNotIn('"token"', result)

    def test_retry_date(self) -> None:
        self.assertEqual(retry_delay({"retry-after": "Wed, 07 Oct 2026 12:00:00 GMT"}, dt.datetime(2026,10,7,11,59,tzinfo=dt.timezone.utc).timestamp()), 60)

    def test_retry_seconds(self) -> None:
        self.assertEqual(retry_delay({"retry-after": "240"}), 240)
        self.assertIsNone(retry_delay({"retry-after": "nonsense"}))

    def test_feed_not_html(self) -> None:
        self.assertFalse(inspect_body(b"<html><body>challenge</body></html>", "rss", 200, False)["valid"])

    def test_feed_rdf(self) -> None:
        result = inspect_body(b'<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"><item/></rdf:RDF>', "rss", 200, False)
        self.assertTrue(result["valid"])
        self.assertIn("project_parser_warning", result)

    def test_xml_dtd(self) -> None:
        self.assertFalse(inspect_body(b'<!DOCTYPE rss [<!ENTITY x "text">]><rss/>', "rss", 200, False)["valid"])

    def test_schema_not_just_json(self) -> None:
        self.assertFalse(inspect_body(b'{"message":"failed"}', "semantic", 200, False)["valid"])
        self.assertTrue(inspect_body(b'{"data":[]}', "semantic", 200, False)["valid"])

    def test_rate_403(self) -> None:
        self.assertTrue(rate_limited({"status":403,"headers":{"x-ratelimit-remaining":"0"}}))
        self.assertFalse(rate_limited({"status":403,"headers":{}}))

    def test_dotenv_no_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp)/".env"
            path.write_text('A="x=y"\nB=$(touch nope)\nC=value # comment\n')
            values, notes = read_dotenv(path)
            self.assertEqual(values["A"], "x=y")
            self.assertEqual(values["B"], "$(touch nope)")
            self.assertEqual(values["C"], "value")
            self.assertTrue(notes)

    def test_feed_list(self) -> None:
        self.assertEqual(len(split_feeds('["https://a.test/rss", "https://b.test/feed"]')), 2)

    def test_restore_blocks_active_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / LOCK_NAME).write_text(str(os.getpid()))
            with mock.patch("builtins.print"):
                self.assertEqual(restore_only(root), 2)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--self-test", action="store_true", help="Локальные проверки диагностического кода, без сети")
    parser.add_argument("--plan", action="store_true", help="Инвентаризация и план без запросов к внешним API")
    parser.add_argument("--isolate", action="store_true", help="Временно остановить работающие collector/insight текущего Compose-проекта и восстановить их")
    parser.add_argument("--restore", action="store_true", help="Восстановить сервисы после аварийного завершения")
    parser.add_argument("--no-docker", action="store_true", help="Только хост")
    parser.add_argument("--no-build", action="store_true", help="Без сборки контрольных образов")
    parser.add_argument("--quick", action="store_true", help="Только базовые проверки основных источников")
    parser.add_argument("--skip-llm-generation", action="store_true", help="Не расходовать токены LLM на маленькую проверочную генерацию")
    args = parser.parse_args()
    if args.worker:
        worker()
        return 0
    if args.self_test:
        result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(UnitTests))
        return 0 if result.wasSuccessful() else 1
    root = pathlib.Path.cwd()
    if args.restore:
        return restore_only(root)
    if not (root/"compose.yaml").is_file() or not (root/"services/collector").is_dir():
        parser.error("Запусти из корня weak-signals: здесь должны быть compose.yaml и services/collector")
    if (root/RECOVERY_NAME).exists():
        print("Найдён незавершённый запуск. Сначала: python3 weak_signals_api_doctor.py --restore")
        return 2
    lock = root/LOCK_NAME
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        try:
            pid = int(lock.read_text())
            os.kill(pid, 0)
        except (ValueError, ProcessLookupError):
            lock.unlink(missing_ok=True)
            return main()
        print("Другой запуск диагностики уже активен; параллельный тест запрещён.")
        return 2
    with os.fdopen(fd, "w") as output:
        output.write(str(os.getpid()))
    def interrupted(signum: int, frame: Any) -> None:
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupted)
    try:
        return Doctor(args).run()
    finally:
        lock.unlink(missing_ok=True)


if __name__ == "__main__":
    if sys.version_info < (3, 10):
        print("Требуется Python 3.10 или новее.", file=sys.stderr)
        sys.exit(2)
    sys.exit(main())
