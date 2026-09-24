#!/usr/bin/env python3
"""Статическая сверка интерфейса с контрактом и требованиями ТЗ.

Собрать фронтенд в среде без доступа к реестру npm нельзя, поэтому проверяется то, что можно
проверить по исходникам: все пути OpenAPI вызываются клиентом, импорты между модулями
разрешаются, обязательные поля источника выводятся на экран, HTML не вставляется в обход React,
а тексты интерфейса — русские.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
UI_SRC = ROOT / "services" / "ui" / "src"
OPENAPI = ROOT / "docs" / "api" / "orchestrator.openapi.yaml"
CLIENT = UI_SRC / "api" / "client.ts"
SOURCES_COMPONENT = UI_SRC / "components" / "signals.tsx"

# Поля источника, которые ТЗ требует показывать пользователю (§2, «Требования к источникам»).
REQUIRED_SOURCE_FIELDS = (
    "title",
    "url",
    "published_at",
    "source_type",
    "language_code",
    "trust_level",
    "summary_ru",
    "summary_kind",
)
# Разделы страницы инсайта, которых требует ТЗ (§2, «просмотр инсайта»).
REQUIRED_INSIGHT_SECTIONS = (
    "Описание технологии",
    "Потенциальное преимущество",
    "Кейс-пример",
    "Оценки в аналитических отчётах",
    "Почему это слабый сигнал",
    "Источники",
    "Происхождение",
)
# Обязательные элементы страницы результатов.
REQUIRED_RESULT_ELEMENTS = ("Обработано источников", "Кандидатов на слабый сигнал", "выше 75")
IMPORT_RE = re.compile(r"""^\s*(?:import|export)\s[^;]*?from\s+["'](\.[^"']+)["']""", re.M)
IMPORT_STATEMENT_RE = re.compile(r"""^\s*import\s+(?!type\s+["'])(.+?)\s+from\s+["'][^"']+["'];""", re.M | re.S)
BINDING_RE = re.compile(r"[A-Za-z_$][\w$]*")
JSX_TEXT_RE = re.compile(r">\s*([А-Яа-яЁё][^<>{}]{3,})<")
LATIN_SENTENCE_RE = re.compile(r">\s*([A-Z][a-z]+(?:\s+[a-z]+){2,})\s*<")


def load_openapi_paths() -> list[str]:
    """Пути HTTP API из нормативной спецификации."""
    spec = yaml.safe_load(OPENAPI.read_text(encoding="utf-8"))
    return [path for path in spec["paths"] if path.startswith("/api/")]


def check_api_coverage(problems: list[str]) -> int:
    """Каждый путь контракта должен вызываться клиентом."""
    client = CLIENT.read_text(encoding="utf-8")
    checked = 0
    for path in load_openapi_paths():
        checked += 1
        fragments = [part for part in re.split(r"\{[a-z_]+\}", path) if part not in ("", "/")]
        if not all(fragment in client for fragment in fragments):
            problems.append(f"путь {path} контракта не вызывается в api/client.ts")
    return checked


def check_imports(problems: list[str]) -> int:
    """Относительные импорты должны указывать на существующие файлы."""
    checked = 0
    for source in sorted(UI_SRC.rglob("*.ts*")):
        for target in IMPORT_RE.findall(source.read_text(encoding="utf-8")):
            checked += 1
            base = (source.parent / target).resolve()
            candidates = [base, *(base.with_suffix(suffix) for suffix in (".ts", ".tsx", ".css"))]
            candidates.append(base / "index.ts")
            candidates.append(base / "index.tsx")
            if not any(candidate.is_file() for candidate in candidates):
                problems.append(f"{source.name}: импорт {target} не разрешается в файл")
    return checked


def check_unused_imports(problems: list[str]) -> int:
    """Неиспользуемый импорт роняет сборку: `tsc` со `strict` считает это ошибкой TS6133."""
    checked = 0
    for source in sorted(UI_SRC.rglob("*.ts*")):
        text = source.read_text(encoding="utf-8")
        body = IMPORT_STATEMENT_RE.sub("", text)
        for clause in IMPORT_STATEMENT_RE.findall(text):
            clause = clause.replace("type ", " ")
            names: list[str] = []
            if "{" in clause:
                inside = clause[clause.index("{") + 1 : clause.rindex("}")]
                for entry in inside.split(","):
                    parts = entry.strip().split()
                    if parts:
                        names.append(parts[-1])
                clause = clause[: clause.index("{")]
            for part in clause.split(","):
                part = part.strip().removeprefix("* as ").strip()
                if part and part not in {"*"}:
                    names.append(part)
            for name in names:
                if not name or not BINDING_RE.fullmatch(name):
                    continue
                checked += 1
                if not re.search(rf"\b{re.escape(name)}\b", body):
                    problems.append(f"{source.name}: импорт {name} не используется (ошибка сборки TS6133)")
    return checked


def check_balanced(problems: list[str]) -> int:
    """Грубая проверка сбалансированности скобок в каждом модуле."""
    pairs = {")": "(", "]": "[", "}": "{"}
    checked = 0
    for source in sorted(UI_SRC.rglob("*.ts*")):
        checked += 1
        text = source.read_text(encoding="utf-8")
        # порядок важен: сначала строки и регулярные литералы, потом комментарии —
        # иначе `//` внутри регулярного выражения съедает остаток строки кода
        text = re.sub(r"\"(?:\\.|[^\"])*\"|'(?:\\.|[^'])*'|`(?:\\.|[^`])*`", '""', text, flags=re.S)
        # `(?![>/*\s])` не даёт принять закрывающий JSX-тег ` />` за регулярное выражение
        text = re.sub(
            r"(?<=[(,=:\s])/(?![>/*\s])(?:\\.|\[[^\]]*\]|[^/\\\n])+/[gimsuy]*", "RE", text
        )
        # блочные комментарии могут занимать несколько строк, строчные — строго до конца строки
        text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
        text = re.sub(r"//[^\n]*", "", text)
        stack: list[str] = []
        for char in text:
            if char in "([{":
                stack.append(char)
            elif char in pairs:
                if not stack or stack.pop() != pairs[char]:
                    problems.append(f"{source.name}: несбалансированная скобка {char}")
                    break
        else:
            if stack:
                problems.append(f"{source.name}: незакрытых скобок: {len(stack)}")
    return checked


def check_source_fields(problems: list[str]) -> None:
    """Все обязательные поля источника должны выводиться компонентом списка источников."""
    text = SOURCES_COMPONENT.read_text(encoding="utf-8")
    for field in REQUIRED_SOURCE_FIELDS:
        if not re.search(rf"source\.{field}\b", text):
            problems.append(f"поле источника {field} не выводится в components/signals.tsx")


def check_insight_sections(problems: list[str]) -> None:
    """Страница инсайта обязана содержать все разделы документа-отчёта."""
    text = (UI_SRC / "pages" / "InsightPage.tsx").read_text(encoding="utf-8")
    for section in REQUIRED_INSIGHT_SECTIONS:
        if section not in text:
            problems.append(f"на странице инсайта нет раздела «{section}»")
    report = (UI_SRC / "lib" / "report.ts").read_text(encoding="utf-8")
    for section in REQUIRED_INSIGHT_SECTIONS:
        if section not in report:
            problems.append(f"в отчёте Markdown нет раздела «{section}»")


def check_result_elements(problems: list[str]) -> None:
    """Страница результатов обязана показывать счётчики, требуемые ТЗ."""
    text = (UI_SRC / "components" / "signals.tsx").read_text(encoding="utf-8")
    for element in REQUIRED_RESULT_ELEMENTS:
        if element not in text:
            problems.append(f"на странице результатов нет показателя «{element}»")


def check_no_raw_html(problems: list[str]) -> None:
    """Данные никогда не вставляются в обход экранирования React (§17 HANDOFF_UI)."""
    for source in sorted(UI_SRC.rglob("*.ts*")):
        text = source.read_text(encoding="utf-8")
        if "dangerouslySetInnerHTML" in text or "innerHTML" in text:
            problems.append(f"{source.name}: используется вставка сырого HTML")


def check_russian_ui(problems: list[str]) -> int:
    """Тексты интерфейса на русском: латинские фразы в JSX недопустимы (§20 HANDOFF_UI)."""
    checked = 0
    for source in sorted((UI_SRC / "pages").rglob("*.tsx")) + sorted((UI_SRC / "components").rglob("*.tsx")):
        text = source.read_text(encoding="utf-8")
        checked += len(JSX_TEXT_RE.findall(text))
        for phrase in LATIN_SENTENCE_RE.findall(text):
            problems.append(f"{source.name}: латинская фраза в интерфейсе — «{phrase}»")
    return checked


def main() -> int:
    """Запускает проверки и печатает итог."""
    problems: list[str] = []
    paths = check_api_coverage(problems)
    imports = check_imports(problems)
    bindings = check_unused_imports(problems)
    modules = check_balanced(problems)
    check_source_fields(problems)
    check_insight_sections(problems)
    check_result_elements(problems)
    check_no_raw_html(problems)
    russian = check_russian_ui(problems)
    print(
        f"ui: путей контракта {paths}, импортов {imports}, импортированных имён {bindings}, "
        f"модулей {modules}, русских строк интерфейса {russian}"
    )
    for problem in problems:
        print(f"ОШИБКА: {problem}")
    print(f"RESULT: {'PASS' if not problems else f'FAIL ({len(problems)})'}")
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
