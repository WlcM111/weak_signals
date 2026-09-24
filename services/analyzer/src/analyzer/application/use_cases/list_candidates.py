"""Сценарий ListCandidates: постраничная выдача кандидатов со стабильным порядком."""

from __future__ import annotations

from analyzer.application.dto import CandidatePage
from analyzer.application.ports import AnalysisRepository, CandidateRepository
from analyzer.application.validation import encode_page_token
from analyzer.domain.errors import NotFoundError
from analyzer.domain.values import Decision


class ListCandidates:
    """Слабые сигналы по возрастанию ранга, затем (по запросу) исключённые по убыванию оценки."""

    def __init__(self, analyses: AnalysisRepository, candidates: CandidateRepository) -> None:
        self._analyses = analyses
        self._candidates = candidates

    def execute(
        self,
        analysis_id: str,
        *,
        include_excluded: bool,
        page_size: int,
        after: tuple[int, float, int] | None,
    ) -> CandidatePage:
        """Страница кандидатов; NOT_FOUND, если анализа нет."""
        if self._analyses.get(analysis_id) is None:
            raise NotFoundError(f"анализ {analysis_id} не найден")
        rows, total = self._candidates.list_candidates(
            analysis_id, include_excluded=include_excluded, after=after, limit=page_size
        )
        next_token = ""
        if len(rows) == page_size and rows:
            last = rows[-1]
            group = 0 if last.decision is Decision.WEAK_SIGNAL else 1
            order_value = float(last.rank) if group == 0 else -last.score
            next_token = encode_page_token(group, order_value, last.cluster_index)
        return CandidatePage(candidates=tuple(rows), next_page_token=next_token, total_count=total)
