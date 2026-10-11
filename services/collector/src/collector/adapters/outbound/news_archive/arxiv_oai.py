"""Корпус метаданных arXiv через OAI-PMH (этап 4.3): https://oaipmh.arxiv.org/oai, наборы cs, eess, q-fin.

OAI-PMH — официальный путь массовой выгрузки метаданных arXiv; выбор по дате подачи не поддерживается, поэтому
набор выгружается целиком, а сохраняются работы с годом первой версии не раньше `since_year`. Выгрузка
возобновляемая: токен продолжения сохраняется после каждой страницы. Пауза между страницами — `pause_seconds`;
503/429 — ожидание Retry-After (управление потоком OAI-PMH), не больше 5 попыток подряд.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, timedelta

from collector.adapters.outbound.sources.xml_utils import parse_xml, text_of
from collector.application.ports import CorpusStore, HttpClient, HttpStatusError, HttpTimeoutError, HttpTransportError
from collector.domain.archive import CorpusHarvestState, CorpusPaper
from collector.domain.text_tokens import tokens_of
from ws_common.clock import Clock

OAI_URL = "https://oaipmh.arxiv.org/oai"
OAI_HOSTS = frozenset({"oaipmh.arxiv.org", "export.arxiv.org"})
NS = {"oai": "http://www.openarchives.org/OAI/2.0/", "ax": "http://arxiv.org/OAI/arXiv/"}
MAX_TRIES = 5


def page_watermark(body: str) -> date | None:
    """Наибольший datestamp заголовков страницы ListRecords (этап 4.6, аудит P12)."""
    root = parse_xml(body, what="arXiv OAI-PMH")
    stamps = []
    for stamp in root.findall(".//oai:header/oai:datestamp", NS):
        try:
            stamps.append(date.fromisoformat(text_of(stamp)[:10]))
        except ValueError:
            continue
    return max(stamps, default=None)


def parse_page(body: str, since_year: int) -> tuple[list[CorpusPaper], int, str | None, bool]:
    """Страница ListRecords → (работы с подходящим годом, всего записей, токен продолжения, «записей нет»)."""
    root = parse_xml(body, what="arXiv OAI-PMH")
    error = root.find("oai:error", NS)
    if error is not None:
        if error.get("code") == "noRecordsMatch":
            return [], 0, None, True
        raise ValueError(f"OAI-PMH: {error.get('code')}: {text_of(error)}")
    papers: list[CorpusPaper] = []
    records = root.findall(".//oai:record", NS)
    for record in records:
        header = record.find("oai:header", NS)
        if header is not None and header.get("status") == "deleted":
            continue
        meta = record.find("oai:metadata/ax:arXiv", NS)
        if meta is None:
            continue
        created = text_of(meta.find("ax:created", NS))
        try:
            created_on = date.fromisoformat(created[:10])
        except ValueError:
            continue
        if created_on.year < since_year:
            continue
        title = " ".join(text_of(meta.find("ax:title", NS)).split())
        abstract = " ".join(text_of(meta.find("ax:abstract", NS)).split())
        paper_id = text_of(meta.find("ax:id", NS)).strip()
        if not title or not paper_id:
            continue
        categories = tuple(text_of(meta.find("ax:categories", NS)).split())
        papers.append(CorpusPaper(paper_id=paper_id, title=title, created_on=created_on, abstract=abstract[:1500],
                                  categories=categories, tokens=tuple(tokens_of(f"{title} {abstract[:300]}"))))
    token_element = root.find(".//oai:resumptionToken", NS)
    token = (token_element.text or "").strip() if token_element is not None else ""
    return papers, len(records), token or None, False


class ArxivOaiHarvester:
    """Возобновляемая выгрузка наборов arXiv в локальный корпус."""

    def __init__(self, http: HttpClient, store: CorpusStore, clock: Clock, pause_seconds: float = 3.0,
                 since_year: int = 2021, report: Callable[[str], None] = print) -> None:
        self._http, self._store, self._clock = http, store, clock
        self._pause, self._since_year, self._report = pause_seconds, since_year, report

    async def harvest(self, set_spec: str, restart: bool = False) -> CorpusHarvestState:
        """Первичная загрузка набора, а после неё — инкрементальное обновление по datestamp (этап 4.6, аудит P12).

        Прежде завершённый набор больше не обновлялся обычным запуском (0 запросов), хотя запускать команду
        рекомендовалось ежемесячно. Теперь при статусе done запрос идёт с `from=<watermark>`; datestamp OAI-PMH —
        дата изменения метаданных, поэтому новые и исправленные записи попадают в корпус без полного перезапуска.
        """
        state = await self._store.harvest_state(set_spec)
        incremental = state is not None and state.status == "done" and not restart
        if incremental:
            since = state.watermark or (date.today() - timedelta(days=45))
            params: dict[str, str] = {"verb": "ListRecords", "metadataPrefix": "arXiv", "set": set_spec,
                                      "from": since.isoformat()}
            self._report(f"{set_spec}: обновление с {since.isoformat()}")
            state = CorpusHarvestState(set_spec, None, state.records, state.kept, "running", state.watermark)
        else:
            if state is None or restart or state.status == "failed" and not state.resumption_token:
                state = CorpusHarvestState(set_spec, None, 0, 0, "running")
            params = ({"verb": "ListRecords", "metadataPrefix": "arXiv", "set": set_spec}
                      if state.resumption_token is None and state.records == 0
                      else {"verb": "ListRecords", "resumptionToken": state.resumption_token or ""})
        watermark = state.watermark
        while True:
            body, failure = await self._fetch(params)
            if failure is not None:
                state = CorpusHarvestState(set_spec, state.resumption_token, state.records, state.kept,
                                           "done" if incremental else "failed", watermark)
                await self._store.save_harvest_state(state, failure)
                self._report(f"{set_spec}: ОТКАЗ {failure}; продолжение — повторным запуском")
                return state
            papers, seen, token, empty = parse_page(body, self._since_year)
            stamp = page_watermark(body) if not empty else None
            watermark = max(filter(None, (watermark, stamp)), default=None)
            kept = await self._store.save_papers(papers)
            status = "done" if token is None else "running"
            state = CorpusHarvestState(set_spec, token, state.records + seen, state.kept + kept, status, watermark)
            await self._store.save_harvest_state(state)
            self._report(f"{set_spec}: записей {state.records}, сохранено {state.kept}" + (" — готово" if token is None else ""))
            if token is None or empty:
                return state
            params = {"verb": "ListRecords", "resumptionToken": token}

    async def _fetch(self, params: dict[str, str]) -> tuple[str, str | None]:
        detail = "нет ответа"
        for attempt in range(MAX_TRIES):
            await self._clock.sleep(self._pause)
            try:
                return await self._http.get_text(OAI_URL, allowed_hosts=OAI_HOSTS, params=params), None
            except HttpStatusError as error:
                detail = f"код {error.status}"
                if error.status not in (429, 503) and error.status < 500:
                    return "", detail
                await self._clock.sleep(error.retry_after or 30.0 * (attempt + 1))
            except (HttpTimeoutError, HttpTransportError) as error:
                detail = f"{type(error).__name__}: {error}"
                await self._clock.sleep(30.0 * (attempt + 1))
        return "", detail
