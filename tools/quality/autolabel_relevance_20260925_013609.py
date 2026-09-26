#!/usr/bin/env python3
"""Авторазметка relevance-20260925-013609.csv: выдача трёх holdout-тем B v1, 16 карточек.

Метки — silver-разметка Claude по рубрике ml/data/dataset_b/LABELING_PROTOCOL.md. Основа: название
и описание карточки, названия, типы и даты её источников (own-topics-20260925-013609.json) и silver-метки
тех же групп в labels_silver_v1.jsonl. Это не экспертная разметка: точность по этим меткам —
предварительная оценка, итоговую дадут два эксперта по слепым листам B v2.

Запуск из корня проекта: python3 tools/quality/autolabel_relevance_20260925_013609.py [--force]
Строки находятся по теме и рангу, название сверяется. Метки, которые уже стоят в файле и отличаются
от этих, без --force не перезаписываются. Перед первой записью сохраняется копия <файл>.orig;
разделитель, BOM и перевод строк файла сохраняются.
"""

from __future__ import annotations

import argparse
import csv
import io
import re
import shutil
import sys
from collections import Counter
from pathlib import Path

FILE = Path("relevance-20260925-013609.csv")
PREFIX = "авто-silver (Claude): "
QS = "квантовые сенсоры для навигации"
FT = "перспективные решения в финтехе"
DT = "цифровые двойники в энергетике"
# (тема, ранг, название карточки, код, причина)
LABELS = [
    (QS, 1, "Квантовые сенсоры на основе запутанности для навигации", "N-GEN",
     "оптомеханические сенсоры с запутанностью — общее направление квантовой сенсорики; навигация есть "
     "только в обзоре, в кластере посторонняя метаэвристика"),
    (QS, 2, "Технология: nanoscale magnetometry", "N-OFF",
     "наномасштабная NV-магнитометрия для физики конденсированного состояния (сверхпроводники при мегабарах) — "
     "соседняя область, навигация упомянута лишь как одно из применений (silver v1: U)"),
    (QS, 3, "Многопараметрическая квантовая метрология для навигационных приложений", "R",
     "многопараметрическая квантовая метрология на ярких солитонах прямо для альтернативной навигации — "
     "теоретические работы 2024 г. с датой"),
    (QS, 4, "Квантовые магнитометры для навигации в условиях недоступности GPS", "R",
     "квантово-обеспеченная магнитная навигация: полевые испытания на самолёте и на земле точнее "
     "стратегической ИНС (arXiv 2504.08167, апрель 2025) — ранний пилот по теме"),
    (FT, 1, "Применение машинного обучения для прогнозирования и оптимизации криптовалютных портфелей на базе Ethereum",
     "N-MAT", "прогноз цены Ethereum и портфели на LSTM/GRU/SVM/RL — массовая исследовательская тема, "
              "а не новая финтех-технология"),
    (FT, 2, "Искусственный интеллект для разведки угроз: автономные подходы", "N-OFF",
     "ИИ-разведка киберугроз и этика ИИ в здравоохранении — не финтех; все источники — обзоры"),
    (FT, 3, "Анализ взаимосвязей между криптовалютами и оптимизация инвестиционных портфелей", "N-OFF",
     "эконометрика связанности криптоактивов (TVP-VAR, минимизация эксцесса) — анализ рынков, "
     "а не финтех-решение"),
    (FT, 4, "Технология транзакций с использованием гомоморфного шифрования в финтехе", "U",
     "протокол EHT на гомоморфном шифровании описан только в самоопубликованных препринтах Zenodo 2026 г., "
     "остальные источники — другие технологии (та же оценка у группы с этими источниками в silver v1)"),
    (FT, 5, "Технология: Prospects of Fintech", "N-OVR",
     "тренды и перспективы финтеха — обзорные статьи без конкретной технологии"),
    (FT, 6, "Технология: Form of NFT", "N-NOI",
     "склейка разнородных текстов о NFT: искусство, оплата обучения, правовой статус музейных копий"),
    (FT, 7, "Использование GAN-сетей для обнаружения мошенничества с помощью синтетических транзакционных данных",
     "R", "генеративные модели (GAN) для синтетических транзакционных данных под обучение антифрод-моделей "
          "банков — бенчмарки и исследования 2024 г."),
    (FT, 8, "Распределённое совместное обучение (Decentralized Federated Learning)", "N-OVR",
     "три обзора по децентрализованному федеративному обучению и учебный репозиторий про обмен навыками — "
     "обзор, связь с финтехом не показана"),
    (FT, 9, "Применение больших языковых моделей (LLM) в финансовом секторе", "N-OVR",
     "LLM в финансах — обзор применений; в кластере GPT-4 Technical Report с чужой аннотацией "
     "и автономная ГИС"),
    (DT, 1, "Цифровой двойник LifeTwin-LFP-SOH для оценки состояния литиевых батарей", "U",
     "по теме только личный репозиторий GitHub (2026) с описанием SOH-двойника LFP-батарей; вторая статья — "
     "о цифровых двойниках личности, подтверждений мало"),
    (DT, 2, "Технология: Multifidelity digital twin", "N-OFF",
     "цифровые двойники животных и садков аквакультуры — не энергетика (silver v1: U)"),
    (DT, 3, "Цифровые двойники для прогнозирования нагрузки в энергетике", "R",
     "цифровые двойники для вероятностного прогноза нагрузки в солнечных smart grid и нагрузки потребителя — "
     "исследования 2023–2025 гг. по теме"),
]


