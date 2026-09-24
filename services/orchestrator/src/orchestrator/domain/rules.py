"""Правила завершения задания: COMPLETED или PARTIAL и сборка сообщения (§7.5 HANDOFF)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from orchestrator.domain.values import JobStatus

COLLECTION_COMPLETED = "COMPLETED"


@dataclass(frozen=True, slots=True)
class Completion:
    """Итоговый статус задания и пояснение, если он неполный."""

    status: JobStatus
    error_message: str = ""


def decide_completion(
    items_written: int,
    requested_top_n: int,
    narratives_fallback: int,
    collection_status: str,
    failed_adapters: Sequence[str] = (),
    deadline_reached: bool = False,
) -> Completion:
    """COMPLETED только при полном результате; иначе PARTIAL с перечислением причин.

    Причины собираются в детерминированном порядке и включают только присутствующие части,
    чтобы сообщение можно было разобрать программно и показать в интерфейсе.
    """
    complete = (
        items_written >= requested_top_n
        and narratives_fallback == 0
        and collection_status == COLLECTION_COMPLETED
        and not failed_adapters
        and not deadline_reached
    )
    if complete:
        return Completion(JobStatus.COMPLETED)
    parts: list[str] = []
    if items_written < requested_top_n:
        parts.append(f"found={items_written}<{requested_top_n}")
    if narratives_fallback:
        parts.append(f"fallback_narratives={narratives_fallback}")
    if failed_adapters:
        parts.append(f"adapters_failed={','.join(sorted(failed_adapters))}")
    if collection_status != COLLECTION_COMPLETED:
        parts.append(f"collection={collection_status}")
    if deadline_reached:
        parts.append("deadline_reached")
    return Completion(JobStatus.PARTIAL, "PARTIAL:" + ";".join(parts))
