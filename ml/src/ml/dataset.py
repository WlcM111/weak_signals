"""Обучающий набор: чтение датасета организаторов, очистка от утечки, сборка и валидация строк.

Позитивы берутся из нормализованного CSV датасета методологов (100 строк), негативы — из файлов
командной разметки. Итог — `data/labels/dataset_ds-<дата>-v<N>.jsonl` по `training_row.schema.json`.
"""

from __future__ import annotations

import csv
import json
import re
import unicodedata
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml

DOMAIN_MAP = {
    "индустриальный ии": "industrial_ai",
    "роботы": "robotics",
    "инфраструктура ии": "ai_infrastructure",
    "финтех": "fintech",
    "защита ии": "ai_security",
    "edge": "edge",
}
STAGE_MAP = (
    ("массов", 5),  # массовое внедрение — зрелая стадия, а не «раннее внедрение» (4)
    ("раннее внедрение", 4),
    ("ранее внедрение", 4),
    ("пилот", 3),
    ("прототип", 2),
    ("poc", 2),
    ("поc", 2),
    ("исследован", 1),
    ("концепц", 1),
)
TREND_MAP = (("растёт быстро", 3), ("растет быстро", 3), ("растёт", 2), ("растет", 2), ("стабиль", 1))
MARKDOWN_LINK_RE = re.compile(r"\[[^\]]*\]\((https?://[^)\s]+)\)")
BARE_URL_RE = re.compile(r"https?://[^\s,)\]]+")
POSITIVE_ORIGIN = "gpb_dataset_2026_09"
NEGATIVE_ORIGIN = "team_negatives_v1"
MAX_DESCRIPTION = 4000
MIN_TITLE = 5


@dataclass(slots=True)
class TrainingRow:
    """Строка обучающего набора (`training_row.schema.json`)."""

    row_id: str
    group_id: str
    origin: str
    title: str
    description: str
    domain_tag: str
    label: int
    label_kind: str
    source_urls: list[str] = field(default_factory=list)
    annotators: list[str] = field(default_factory=list)
    stage_ordinal: int | None = None
    trend_ordinal: int | None = None
    score_original: int | None = None
    annotation_agreement: str | None = None

    def to_json(self) -> dict[str, Any]:
        """Словарь для записи в JSONL (без пустых необязательных полей не обойтись: схема их допускает)."""
        return asdict(self)

    @property
    def text(self) -> str:
        """Текст наблюдения для лексических и эмбеддинговых признаков."""
        return f"{self.title}. {self.description}".strip() if self.description else self.title


class DatasetError(RuntimeError):
    """Датасет не соответствует ожидаемому формату или схеме."""


def slugify(value: str) -> str:
    """Латинский слаг названия для `group_id` (кириллица транслитерируется по таблице)."""
    table = {
        "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z",
        "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
        "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch",
        "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
    }
    lowered = unicodedata.normalize("NFKC", value).lower()
    converted = "".join(table.get(char, char) for char in lowered)
    slug = re.sub(r"[^a-z0-9]+", "-", converted).strip("-")
    return slug[:80] or "row"


def load_leak_patterns(path: Path) -> tuple[list[re.Pattern[str]], list[str]]:
    """Читает `leak_patterns.yaml`: регулярные выражения очистки и маркерные токены."""
    payload = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    patterns = [re.compile(item, re.IGNORECASE | re.UNICODE) for item in payload.get("patterns", [])]
    tokens = [str(item).lower() for item in payload.get("marker_tokens", [])]
    return patterns, tokens


def clean_description(text: str, patterns: Sequence[re.Pattern[str]]) -> str:
    """Удаляет фразы-маркеры разметки и схлопывает пробелы (§6.1 HANDOFF)."""
    cleaned = text
    for pattern in patterns:
        cleaned = pattern.sub(" ", cleaned)
    cleaned = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", cleaned)  # markdown-ссылки → текст
    cleaned = re.sub(r"\s*[—–-]\s*$", "", cleaned.strip())
    return re.sub(r"\s{2,}", " ", cleaned).strip(" ,;—-")


def parse_sources(value: str) -> list[str]:
    """Извлекает URL из колонки «Источники» (markdown-ссылки и голые адреса)."""
    urls = MARKDOWN_LINK_RE.findall(value or "")
    if not urls:
        urls = BARE_URL_RE.findall(value or "")
    seen: list[str] = []
    for url in urls:
        cleaned = url.rstrip(".,;)")
        if cleaned not in seen:
            seen.append(cleaned)
    return seen


def ordinal_from(value: str, table: Sequence[tuple[str, int]]) -> int | None:
    """Порядковое значение по первому совпавшему маркеру таблицы."""
    lowered = (value or "").lower()
    best: int | None = None
    for marker, ordinal in table:
        if marker in lowered:
            best = ordinal if best is None else max(best, ordinal)
    return best


