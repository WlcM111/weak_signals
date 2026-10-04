"""Повтор задания по снимку без сети (аудит A3, режим А): отбор analyzer, рубрика, доводка и выдача.

Код сервисов тот же, что на стенде; меняются только адаптеры границ (см. `memory.py`). Критерий: повтор одной версии
даёт ту же выдачу, что живое задание, и два повтора подряд совпадают. Параметры можно переопределить (`--set`) —
это абляция на тех же данных; новые запросы к LLM при этом не имеют записи и считаются явно (статус INCOMPLETE).
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ml.benchmark.memory import (
    FrozenSyncClock,
    LoopbackAnalyzer,
    LoopbackCollector,
    LoopbackInsight,
    MemoryAnalysisRepository,
    MemoryCallLog,
    MemoryCandidateRepository,
    MemoryEmbeddingCache,
    MemoryInsightRepository,
    MemoryResultRepository,
    Misses,
    ReplayClock,
    ReplayEmbedder,
    ReplayProvider,
    SnapshotCollectorReader,
    feature_labels,
)
from ml.benchmark.snapshot import Snapshot, live_items, normalize_item, parse_time, sha256_file

REPO_ROOT = Path(__file__).resolve().parents[4]
IDENTICAL, DIFFERENT, INCOMPLETE, NOT_COMPARABLE = "IDENTICAL", "DIFFERENT", "INCOMPLETE", "NOT_COMPARABLE"
OVERRIDE_GROUPS = ("analyzer", "params", "narrate")
SCORE_TOLERANCE = 0.00011  # единица четвёртого знака: orchestrator.result_items.score — numeric(5,4)


@dataclass
class ReplayResult:
    """Итог повтора одного снимка."""

    snapshot: str
    status: str
    items: list[dict[str, Any]] = field(default_factory=list)
    live_items: list[dict[str, Any]] = field(default_factory=list)
    differences: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    misses: dict[str, Any] = field(default_factory=dict)
    trace: dict[str, Any] = field(default_factory=dict)
    candidates: dict[str, str] = field(default_factory=dict)
    overrides: dict[str, Any] = field(default_factory=dict)
    digest: str = ""

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def parse_overrides(pairs: list[str]) -> dict[str, Any]:
    """`группа.параметр=значение` (значение — JSON или строка) → словарь переопределений."""
    result: dict[str, Any] = {}
    for pair in pairs:
        key, _, raw = pair.partition("=")
        group = key.split(".", 1)[0]
        if group not in OVERRIDE_GROUPS or "." not in key or not raw:
            raise ValueError(f"переопределение {pair!r}: ожидается <{'|'.join(OVERRIDE_GROUPS)}>.<параметр>=<значение>")
        try:
            result[key] = json.loads(raw)
        except json.JSONDecodeError:
            result[key] = raw
    return result


def _group(overrides: dict[str, Any], name: str) -> dict[str, Any]:
    return {key.split(".", 1)[1]: value for key, value in overrides.items() if key.startswith(name + ".")}


def _paths() -> dict[str, Path]:
    analyzer_config = REPO_ROOT / "services" / "analyzer" / "config"
    return {
        "registry": Path(os.environ.get("WS_FEATURE_REGISTRY_PATH") or REPO_ROOT / "schemas" / "feature_registry_v1.json"),
        "lexicons": Path(os.environ.get("WS_LEXICON_DIR") or analyzer_config / "lexicons"),
        "stage_rules": Path(os.environ.get("WS_STAGE_RULES_PATH") or analyzer_config / "stage_rules.yaml"),
    }


def replay(snapshot: Snapshot, overrides: dict[str, Any] | None = None) -> ReplayResult:
    """Повторяет задание снимка; возвращает выдачу повтора и сравнение с живой выдачей."""
    overrides = dict(overrides or {})
    result = ReplayResult(snapshot=snapshot.name, status=INCOMPLETE, overrides=overrides)
    result.issues += snapshot.verify()
    for required in ("capture.json", "vectors.npz", "documents.jsonl", "analysis.json", "trace.json", "job.json"):
        if not snapshot.has(required):
            result.issues.append(f"в снимке нет {required}: повтор невозможен")
    if any(issue.startswith("в снимке нет") for issue in result.issues):
        return result
    misses = Misses()
    candidates_repo, analysis_status = _replay_analysis(snapshot, overrides, misses, result)
    if analysis_status != "COMPLETED":
        result.issues.append(f"повтор анализа завершился статусом {analysis_status}")
        result.misses = _misses_dict(misses)
        return result
    asyncio.run(_replay_narrate(snapshot, overrides, candidates_repo, misses, result))
    result.misses = _misses_dict(misses)
    result.live_items = live_items(snapshot)
    result.digest = hashlib.sha256(json.dumps(result.items, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    live_narrate = (snapshot.json("trace.json") or {}).get("narrate") or {}
    if misses.total():
        result.status = INCOMPLETE
    elif live_narrate.get("deadline_cut") or any("не совпадает" in issue for issue in result.issues):
        result.status = NOT_COMPARABLE
    else:
        result.differences = compare(result.live_items, result.items)
        result.status = IDENTICAL if not result.differences else DIFFERENT
    return result


def compare(live: list[dict[str, Any]], replayed: list[dict[str, Any]]) -> list[str]:
    """Различия выдач по полям карточки, существенным для оценки."""
    differences = []
    if len(live) != len(replayed):
        differences.append(f"карточек: живое {len(live)}, повтор {len(replayed)}")
    for left, right in zip(live, replayed, strict=False):
        for key in ("rank", "candidate_id", "title_ru", "score", "narrative_status", "sources"):
            if key == "score" and abs(float(left.get(key) or 0) - float(right.get(key) or 0)) <= SCORE_TOLERANCE:
                continue  # в базе уверенность хранится как numeric(5,4): округление базы — не различие выдачи
            if left.get(key) != right.get(key):
                differences.append(f"ранг {left.get('rank')}: {key}: живое {left.get(key)!r}, повтор {right.get(key)!r}")
    return differences


def _misses_dict(misses: Misses) -> dict[str, Any]:
    return {"llm": len(misses.llm), "embeddings": len(misses.embeddings), "encyclopedia": len(misses.encyclopedia),
            "embedding_lookups": misses.embedding_lookups, "llm_keys": misses.llm[:20],
            "embedding_texts": misses.embeddings[:20], "encyclopedia_calls": misses.encyclopedia[:20]}


def _replay_analysis(snapshot: Snapshot, overrides: dict[str, Any], misses: Misses,
                     result: ReplayResult) -> tuple[MemoryCandidateRepository, str]:
    from analyzer.adapters.outbound.config_loader import load_feature_registry, load_lexicons  # noqa: PLC0415
    from analyzer.adapters.outbound.model_store import FileSystemModelStore  # noqa: PLC0415
    from analyzer.application.active_model import ActiveModelHolder  # noqa: PLC0415
    from analyzer.application.capture import unpack_vectors  # noqa: PLC0415
    from analyzer.application.use_cases.run_analysis import RunAnalysis, RunAnalysisConfig  # noqa: PLC0415
    from analyzer.domain.entities import Analysis  # noqa: PLC0415
    from analyzer.domain.values import AnalysisParams  # noqa: PLC0415

    capture = snapshot.json("capture.json")
    analysis_row = snapshot.json("analysis.json")
    documents, calls = unpack_vectors(snapshot.path("vectors.npz").read_bytes())
    paths = _paths()
    registry = load_feature_registry(paths["registry"])
    lexicons = load_lexicons(paths["lexicons"], paths["stage_rules"])
    model_dirs = sorted(p for p in snapshot.path("model").iterdir() if p.is_dir()) if snapshot.has("model") else []
    if len(model_dirs) != 1:
        raise ValueError(f"в снимке {snapshot.name} ожидается один каталог модели analyzer в model/, найдено {len(model_dirs)}")
    bundle = FileSystemModelStore(snapshot.path("model"), True).load_from_path(model_dirs[0].name, registry)
    if bundle.version.model_version_id != capture["model_version_id"]:
        result.issues.append(f"модель analyzer снимка {bundle.version.model_version_id} не совпадает с записью "
                             f"{capture['model_version_id']}")
    config_values = dict(capture["config"]) | _group(overrides, "analyzer")
    config_values = {key: tuple(value) if isinstance(value, list) else value for key, value in config_values.items()}
    config = RunAnalysisConfig(**config_values)
    params = AnalysisParams(**(dict(capture["params"]) | _group(overrides, "params")))
    analysis = Analysis(analysis_id=str(analysis_row["analysis_id"]), idempotency_key="replay",
                        collection_id=str(analysis_row.get("collection_id") or ""), query_text=capture["query_text"],
                        model_version_id=capture["model_version_id"], params=params)
    analyses, candidates = MemoryAnalysisRepository(), MemoryCandidateRepository()
    use_case = RunAnalysis(
        analyses=analyses, candidates=candidates, embedding_cache=MemoryEmbeddingCache(documents),
        collector=SnapshotCollectorReader(snapshot.jsonl("documents.jsonl"), capture.get("encyclopedia") or [], misses),
        embedder=ReplayEmbedder(capture["embedding_model"], calls, misses), active_model=ActiveModelHolder(bundle),
        lexicons=lexicons, clock=FrozenSyncClock(parse_time(capture["as_of"])), config=config,
    )
    status = use_case.execute(analysis, "replay")
    status_name = getattr(status, "value", str(status))
    if status_name != "COMPLETED":
        result.issues.append(f"анализ: {analyses.finished}")
        return candidates, status_name
    result.candidates = candidates.assign_ids(analysis_row.get("candidates") or [], f"replay:{analysis.analysis_id}")
    candidates.labels = feature_labels(registry, str(paths["registry"]))  # type: ignore[attr-defined]
    return candidates, status_name


async def _replay_narrate(snapshot: Snapshot, overrides: dict[str, Any], candidates: MemoryCandidateRepository,
                          misses: Misses, result: ReplayResult) -> None:
    from insight.application.prompt_builder import PromptBuilder  # noqa: PLC0415
    from insight.application.provider_chain import ChainConfig, ProviderChain  # noqa: PLC0415
    from insight.application.use_cases.finalize_cards import FinalizeCards  # noqa: PLC0415
    from insight.application.use_cases.generate_insight import GenerateConfig, GenerateInsight  # noqa: PLC0415
    from insight.application.use_cases.judge_candidates import JudgeCandidates  # noqa: PLC0415
    from insight.application.use_cases.rubric_judge import RubricJudge  # noqa: PLC0415
    from orchestrator.application.stages.narrate import NarrateConfig, run_narrate  # noqa: PLC0415

    trace = snapshot.json("trace.json")
    narrate = trace.get("narrate") or {}
    if "config" not in narrate:
        result.issues.append("в трассе задания нет настроек стадии narrate: повтор невозможен")
        return
    values = dict(narrate["config"]) | _group(overrides, "narrate")
    live_digests = (narrate.get("models") or {}).get("sha256") or {}
    for key in ("rubric_model_path", "signal_model_path", "profile_model_path"):
        if values.get(key):
            local = snapshot.path("orchestrator_config") / Path(str(values[key])).name
            if live_digests.get(key) and (not local.is_file() or sha256_file(local) != live_digests[key]):
                result.issues.append(f"модель {key} снимка не совпадает с моделью живого задания")
            values[key] = str(local)
    config = NarrateConfig(**values, llm_allowed=None)
    recordings = snapshot.jsonl("llm_recordings.jsonl")
    order: list[str] = []
    for record in recordings:
        if record["provider"] not in order:
            order.append(record["provider"])
    models = {record["provider"]: record["model"] for record in recordings}
    temperature = float(recordings[0]["temperature"]) if recordings else 0.2
    as_of = parse_time((snapshot.json("capture.json") or {}).get("as_of"))
    clock = ReplayClock(as_of)
    chain = ProviderChain([ReplayProvider(name, models[name], recordings, misses) for name in order], MemoryCallLog(),
                          clock, ChainConfig(temperature=temperature))
    prompts = PromptBuilder(snapshot.path("insight/prompts"), snapshot.path("insight/schemas"))
    insight = LoopbackInsight(JudgeCandidates(chain, prompts), RubricJudge(chain, prompts), FinalizeCards(chain, prompts),
                              GenerateInsight(chain, prompts, MemoryInsightRepository(), clock, GenerateConfig()))
    results = MemoryResultRepository()
    job = trace.get("job") or {}

    async def check() -> None:
        return None

    await run_narrate(analyzer=LoopbackAnalyzer(candidates, getattr(candidates, "labels", {})),
                      collector=LoopbackCollector(snapshot.jsonl("documents.jsonl")), insight=insight if order else None,
                      results=results, job_id=snapshot.job_id, query_text=str(job.get("query_text") or snapshot.topic),
                      analysis_id=str(snapshot.json("analysis.json")["analysis_id"]), config=config, check=check)
    result.trace = results.traces
    result.items = [normalize_item({"rank": item.rank, "candidate_id": item.candidate_id, "title_ru": item.title_ru,
                                    "score": item.score, "narrative_status": item.narrative_status.value},
                                   [source.document_id for source in sorted(item.sources, key=lambda s: s.position)])
                    for item in sorted(results.items, key=lambda i: i.rank)]
