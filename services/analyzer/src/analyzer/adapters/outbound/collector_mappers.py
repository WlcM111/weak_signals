"""`common.v1.Document` → документ анализа (общая функция клиента collector и повтора режима А)."""

from __future__ import annotations

from datetime import UTC

from weaksignals.common.v1 import common_pb2

from analyzer.domain.entities import DocumentRef
from analyzer.domain.values import SourceType, TrustLevel


def to_document_ref(document: common_pb2.Document, relevance_rank: int) -> DocumentRef:
    """`common.v1.Document` → документ в памяти анализа."""
    return DocumentRef(
        document_id=document.document_id,
        title=document.title,
        text=document.text,
        language_code=document.language_code,
        source_type=SourceType[
            common_pb2.SourceType.Name(document.source_type).removeprefix("SOURCE_TYPE_")
        ],
        trust_level=TrustLevel[
            common_pb2.TrustLevel.Name(document.trust_level).removeprefix("TRUST_LEVEL_")
        ],
        origin_domain=document.origin_domain,
        url=document.url,
        published_at=document.published_at.ToDatetime(tzinfo=UTC) if document.HasField("published_at") else None,
        citation_count=document.citation_count if document.HasField("citation_count") else None,
        engagement_count=document.engagement_count if document.HasField("engagement_count") else None,
        relevance_rank=relevance_rank,
    )