def read_positive_rows(csv_path: Path, leak_patterns_path: Path) -> list[TrainingRow]:
    """Строит позитивные строки из нормализованного CSV датасета организаторов."""
    patterns, _ = load_leak_patterns(leak_patterns_path)
    rows: list[TrainingRow] = []
    with Path(csv_path).open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {"n", "tech", "domain", "companies", "why", "stage", "trend", "score", "sources"}
        missing = required - set(reader.fieldnames or ())
        if missing:
            raise DatasetError(f"в датасете нет колонок: {', '.join(sorted(missing))}")
        for record in reader:
            number = int(record["n"])
            title = record["tech"].strip()
            # Колонка «Компании» в текст не добавляется: у негативов команды такой колонки нет, и
            # подстрока «Компании:» в одиночку отделяла классы с точностью 100 % (утечка конструкции).
            description = clean_description(record["why"].strip(), patterns)
            domain = DOMAIN_MAP.get(record["domain"].strip().lower(), "other")
            rows.append(
                TrainingRow(
                    row_id=f"pos-{number:04d}",
                    group_id=slugify(title),
                    origin=POSITIVE_ORIGIN,
                    title=title,
                    description=description[:MAX_DESCRIPTION],
                    domain_tag=domain,
                    label=1,
                    label_kind="WEAK_SIGNAL",
                    source_urls=parse_sources(record["sources"]),
                    annotators=["methodologists_gpb"],
                    stage_ordinal=_stage_ordinal(record),
                    trend_ordinal=_trend_ordinal(record),
                    score_original=_score(record),
                )
            )
    return rows


def _stage_ordinal(record: dict[str, str]) -> int | None:
    """Стадия 1..4: сначала нормализованная колонка, иначе разбор текста (при «A → B» берётся B)."""
    if (record.get("stage_b") or "").strip().isdigit():
        return int(record["stage_b"])
    text = record.get("stage", "")
    tail = text.split("→")[-1] if "→" in text else text
    return ordinal_from(tail, STAGE_MAP)


def _trend_ordinal(record: dict[str, str]) -> int | None:
    """Тренд 1..3 из нормализованной колонки или текста."""
    if (record.get("trend_b") or "").strip().isdigit():
        return int(record["trend_b"])
    return ordinal_from(record.get("trend", ""), TREND_MAP)


def _score(record: dict[str, str]) -> int | None:
    """Балл 3..7 из колонки «Балл»."""
    raw = (record.get("score") or "").strip()
    return int(raw) if raw.isdigit() else None


def read_jsonl(path: Path) -> list[TrainingRow]:
    """Читает строки набора из JSONL."""
    rows: list[TrainingRow] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        rows.append(TrainingRow(**payload))
    return rows


def write_jsonl(path: Path, rows: Iterable[TrainingRow]) -> int:
    """Записывает набор в JSONL; возвращает число строк."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row.to_json(), ensure_ascii=False) + "\n")
            count += 1
    return count


def validate_rows(rows: Sequence[TrainingRow], schema_path: Path) -> list[str]:
    """Проверяет строки по `training_row.schema.json`; возвращает список нарушений."""
    schema = json.loads(Path(schema_path).read_text(encoding="utf-8"))
    problems: list[str] = []
    seen_ids: set[str] = set()
    for row in rows:
        payload = {key: value for key, value in row.to_json().items() if value is not None or key in schema["required"]}
        problems.extend(
            f"{row.row_id}: {message}" for message in _validate_against(payload, schema)
        )
        if row.row_id in seen_ids:
            problems.append(f"{row.row_id}: дублирующийся row_id")
        seen_ids.add(row.row_id)
        if len(row.title) < MIN_TITLE:
            problems.append(f"{row.row_id}: слишком короткое название")
    return problems


def _validate_against(payload: dict[str, Any], schema: dict[str, Any]) -> list[str]:
    """Валидация одного объекта минимальным валидатором JSON Schema из `tools/minischema.py`."""
    import sys  # noqa: PLC0415 - импорт рядом с использованием

    tools = str(Path(__file__).resolve().parents[3] / "tools")
    if tools not in sys.path:
        sys.path.insert(0, tools)
    import minischema  # noqa: PLC0415

    return list(minischema.validate_root(payload, schema))


def _truncate_at_clause(text: str, limit: int) -> str:
    """Усечение текста до лимита по границе предложения или оборота."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    boundary = max(cut.rfind(". "), cut.rfind("; "), cut.rfind(", "))
    return (cut[:boundary] if boundary > limit * 0.5 else cut).strip(" ,;—-")


def align_text_lengths(rows: Sequence[TrainingRow]) -> list[TrainingRow]:
    """Выравнивает длину описаний классов: описания более «длинного» класса усекаются до медианы другого.

    Позитивы написаны методологами (медиана 266 символов), негативы — командой (179): длина текста
    в одиночку отделяла классы с точностью 0.99. Модель, обученная на таких текстах, учит длину и
    стиль автора, а не содержание, и не переносится на проверочный набор организаторов, где оба
    класса написаны одними людьми. Выравнивание убирает этот ложный признак.
    """
    positives = sorted(len(row.description) for row in rows if row.label == 1)
    negatives = sorted(len(row.description) for row in rows if row.label == 0)
    if not positives or not negatives:
        return list(rows)
    median_positive = positives[len(positives) // 2]
    median_negative = negatives[len(negatives) // 2]
    long_label = 1 if median_positive > median_negative else 0
    limit = min(median_positive, median_negative)
    return [
        replace(row, description=_truncate_at_clause(row.description, limit)) if row.label == long_label else row
        for row in rows
    ]


def split_by_label(rows: Sequence[TrainingRow]) -> tuple[list[TrainingRow], list[TrainingRow]]:
    """Разделяет набор на позитивы и негативы."""
    return [row for row in rows if row.label == 1], [row for row in rows if row.label == 0]


def iter_texts(rows: Sequence[TrainingRow]) -> Iterator[str]:
    """Тексты наблюдений в порядке строк."""
    for row in rows:
        yield row.text