def _norm(text: str) -> str:
    return re.sub(r"[^0-9a-zа-яё]+", " ", (text or "").lower()).strip()


def main() -> int:
    parser = argparse.ArgumentParser(description="авторазметка relevance-20260925-013609.csv")
    parser.add_argument("--force", action="store_true", help="перезаписать уже стоящие другие метки")
    args = parser.parse_args()
    if not FILE.is_file():
        print(f"СТОП: нет файла {FILE} — запустите из корня проекта")
        return 1
    raw = FILE.read_bytes()
    bom = raw.startswith(b"\xef\xbb\xbf")
    lines = raw.decode("utf-8-sig").splitlines(keepends=True)
    head = next((i for i, line in enumerate(lines[:5]) if "label" in line.lower()), None)
    if head is None:
        print("СТОП: в первых строках файла нет заголовка с колонкой label")
        return 1
    delimiter = max((",", ";", "\t"), key=lines[head].count)
    newline = "\r\n" if lines[head].endswith("\r\n") else "\n"
    reader = csv.DictReader(io.StringIO("".join(lines[head:])), delimiter=delimiter)
    fields = list(reader.fieldnames or [])
    rows = []
    for row in reader:
        row.pop(None, None)
        if any((value or "").strip() for value in row.values()):
            rows.append(row)
    if not {"topic", "rank", "title", "label", "comment"} <= set(fields):
        print(f"СТОП: нет нужных колонок, в файле: {fields}")
        return 1
    if len(rows) != len(LABELS):
        print(f"СТОП: строк {len(rows)}, ожидалось {len(LABELS)} — это не тот файл")
        return 1
    index = {}
    for row in rows:
        try:
            rank = int(float((row["rank"] or "").replace(",", ".")))
        except ValueError:
            print(f"СТОП: неверный rank «{row['rank']}»")
            return 1
        index[(_norm(row["topic"]), rank)] = row
    conflicts = []
    for topic, rank, title, code, _reason in LABELS:
        row = index.get((_norm(topic), rank))
        if row is None or _norm(row["title"]) != _norm(title):
            print(f"СТОП: не найдена строка «{topic}» #{rank} «{title}» — это не тот файл")
            return 1
        current = (row["label"] or "").strip()
        if current and current.upper() != code and not args.force:
            conflicts.append(f"{topic} #{rank}: в файле «{current}», авторазметка {code}")
    if conflicts:
        print("СТОП: в файле уже есть другие метки, ничего не изменено (перезаписать: --force)")
        print("\n".join(conflicts))
        return 1
    changed = 0
    for topic, rank, _title, code, reason in LABELS:
        row = index[(_norm(topic), rank)]
        comment = PREFIX + reason
        if row["label"] != code or row["comment"] != comment:
            row["label"], row["comment"] = code, comment
            changed += 1
    counts = dict(sorted(Counter(code for *_, code, _reason in LABELS).items()))
    if not changed:
        print(f"изменений нет: {FILE} уже размечен этими метками {counts}")
        return 0
    backup = FILE.with_name(FILE.name + ".orig")
    if not backup.exists():
        shutil.copy2(FILE, backup)
    out = io.StringIO()
    out.write("".join(lines[:head]))
    writer = csv.DictWriter(out, fieldnames=fields, delimiter=delimiter, lineterminator=newline)
    writer.writeheader()
    writer.writerows(rows)
    FILE.write_bytes(("\ufeff" if bom else "").encode("utf-8") + out.getvalue().encode("utf-8"))
    print(f"РАЗМЕЧЕНО: {FILE}, строк изменено {changed} из {len(LABELS)}, метки {counts}; копия исходника: {backup}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
