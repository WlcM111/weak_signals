/** Страница 3 «Инсайт»: документ-отчёт по одной технологии (§7 HANDOFF_UI, §4 ТЗ). */

import { useEffect, useRef, useState } from "react";
import type { MouseEvent } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { ApiCallError, api } from "../api/client";
import {
  CONFIDENCE_BAND_RU, NARRATIVE_STATUS_RU, SOURCE_TYPE_RU, STAGE_ORDINAL_RU, TREND_ORDINAL_RU,
  confidenceChipClass, narrativeChipClass, translate,
} from "../lib/format";
import { buildInsightReport, reportFileName } from "../lib/report";
import { Banner, ScoreDial, SkeletonList } from "../components/ui";
import { FeatureTable, PredictorChart, SourceList } from "../components/signals";
import { IconArrowLeft, IconDownload } from "../components/icons";
import { scrollBehavior } from "../lib/style";

const ANALYTICAL_TYPES = new Set(["ANALYTICAL_REPORT", "GOVERNMENT", "STANDARD"]);

const SECTIONS = [
  { id: "insight-description", title: "Описание технологии" },
  { id: "insight-advantage", title: "Потенциальное преимущество" },
  { id: "insight-case", title: "Кейс-пример" },
  { id: "insight-analytics", title: "Оценки в аналитических отчётах" },
  { id: "insight-why", title: "Почему это слабый сигнал" },
  { id: "insight-sources", title: "Источники" },
  { id: "insight-provenance", title: "Происхождение" },
] as const;

type SectionId = (typeof SECTIONS)[number]["id"];

function isSideColumn(node: HTMLElement | null): boolean {
  return Boolean(node && getComputedStyle(node).position === "sticky");
}

function scrollerOf(node: HTMLElement | null): Element {
  for (let parent = node?.parentElement; parent; parent = parent.parentElement) {
    const overflow = getComputedStyle(parent).overflowY;
    if ((overflow === "auto" || overflow === "scroll") && parent.scrollHeight > parent.clientHeight) return parent;
  }
  return document.scrollingElement ?? document.documentElement;
}

function isOnScreen(id: string): boolean {
  const rect = document.getElementById(id)?.getBoundingClientRect();
  return Boolean(rect && rect.bottom > 0 && rect.top < window.innerHeight);
}

