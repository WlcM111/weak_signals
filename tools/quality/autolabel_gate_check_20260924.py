"""Авторазметка ml/reports/quality/gate_check.csv: 81 карточка прогонов 22:42 (штатный) и 23:01 (constant).

Метки — silver-разметка Claude по рубрике ml/data/dataset_b/LABELING_PROTOCOL.md. 36 карточек принадлежат
группам B v1 с единственной silver-меткой — она перенесена для согласованности; остальные 45 размечены
по названию карточки, названиям, типам и датам её источников. Это не экспертная разметка: точность по этим
меткам — предварительная оценка того, помогает ли порог модели v1.

Запуск из корня проекта: python3 tools/quality/autolabel_gate_check_20260924.py [--force]
Строки находятся по group_id, автоназвание кластера сверяется. Метки, которые уже стоят в файле и
отличаются от этих, без --force не перезаписываются. Перед первой записью сохраняется копия <файл>.orig;
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

FILE = Path("ml/reports/quality/gate_check.csv")
PREFIX = "авто-silver (Claude): "
# (group_id, автоназвание кластера, код, причина)
LABELS = [
    ('bgrp-3c76073859', 'GANs for Fraud', 'R',
     'как у той же группы в silver v1: синтетические финансовые данные и GAN для обучения антифрод-моделей'),
    ('bgrp-f35656381a', 'Industrial Defect Detection', 'N-MAT',
     'обнаружение дефектов на базе YOLO — распространённая практика машинного зрения в промышленности'),
    ('bgrp-3c8e35f66d', 'responsible AI systems', 'N-OFF',
     'как у той же группы в silver v1: ответственный ИИ и выравнивание, не инфраструктура'),
    ('bgrp-01f42163d4', 'robotics Trends scopes', 'N-OVR',
     'как у той же группы в silver v1: обзор цифровых двойников в робототехнике'),
    ('bgrp-dfb53aaa2c', 'Distributed Edge Intelligence', 'N-OVR',
     'обзоры о больших моделях на периферии сети'),
    ('bgrp-f8d6ebe4ba', 'Artificial Intelligence', 'N-OVR',
     'обзоры ИИ в Индустрии 4.0'),
    ('bgrp-deb3b1f00f', 'Sustainable Deep Learning', 'R',
     'как у той же группы в silver v1: обучение на устройстве (TinyTrain) и сжатие со смешанной точностью для edge'),
    ('bgrp-c5d6fbc793', 'Embodied AI Physical-Layer', 'N-NOI',
     'склейка несвязанных препринтов (DPO, карта решений, eBPF, ESP32, квантовая сеть на FPGA)'),
    ('bgrp-3feee7e38d', 'Form of NFT', 'N-NOI',
     'как у той же группы в silver v1: несвязные тексты о NFT'),
    ('bgrp-472c87c65f', 'Metaverse for Healthcare', 'N-OFF',
     'метавселенная в здравоохранении — не финтех'),
    ('bgrp-3ee738f4dd', 'Models for Robotics', 'N-OVR',
     'как у той же группы в silver v1: обзоры LLM в робототехнике'),
    ('bgrp-204db78482', 'AI-Driven Threat Intelligence', 'N-OFF',
     'как у той же группы в silver v1: ИИ в здравоохранении и киберразведке'),
    ('bgrp-43d7ec7b0c', 'Human AI Safety', 'N-NOI',
     'как у той же группы в silver v1: препринт Zenodo о психологических рисках и статья о GDPR'),
    ('bgrp-328205e217', 'Complete Automation', 'N-OVR',
     'как у той же группы в silver v1: эссе об автоматизации'),
    ('bgrp-0fe757dc2d', 'Trustworthy Edge Intelligence', 'N-OVR',
     'обзоры edge AI и периферийных вычислений'),
    ('bgrp-beb3b7d9c1', 'Применение искусственного интеллекта', 'N-GEN',
     'общие статьи о применении ИИ в экономике, аудите и HR'),
    ('bgrp-b4bcb3c83c', 'Blockchain', 'N-GEN',
     'блокчейн в целом — обзоры и общие работы'),
    ('bgrp-bd13bc0245', 'CyberStrategyInstitute ai-safe2-framework', 'U',
     'фреймворк безопасности ИИ в одном репозитории GitHub, остальное — общие рамки управления рисками; подтверждений мало'),
    ('bgrp-c57dd586bd', 'Automation for Industry', 'N-GEN',
     'автоматизация Индустрии 4.0 — общее понятие, разнородные статьи'),
    ('bgrp-64776ce65d', 'Evaluating Human-AI Collaboration', 'N-OVR',
     'как у той же группы в silver v1: обзоры взаимодействия человека и ИИ и XAI'),
    ('bgrp-46b8f7e990', 'Large Language Model', 'N-GEN',
     'как у той же группы в silver v1: общее понятие LLM, несвязный кластер'),
    ('bgrp-766215ca9b', 'AI storage infrastructure', 'N-GEN',
     'инфраструктура хранения данных для ИИ — общее понятие, смесь обзоров'),
    ('bgrp-0577e4578a', 'Влияние искусственного интеллекта', 'N-GEN',
     'общие статьи о влиянии ИИ на экономику, право и безопасность'),
    ('bgrp-17257dd364', 'Integrating AI', 'N-OFF',
     'ИИ в управлении знаниями и CRM — не инфраструктура ИИ'),
    ('bgrp-ea5dcdf7d5', 'Artificial Intelligence', 'N-GEN',
     'как у той же группы в silver v1: общее понятие ИИ'),
    ('bgrp-b59f299bb0', 'Machine learning Ethereum', 'N-MAT',
     'как у той же группы в silver v1: прогноз цены криптовалют ML — рутинная тема'),
    ('bgrp-3aa71b5be8', 'EdgeAI-MotorMind', 'N-NOI',
     'как у той же группы в silver v1: несвязный кластер'),
    ('bgrp-6aec844635', 'применения искусственного интеллекта', 'N-GEN',
     'общие статьи о применении ИИ в экономике'),
    ('bgrp-90b243039c', '6G Network Edge', 'R',
     'безопасность периферии сетей 6G (6G-XSec, zero trust 6GC) — конкретные подходы для ещё не развёрнутых сетей'),
    ('bgrp-72d2237ffe', 'Satellite-Terrestrial Edge Computing', 'U',
     'орбитальные и спутниковые периферийные вычисления — область исследований, стадию оценить трудно (как Vehicular Edge Computing в silver v1)'),
    ('bgrp-18e60f42b3', 'AI Risks', 'N-OVR',
     'как у той же группы в silver v1: обзор катастрофических рисков ИИ | регулирование ИИ — политика'),
    ('bgrp-b83532c15e', 'Artificial General Intelligence', 'N-GEN',
     'AGI — общее понятие и обзоры'),
    ('bgrp-b979d33e56', 'decentralized finance protocols', 'N-OVR',
     'обзоры и правовые статьи о DeFi'),
    ('bgrp-19b47ef091', 'Learning Operations MLOps', 'N-MAT',
     'MLOps — устоявшаяся практика, обзорные статьи'),
    ('bgrp-8a5ee6e2db', 'Large Language Models', 'N-OVR',
     'обзоры уязвимостей больших языковых моделей'),
    ('bgrp-c87d35991e', 'GPT-4 Technical Report', 'N-OVR',
     'как у той же группы в silver v1: обзор LLM в финансах'),
    ('bgrp-8b52527d73', 'generative conversational AI', 'N-OVR',
     'обзоры генеративного ИИ и ChatGPT'),
    ('bgrp-fd4d15d193', 'Waste-to-Energy-Coupled AI Data', 'R',
     'как у той же группы в silver v1: ЦОД для ИИ с энергией из переработки отходов; в кластере также спекулятивные тексты'),
    ('bgrp-fec52e9761', 'Deep Learning Algorithms', 'N-GEN',
     'алгоритмы глубокого обучения — общее понятие'),
    ('bgrp-9efd32108f', '1D-CNN-IDS', 'N-MAT',
     'как у той же группы в silver v1: CNN-IDS для IIoT — рутинное направление исследований'),
    ('bgrp-a0ef360a9a', 'Vehicular Edge Computing', 'U',
     'как у той же группы в silver v1: Vehicular Edge Computing — исследовательская область; обзор и одна статья, стадию оценить нельзя'),
    ('bgrp-10fefbde0f', 'Sustainable Serverless Edge', 'R',
     'планирование функций в устойчивых бессерверных периферийных вычислениях (REPFS) — конкретный исследовательский метод'),
    ('bgrp-42487edaf2', 'акустической Mesh-системы мониторинга', 'N-NOI',
     'как у той же группы в silver v1: отчёт о разработке учебно-прикладной системы на ESP32, не рыночный сдвиг'),
    ('bgrp-729f240358', 'medical robots turnover', 'N-NOI',
     'правовые риски медицинских роботов и спекулятивный препринт — склейка'),
    ('bgrp-40b0941968', 'Cybersecurity for AI', 'N-OFF',
     'как у той же группы в silver v1: ИИ в кибербезопасности, не защита систем ИИ'),
    ('bgrp-a01181fe47', 'Trusted artificial intelligence', 'N-GEN',
     'как у той же группы в silver v1: доверенный ИИ — общее понятие'),
    ('bgrp-9416bb2d74', 'AI in Cybersecurity', 'N-OFF',
     'ИИ для кибербезопасности, а не защита систем ИИ (как в silver v1)'),
    ('bgrp-d72bb7d10e', 'Prospects of Fintech', 'N-OVR',
     'как у той же группы в silver v1: перспективы финтеха — обзор'),
    ('bgrp-adbe4b1320', 'generative conversational AI', 'N-OFF',
     'ChatGPT в журналистике, образовании и маркетинге — не робототехника'),
    ('bgrp-efdc454719', 'Эффективные совместные периферийные', 'U',
     'как у той же группы в silver v1: кооперативные периферийные вычисления для транспорта: академично, конкретика неясна'),
    ('bgrp-3f9b916120', 'РАЗВИТИЯ ФИНАНСОВЫХ ТЕХНОЛОГИЙ', 'N-GEN',
     'общие статьи о цифровизации финансов и инвестиций'),
    ('bgrp-743b32204e', 'artificial intelligence development', 'N-OVR',
     'как у той же группы в silver v1: политика развития ИИ в России'),
    ('bgrp-3d3f18c4b4', 'Education Will AI', 'N-OFF',
     'ИИ и AGI в образовании — не робототехника'),
    ('bgrp-6a32f8459a', 'Survey on ChatGPT', 'N-OVR',
     'обзоры ChatGPT и генеративного ИИ'),
    ('bgrp-03cbb68cf1', 'Machine Learning', 'N-GEN',
     'машинное обучение в производстве — общее понятие, обзоры'),
    ('bgrp-b2bfe7e019', 'использования искусственного интеллекта', 'N-GEN',
     'общие статьи о регулировании и применении ИИ'),
    ('bgrp-cc6010878c', 'Security in IoMT', 'R',
     'как у той же группы в silver v1: zero trust на периферии сети медицинского IoT: конкретное раннее направление'),
    ('bgrp-8b02725e4a', 'Intelligent Soft Robotics', 'N-OVR',
     'обзоры интеллектуальной мягкой робототехники'),
    ('bgrp-e67dc9ba8a', 'Data-centric Artificial Intelligence', 'N-OVR',
     'обзоры data-centric AI и выравнивания ИИ'),
    ('bgrp-6bfdbc8334', 'intelligence machine learning', 'N-OVR',
     'обзоры ИИ в робототехнике и логистике'),
    ('bgrp-86b7c1f7e8', 'Constructive Brain Quantum-Computer', 'N-HYP',
     'спекулятивные препринты о «квантовой метавселенной» без проверяемого содержания'),
    ('bgrp-bde4d00e3c', 'AI and Blockchain', 'N-GEN',
     'ИИ и блокчейн в финансах — общие статьи'),
    ('bgrp-a14bf1b39b', 'Decentralized Federated Learning', 'N-OVR',
     'как у той же группы в silver v1: обзоры федеративного обучения на блокчейне'),
    ('bgrp-f3f2c794f8', 'LinkedInLearning ai-for-telecom-network-optimization-and-security-in-5g-edge-systems-5215414', 'N-OVR',
     'как у той же группы в silver v1: учебный курс и обзор edge AI для телекома'),
    ('bgrp-6e657bf18c', 'Certification by Circulation', 'N-NOI',
     'статья о безопасности ОС для ИИ-агентов вместе с философскими препринтами — склейка'),
    ('bgrp-8951ff0829', 'interlinkages between cryptocurrencies', 'N-OFF',
     'как у той же группы в silver v1: эконометрика связей криптоактивов'),
    ('bgrp-a802caf2dd', 'Large Language Models', 'N-OFF',
     'LLM в медицине и образовании — не инфраструктура ИИ'),
    ('bgrp-5a5f1f8266', 'AI-Enabled Intrusion Detection', 'N-OFF',
     'как у той же группы в silver v1: ИИ для кибербезопасности (IDS), а не защита систем ИИ — лексически близкая другая тема'),
    ('bgrp-f483329619', 'robot and neurotechnologies', 'N-OVR',
     'как у той же группы в silver v1: правовые аспекты'),
    ('bgrp-1d4e54bfcd', 'Towards AI-Enabled Data-to-Insights', 'N-NOI',
     'как у той же группы в silver v1: несвязный кластер'),
    ('bgrp-b78e2aa68f', 'AI Nano Platform', 'U',
     'как у той же группы в silver v1: no-code платформа ИИ для промышленности из препринта Zenodo, подтверждений нет'),
    ('bgrp-e1d6e23356', 'Educational Platforms Integration', 'N-OFF',
     'ИИ в образовательных платформах — не инфраструктура ИИ'),
    ('bgrp-32fd3cea4f', 'Efficient Off-chain Micro-payment', 'R',
     'платёжные каналы с сохранением приватности (CAPE) и офчейн-микроплатежи — раннее направление финтеха (как в silver v1)'),
    ('bgrp-0ee32293d8', 'Trends in Cryptocurrency', 'N-OVR',
     'обзоры регулирования и инвестиций в криптовалюты'),
    ('bgrp-f8918089e1', 'fog computing system', 'R',
     'как у той же группы в silver v1: фреймворки ИИ-агентов на Raspberry Pi 2 ГБ: агентный инференс на слабом edge-железе, ранняя стадия'),
    ('bgrp-9f53ee401f', 'Intelligence in Healthcare', 'N-OVR',
     'как у той же группы в silver v1: обзоры ИИ в медицине'),
    ('bgrp-c08ab23782', 'generative conversational AI', 'N-OFF',
     'генеративный ИИ и ChatGPT — не финтех'),
    ('bgrp-d7604cda8b', 'Artificial Intelligence-Driven Hybrid', 'N-OVR',
     'обзоры систем хранения энергии для ЦОД ИИ'),
    ('bgrp-ab29d59653', 'AI Act Data', 'N-OFF',
     'правовое управление данными по AI Act — не технология инфраструктуры ИИ'),
    ('bgrp-c1a1b31558', 'Multi-access Edge Computing', 'N-MAT',
     'как у той же группы в silver v1: MEC — стандартизированная парадигма ETSI, общее понятие темы'),
    ('bgrp-0c14c1daf6', 'Deepfake Audio Detection', 'N-OFF',
     'обнаружение дипфейков — ИИ для безопасности людей, а не защита систем ИИ (как в silver v1)'),
]


def _norm(text: str) -> str:
    return re.sub(r"[^0-9a-zа-яё]+", " ", (text or "").lower()).strip()


def main() -> int:
    parser = argparse.ArgumentParser(description="авторазметка gate_check.csv")
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
    if not {"group_id", "cluster_title", "label", "comment"} <= set(fields):
        print(f"СТОП: нет нужных колонок, в файле: {fields}")
        return 1
    if len(rows) != len(LABELS):
        print(f"СТОП: строк {len(rows)}, ожидалось {len(LABELS)} — это не тот файл")
        return 1
    index = {row["group_id"]: row for row in rows}
    conflicts = []
    for gid, auto, code, _reason in LABELS:
        row = index.get(gid)
        if row is None or _norm(row["cluster_title"]) != _norm(auto):
            print(f"СТОП: не найдена строка {gid} «{auto}» — это не тот файл")
            return 1
        current = (row["label"] or "").strip()
        if current and current.upper() != code and not args.force:
            conflicts.append(f"{gid}: в файле «{current}», авторазметка {code}")
    if conflicts:
        print("СТОП: в файле уже есть другие метки, ничего не изменено (перезаписать: --force)")
        print("\n".join(conflicts))
        return 1
    changed = 0
    for gid, _auto, code, reason in LABELS:
        row = index[gid]
        comment = PREFIX + reason
        if row["label"] != code or row["comment"] != comment:
            row["label"], row["comment"] = code, comment
            changed += 1
    counts = dict(sorted(Counter(code for _gid, _auto, code, _reason in LABELS).items()))
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
