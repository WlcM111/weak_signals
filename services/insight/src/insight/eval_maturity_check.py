"""Опыт этапа 5.4: второй проход зрелости на размеченных карточках — тем же кодом, что в рабочем пути.

Для каждой карточки только с научными, патентными и кодовыми источниками из `labeled.jsonl`
(`tools/diagnostics/build_maturity_set.py`) модель называет задачу, год, с которого её решают, широту и вид новизны —
классом `MaturityCheck`, который в рабочем пути вызывает рубрика. Карточки с рыночными источниками не отправляются:
правило их не трогает. Итог проверяет `tools/diagnostics/maturity_check_eval.py`.

Запуск в контейнере insight: python -m insight.eval_maturity_check --input labeled.jsonl --out assessments.jsonl
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict
from pathlib import Path


async def run(args: argparse.Namespace) -> int:
    from insight.adapters.outbound.metrics import PrometheusMetrics  # noqa: PLC0415 - рабочий контур
    from insight.adapters.outbound.postgres.repositories import PostgresLLMCallLog, PostgresPromptRegistry  # noqa: PLC0415
    from insight.application.prompt_builder import PromptBuilder  # noqa: PLC0415
    from insight.application.provider_chain import ChainConfig, ProviderChain  # noqa: PLC0415
    from insight.application.use_cases.get_provider_status import RegisterPrompts  # noqa: PLC0415
    from insight.application.use_cases.maturity_check import MaturityCheck, MaturityInput  # noqa: PLC0415
    from insight.config import InsightSettings  # noqa: PLC0415
    from insight.main import build_providers  # noqa: PLC0415
    from ws_common.clock import SystemClock  # noqa: PLC0415
    from ws_common.config import load_settings  # noqa: PLC0415
    from ws_common.db import build_pool  # noqa: PLC0415
    from ws_common.logging import configure_logging  # noqa: PLC0415

    settings = load_settings(InsightSettings)
    configure_logging(settings.service_name, settings.log_level, settings.log_format)
    rows = [json.loads(line) for line in Path(args.input).read_text(encoding="utf-8").splitlines() if line.strip()]
    targets = [row for row in rows if row.get("research_only")]
    pool = build_pool(settings.pg_dsn, min_size=1, max_size=2, statement_timeout_ms=settings.pg_statement_timeout_ms,
                      application_name=f"{settings.service_name}-maturity")
    await pool.open(wait=True, timeout=30)
    prompts = PromptBuilder(settings.prompts_dir, settings.schemas_dir, settings.rubric_prompt_version)
    await RegisterPrompts(PostgresPromptRegistry(pool), prompts).execute()
    chain = ProviderChain(build_providers(settings), PostgresLLMCallLog(pool), SystemClock(), ChainConfig(
        timeout_seconds=settings.llm_timeout_seconds, temperature=settings.llm_temperature,
        circuit_breaker_failures=settings.circuit_breaker_failures,
        circuit_breaker_cooldown_seconds=settings.circuit_breaker_cooldown_seconds,
        daily_token_budget=settings.llm_daily_token_budget, total_budget_seconds=settings.llm_total_budget_seconds),
        PrometheusMetrics())
    check = MaturityCheck(chain, prompts)
    inputs = [MaturityInput(row["id"], row.get("title_ru", ""), row.get("description_ru", ""),
                            tuple(row.get("sources") or ())) for row in targets]
    found = {}
    for start in range(0, len(inputs), 20):  # прогресс по 20 карточек; внутри — пачки по 5
        found.update(await check.assess(inputs[start : start + 20]))
        print(f"оценено: {len(found)} из {len(inputs)} (несовпадений: {check.stats['misaligned']}, "
              f"сбоев пачек: {check.stats['failed_batches']})", flush=True)
    missing = [item for item in inputs if item.key not in found]
    if missing:  # один повтор для неоценённых
        found.update(await check.assess(missing))
    await pool.close()
    with open(args.out, "w", encoding="utf-8") as handle:
        for item in inputs:
            if item.key in found:
                handle.write(json.dumps(asdict(found[item.key]), ensure_ascii=False) + "\n")
    print(f"готово: {len(found)} из {len(inputs)} → {args.out}; статистика: {check.stats}")
    return 0 if found else 3


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="второй проход зрелости на размеченных карточках (этап 5.4)")
    parser.add_argument("--input", required=True)
    parser.add_argument("--out", required=True)
    return asyncio.run(run(parser.parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
