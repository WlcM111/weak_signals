"""Сценарий ScoreText: прямой скоринг описания технологии (§7 HANDOFF).

Без обогащения используются коллекционные и энциклопедические значения-заглушки из артефакта
активной модели: ответ помечается `enrichment_applied=false`, чтобы клиент видел, что признаки
сбора не вычислялись. С обогащением запускается ENRICHMENT-коллекция collector-а.
"""

from __future__ import annotations

import hashlib

from analyzer.application.active_model import ActiveModelHolder
from analyzer.application.dto import ModelBundle, ScoreTextResult
from analyzer.application.ports import CollectorReader, Embedder, MetricsSink, NullMetrics
from analyzer.application.scoring import build_contributions, merge_feature_values, score_rows
from analyzer.domain.entities import DocumentRef
from analyzer.domain.errors import CollectorUnavailable, InternalError
from analyzer.domain.features import (
    COLLECTION_FEATURES,
    ENCYCLOPEDIA_FEATURES,
    EncyclopediaSignal,
    Lexicons,
    collection_features,
    empty_collection_features,
    embedding_features,
    encyclopedia_features,
    lexical_features,
)
from analyzer.domain.rules import apply_exclusion_rules, decide_by_score, explain_model_decision
from analyzer.domain.values import DecisionReason
from ws_common.clock import SyncClock
from ws_common.logging import get_logger

PASSAGE_PREFIX = "passage: "  # тот же префикс e5, с которым trainer кодирует обучающие тексты
ENRICHMENT_POLL_SECONDS = 2.0
ENRICHMENT_CHUNK_SIZE = 200
ENRICHMENT_MAX_DOCUMENTS = 200
SELF_QUERY_SIMILARITY = 1.0


class ScoreText:
    """Оценивает произвольное описание технологии активной моделью."""

    def __init__(
        self,
        collector: CollectorReader,
        embedder: Embedder,
        active_model: ActiveModelHolder,
        lexicons: Lexicons,
        clock: SyncClock,
        enrichment_timeout_seconds: float = 60.0,
        metrics: MetricsSink | None = None,
    ) -> None:
        self._collector = collector
        self._embedder = embedder
        self._active_model = active_model
        self._lexicons = lexicons
        self._clock = clock
        self._enrichment_timeout = enrichment_timeout_seconds
        self._metrics: MetricsSink = metrics or NullMetrics()
        self._log = get_logger("analyzer.score_text")

    def execute(self, title: str, description: str, with_enrichment: bool) -> ScoreTextResult:
        """Признаки, правила и модель для текста «заголовок + описание»."""
        bundle = self._active_model.get()
        text = f"{title}. {description}".strip() if description else title
        now = self._clock.now()
        documents: list[DocumentRef] = []
        enrichment_applied = False
        if with_enrichment:
            documents, enrichment_applied = self._enrich(title)
        collection_values = (
            collection_features(documents, now)
            if enrichment_applied and documents
            else self._defaults(bundle, COLLECTION_FEATURES, empty_collection_features())
        )
        encyclopedia_values = (
            encyclopedia_features(self._encyclopedia(title), now)
            if enrichment_applied
            else self._defaults(bundle, ENCYCLOPEDIA_FEATURES, dict.fromkeys(ENCYCLOPEDIA_FEATURES, 0.0))
        )
        vector = self._embedder.encode([text], PASSAGE_PREFIX)[0]
        values = merge_feature_values(
            lexical_features(text, self._lexicons, now),
            collection_values,
            encyclopedia_values,
            embedding_features(vector, bundle.weak_centroid, bundle.mature_centroid, None)
            | {"emb_sim_query": SELF_QUERY_SIMILARITY},
        )
        return self._decide(bundle, values, documents, enrichment_applied)

    def _decide(
        self,
        bundle: ModelBundle,
        values: dict[str, float],
        documents: list[DocumentRef],
        enrichment_applied: bool,
    ) -> ScoreTextResult:
        """Правила исключения имеют приоритет над моделью, как и в конвейере анализа."""
        rule = apply_exclusion_rules(values, documents, bundle.thresholds)
        try:
            scores, contributions, stages, trends = score_rows(bundle, [values])
        except Exception as error:  # noqa: BLE001 - без модели ответ невозможен
            raise InternalError(f"модель не смогла оценить описание: {error}") from error
        score = float(scores[0])
        features = build_contributions(bundle, values, contributions[0])
        if rule is not None:
            decision, reason, explanation = rule.decision, rule.reason, rule.explanation_ru
        else:
            decision = decide_by_score(score, bundle.threshold, values, bundle.thresholds)
            reason = DecisionReason.MODEL_SCORE
            explanation = explain_model_decision(score, bundle.threshold, features)
        self._log.info(
            "score_text.decided",
            decision=decision.value,
            reason=reason.value,
            score=round(score, 4),
            enrichment_applied=enrichment_applied,
        )
        return ScoreTextResult(
            score=score,
            decision=decision,
            decision_reason=reason,
            features=features,
            model_version_id=bundle.version.model_version_id,
            enrichment_applied=enrichment_applied,
            explanation_ru=explanation,
            predicted_stage=stages[0] if stages else None,
            predicted_trend=trends[0] if trends else None,
        )

    def _enrich(self, title: str) -> tuple[list[DocumentRef], bool]:
        """ENRICHMENT-сбор по названию технологии; при недоступности collector — без обогащения."""
        key = f"score:{hashlib.sha256(title.encode('utf-8')).hexdigest()[:32]}:enrich"
        deadline = self._clock.monotonic() + self._enrichment_timeout
        try:
            collection_id = self._collector.start_enrichment(key, title)
            while True:
                info = self._collector.get_collection(collection_id)
                if info.is_terminal:
                    break
                if self._clock.monotonic() >= deadline:
                    self._log.warning("enrichment.timeout", collection_id=collection_id)
                    return [], False
                self._clock.sleep(ENRICHMENT_POLL_SECONDS)
            if info.documents_total < 1:
                return [], False
            documents = list(
                self._collector.stream_documents(
                    collection_id, ENRICHMENT_CHUNK_SIZE, ENRICHMENT_MAX_DOCUMENTS
                )
            )
        except CollectorUnavailable as error:
            self._log.warning("enrichment.unavailable", error=str(error))
            return [], False
        self._log.info("enrichment.applied", documents=len(documents))
        return documents, bool(documents)

    def _encyclopedia(self, title: str) -> EncyclopediaSignal:
        """Лучший результат Wikipedia по названию на русском и английском."""
        best = EncyclopediaSignal()
        for language in ("ru", "en"):
            try:
                hits = self._collector.check_encyclopedia([title], language)
            except CollectorUnavailable:
                continue
            for hit in hits:
                if hit.exists and (not best.exists or max(hit.pageviews_30d, 0) > best.pageviews_30d):
                    best = EncyclopediaSignal(
                        exists=True,
                        pageviews_30d=max(hit.pageviews_30d, 0),
                        created_at=hit.created_at,
                    )
        return best

    @staticmethod
    def _defaults(
        bundle: ModelBundle, names: tuple[str, ...], fallback: dict[str, float]
    ) -> dict[str, float]:
        """Значения-заглушки признаков из артефакта модели с резервом «пустой коллекции»."""
        return {name: float(bundle.feature_defaults.get(name, fallback.get(name, 0.0))) for name in names}