export default function InsightPage() {
  const { itemId = "" } = useParams();
  const [params] = useSearchParams();
  const jobId = params.get("job") ?? "";
  const [active, setActive] = useState<SectionId>(SECTIONS[0].id);
  // Раздел, выбранный в оглавлении, держится до ручной прокрутки
  const pinned = useRef<SectionId | null>(null);

  const item = useQuery({
    queryKey: ["item", itemId],
    queryFn: () => api.getResultItem(itemId),
    enabled: Boolean(itemId),
  });

  useEffect(() => {
    document.title = item.data ? `${item.data.title_ru} — инсайт` : "Инсайт";
  }, [item.data]);

  useEffect(() => {
    if (!item.data) return undefined;
    const visible = new Map<string, boolean>();
    let frame = 0;
    const update = () => {
      frame = 0;
      if (pinned.current) return;
      const sources = document.getElementById("insight-sources");
      const list = SECTIONS.filter((section) => !(section.id === "insight-sources" && isSideColumn(sources)));
      const scroller = scrollerOf(document.getElementById(SECTIONS[0].id));
      const atEnd = scroller.scrollTop > 0 && scroller.scrollTop + scroller.clientHeight >= scroller.scrollHeight - 2;
      const pick = atEnd
        ? [...list].reverse().find((section) => isOnScreen(section.id))
        : list.find((section) => visible.get(section.id));
      if (pick) setActive(pick.id);
    };
    const schedule = () => {
      if (!frame) frame = requestAnimationFrame(update);
    };
    const release = () => {
      pinned.current = null;
    };
    const observer = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => visible.set(entry.target.id, entry.isIntersecting));
        schedule();
      },
      { rootMargin: "-12% 0px -55% 0px" },
    );
    SECTIONS.forEach((section) => {
      const node = document.getElementById(section.id);
      if (node) observer.observe(node);
    });
    const inputs = ["wheel", "pointerdown", "keydown"] as const;
    inputs.forEach((name) => window.addEventListener(name, release, { passive: true }));
    document.addEventListener("scroll", schedule, { capture: true, passive: true });
    return () => {
      observer.disconnect();
      if (frame) cancelAnimationFrame(frame);
      inputs.forEach((name) => window.removeEventListener(name, release));
      document.removeEventListener("scroll", schedule, { capture: true });
    };
  }, [item.data]);

  if (item.isLoading) {
    return (
      <div className="page page--narrow">
        <SkeletonList rows={4} />
      </div>
    );
  }
  if (item.isError) {
    return (
      <div className="page page--narrow">
        <Banner kind="error" title="Инсайт недоступен">
          {item.error instanceof ApiCallError ? item.error.message : "Повторите попытку позже"}
        </Banner>
      </div>
    );
  }

  const data = item.data!;
  const reports = data.sources.filter((source) => ANALYTICAL_TYPES.has(source.source_type));
  // Аналитических отчётов среди источников может не быть: тогда показываются оценки из самих источников
  // карточки (научные публикации, патенты, отраслевые СМИ) — их русскоязычные резюме.
  const analytical = reports.length > 0 ? reports : data.sources.filter((source) => source.summary_ru);
  const caseSource = data.sources.find((source) => source.document_id === data.case_document_id);

  function downloadReport() {
    const blob = new Blob([buildInsightReport(data)], { type: "text/markdown;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = reportFileName(data);
    link.click();
    URL.revokeObjectURL(url);
  }

  function jumpTo(event: MouseEvent<HTMLAnchorElement>, id: SectionId) {
    const target = document.getElementById(id);
    if (!target) return;
    event.preventDefault();
    pinned.current = id;
    setActive(id);
    target.scrollIntoView({ behavior: scrollBehavior(), block: "start" });
  }

  return (
    <article className="page insight">
      <div className="insight__bar">
        {jobId ? (
          <Link className="btn btn--ghost" to={`/results/${jobId}`}>
            <IconArrowLeft size={18} />
            К списку сигналов
          </Link>
        ) : (
          <Link className="btn btn--ghost" to="/">
            <IconArrowLeft size={18} />
            К запросу
          </Link>
        )}
        <button type="button" className="btn btn--primary" onClick={downloadReport}>
          <IconDownload size={18} />
          Скачать отчёт (Markdown)
        </button>
      </div>

      <aside className="insight__summary">
        <div className="insight__card">
          <p className="insight__context">
            <span>Запрос: {data.query_text}</span>
            {data.title_auto && data.title_auto !== data.title_ru ? (
              <span>Исходное название кандидата: {data.title_auto}</span>
            ) : null}
          </p>
          <h1 className="insight__title">{data.title_ru}</h1>
          <div className="insight__score">
            <ScoreDial value={data.score} size="lg" />
            <div className="insight__chips">
              <span className="chip chip--accent">Слабый сигнал, ранг {data.rank}</span>
              <span className={confidenceChipClass(data.confidence_band)}>
                {translate(CONFIDENCE_BAND_RU, data.confidence_band)}
              </span>
              <span className={narrativeChipClass(data.narrative_status)}>
                {translate(NARRATIVE_STATUS_RU, data.narrative_status)}
              </span>
              {data.predicted_stage ? (
                <span className="chip">
                  Стадия: {STAGE_ORDINAL_RU[data.predicted_stage] ?? data.predicted_stage}
                </span>
              ) : null}
              {data.predicted_trend ? (
                <span className="chip">
                  Тренд упоминаний: {TREND_ORDINAL_RU[data.predicted_trend] ?? data.predicted_trend}
                </span>
              ) : null}
            </div>
          </div>
        </div>
        <nav className="toc" aria-label="Разделы инсайта">
          <ol className="toc__list">
            {SECTIONS.map((section) => (
              <li key={section.id}>
                <a
                  className={`toc__link ${active === section.id ? "toc__link--active" : ""}`}
                  href={`#${section.id}`}
                  aria-current={active === section.id ? "location" : undefined}
                  onClick={(event) => jumpTo(event, section.id)}
                >
                  {section.title}
                  {section.id === "insight-sources" ? <span className="toc__count">{data.sources.length}</span> : null}
                </a>
              </li>
            ))}
          </ol>
        </nav>
      </aside>

      <div className="insight__main">
        <div className="insight__pair">
          <section className="doc-section doc-section--lead" id="insight-description">
            <h2 className="doc-section__title">Описание технологии</h2>
            <p className="doc-lead">{data.description_ru || "—"}</p>
          </section>
          <section className="doc-section doc-section--lead" id="insight-advantage">
            <h2 className="doc-section__title">Потенциальное преимущество</h2>
            <p className="doc-lead">{data.advantage_ru || "—"}</p>
          </section>
        </div>

        <section className="doc-section" id="insight-case">
          <h2 className="doc-section__title">Кейс-пример</h2>
          <p>{data.case_example_ru || "—"}</p>
          {caseSource ? (
            <p className="doc-note">
              Источник кейс-примера: <a href={`#source-${caseSource.document_id}`}>{caseSource.title}</a>
            </p>
          ) : null}
        </section>

        <section className="doc-section" id="insight-analytics">
          <h2 className="doc-section__title">Оценки в аналитических отчётах</h2>
          {analytical.length === 0 ? (
            <p className="doc-note">Аналитические отчёты по теме среди найденных источников отсутствуют.</p>
          ) : (
            <>
            {reports.length === 0 ? (
              <p className="doc-note">
                Аналитических отчётов среди источников нет — ниже оценки по научным публикациям, патентам и
                отраслевым источникам карточки.
              </p>
            ) : null}
            <ul className="analytics">
              {analytical.map((source) => (
                <li key={source.document_id} className="analytics__item">
                  <a href={`#source-${source.document_id}`}>{source.title}</a>{" "}
                  <span className="analytics__type">({translate(SOURCE_TYPE_RU, source.source_type)})</span>
                  <p className="analytics__summary">{source.summary_ru}</p>
                </li>
              ))}
            </ul>
            </>
          )}
        </section>

        <section className="doc-section" id="insight-why">
          <h2 className="doc-section__title">Почему это слабый сигнал</h2>
          <p>{data.explanation_ru || "—"}</p>
          {data.decision_explanation_ru && data.decision_explanation_ru !== data.explanation_ru ? (
            <p className="doc-note">{data.decision_explanation_ru}</p>
          ) : null}
          <div className="doc-figure">
            <h3 className="doc-figure__title">Ключевые предикторы</h3>
            <PredictorChart features={data.key_predictors} />
          </div>
          <details className="disclosure">
            <summary className="disclosure__summary">Все признаки модели ({data.features.length})</summary>
            <div className="disclosure__body">
              <p className="doc-note">
                Значение — величина признака у кандидата, вклад — влияние признака на решение модели.
                Положительный вклад поддерживает отнесение к слабому сигналу, отрицательный — говорит
                о зрелости технологии.
              </p>
              <FeatureTable features={data.features} />
            </div>
          </details>
        </section>

        <section className="doc-section insight__sources" id="insight-sources">
          <h2 className="doc-section__title">
            Источники <span className="doc-section__count">{data.sources.length}</span>
          </h2>
          <p className="doc-note">
            Для каждого источника указаны наименование, ссылка, дата публикации, тип, язык оригинала и
            уровень доверенности. Резюме иноязычных материалов помечено как автоматическое.
          </p>
          <SourceList sources={data.sources} caseDocumentId={data.case_document_id} />
        </section>

        <section className="doc-section" id="insight-provenance">
          <h2 className="doc-section__title">Происхождение</h2>
          <dl className="provenance">
            <div>
              <dt>Модель</dt>
              <dd>{data.provenance.model_version_id || "—"}</dd>
            </div>
            <div>
              <dt>Провайдер LLM</dt>
              <dd>{data.provenance.llm_provider || "—"}</dd>
            </div>
            <div>
              <dt>Модель LLM</dt>
              <dd>{data.provenance.llm_model || "—"}</dd>
            </div>
            <div>
              <dt>Версия промпта</dt>
              <dd>{data.provenance.prompt_version || "—"}</dd>
            </div>
          </dl>
          <p className="doc-note">
            <span className={narrativeChipClass(data.narrative_status)}>
              {translate(NARRATIVE_STATUS_RU, data.narrative_status)}
            </span>
          </p>
        </section>
      </div>
    </article>
  );
}
