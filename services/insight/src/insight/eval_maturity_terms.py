"""Опыт R-022(b)/R-102 (этап 5.3): термины на английском для проверки истории области по OpenAlex.

Для каждой карточки из входного JSONL (`id`, `title_ru`, `description_ru`, `sources` — заголовки) LLM называет
ключевую технологию (`core_en`) и задачу (`task_en`) в общепринятой терминологии публикаций. Зрелость LLM не оценивает:
по замерам judge_v7/judge_v8 это не удаётся, а назвать термин — задача извлечения. Вызовы пишутся в журнал insight.

Запуск в контейнере insight: python -m insight.eval_maturity_terms --input set.jsonl --out terms.jsonl
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

BATCH = 20


def payload(row: dict) -> dict:
    return {"id": row["id"], "title": row.get("title_ru", "")[:300], "description": (row.get("description_ru") or "")[:500],
            "sources": [title[:150] for title in (row.get("sources") or [])[:3]]}


def parse_items(text: str) -> list[dict]:
    start, end = text.find("{"), text.rfind("}")
    data = json.loads(text[start : end + 1]) if start >= 0 < end else {}
    items = data.get("items") if isinstance(data, dict) else None
    return [item for item in items or [] if isinstance(item, dict) and item.get("id")]


async def run(args: argparse.Namespace) -> int:
    from insight.adapters.outbound.metrics import PrometheusMetrics  # noqa: PLC0415 - рабочий контур
    from insight.adapters.outbound.postgres.repositories import PostgresLLMCallLog, PostgresPromptRegistry  # noqa: PLC0415
    from insight.application.prompt_builder import PromptBuilder  # noqa: PLC0415
    from insight.application.provider_chain import ChainConfig, ProviderChain  # noqa: PLC0415
    from insight.application.use_cases.get_provider_status import RegisterPrompts  # noqa: PLC0415
    from insight.config import InsightSettings  # noqa: PLC0415
    from insight.domain.values import Purpose  # noqa: PLC0415
    from insight.main import build_providers  # noqa: PLC0415
    from ws_common.clock import SystemClock  # noqa: PLC0415
    from ws_common.config import load_settings  # noqa: PLC0415
    from ws_common.db import build_pool  # noqa: PLC0415
    from ws_common.logging import configure_logging  # noqa: PLC0415

    settings = load_settings(InsightSettings)
    configure_logging(settings.service_name, settings.log_level, settings.log_format)
    rows = [json.loads(line) for line in Path(args.input).read_text(encoding="utf-8").splitlines() if line.strip()]
    pool = build_pool(settings.pg_dsn, min_size=1, max_size=2, statement_timeout_ms=settings.pg_statement_timeout_ms,
                      application_name=f"{settings.service_name}-terms")
    await pool.open(wait=True, timeout=30)
    prompts = PromptBuilder(settings.prompts_dir, settings.schemas_dir, settings.rubric_prompt_version)
    await RegisterPrompts(PostgresPromptRegistry(pool), prompts).execute()
    chain = ProviderChain(build_providers(settings), PostgresLLMCallLog(pool), SystemClock(), ChainConfig(
        timeout_seconds=settings.llm_timeout_seconds, temperature=settings.llm_temperature,
        circuit_breaker_failures=settings.circuit_breaker_failures,
        circuit_breaker_cooldown_seconds=settings.circuit_breaker_cooldown_seconds,
        daily_token_budget=settings.llm_daily_token_budget, total_budget_seconds=settings.llm_total_budget_seconds),
        PrometheusMetrics())
    found: dict[str, dict] = {}
    for start in range(0, len(rows), BATCH):
        batch = rows[start : start + BATCH]
        for attempt in (1, 2):  # один повтор пакета при сбое или неполном ответе
            missing = [row for row in batch if row["id"] not in found]
            if not missing:
                break
            bundle = prompts.build_maturity_terms_prompt([payload(row) for row in missing])
            try:
                outcome = await chain.complete(bundle.messages, json_schema=bundle.json_schema, max_tokens=2500,
                                               purpose=Purpose.JUDGE, prompt_version=bundle.prompt_version,
                                               request_sha256=bundle.request_sha256)
                wanted = {row["id"] for row in missing}
                for item in parse_items(outcome.result.text):
                    if item["id"] in wanted:
                        found[item["id"]] = {"id": item["id"], "core_en": str(item.get("core_en", "")).strip(),
                                             "task_en": str(item.get("task_en", "")).strip()}
            except Exception as error:  # noqa: BLE001 - опыт: сбой пакета фиксируется, остальные пакеты идут дальше
                print(f"пакет {start // BATCH + 1}, попытка {attempt}: {type(error).__name__}: {str(error)[:160]}", flush=True)
        print(f"термины: {len(found)} из {len(rows)}", flush=True)
    await pool.close()
    with open(args.out, "w", encoding="utf-8") as handle:
        for row in rows:
            if row["id"] in found:
                handle.write(json.dumps(found[row["id"]], ensure_ascii=False) + "\n")
    print(f"готово: {len(found)} из {len(rows)} → {args.out}")
    return 0 if found else 3


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="термины для проверки истории области (этап 5.3)")
    parser.add_argument("--input", required=True)
    parser.add_argument("--out", required=True)
    return asyncio.run(run(parser.parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
