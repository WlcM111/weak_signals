/** Компоненты выдачи: ход задания, сводка, обозреватель сигналов, исключённые, признаки, источники. */

import { useEffect, useRef, useState } from "react";
import type { KeyboardEvent } from "react";
import { Link } from "react-router-dom";
import type {
  ExcludedCandidate, Feature, JobStatus, ResultItemSummary, ResultStats, Source,
} from "../api/types";
import {
  CONFIDENCE_BAND_RU, CONFIDENT_SCORE, DECISION_REASON_RU, DECISION_RU, DIRECTION_RU,
  JOB_STATUS_RU, LANGUAGE_RU, NARRATIVE_STATUS_RU, SOURCE_TYPE_RU, STAGE_HINT_RU, TRUST_LEVEL_RU,
  confidenceChipClass, formatDate, formatNumber, isSafeLink, jobProgressShare, linkHost,
  narrativeChipClass, percent, summaryMark, translate, trustChipClass,
} from "../lib/format";
import { cssVars } from "../lib/style";
import { rootFontSize, useSize } from "../lib/useSize";
import { IconArrowRight, IconChevron, IconExternal, IconFilter } from "./icons";
import { Progress, ScoreDial, Stat } from "./ui";

const SPLIT_MIN_REM = 40;

function signed(value: number, digits = 2): string {
  const text = Math.abs(value).toFixed(digits).replace(".", ",");
  return value < 0 ? `\u2212${text}` : `+${text}`;
}

function toneOf(direction: string): "signal" | "mature" | "neutral" {
  if (direction === "supports_weak_signal") return "signal";
  if (direction === "supports_mature") return "mature";
  return "neutral";
}

/* ---------- ход задания ---------- */

export function JobProgressPanel({ status, done, total, hint }: {
  status: JobStatus;
  done: number;
  total: number;
  hint?: string;
}) {
  return (
    <div className="live">
      <div className="live__line">
        <span className="live__stage">
          <span className="live__pulse" aria-hidden="true" />
          {translate(JOB_STATUS_RU, status)}
        </span>
        <span className="live__hint">{hint ?? translate(STAGE_HINT_RU, status)}</span>
      </div>
      <Progress share={jobProgressShare(status, done, total)} label="Ход выполнения задания" />
      {status === "NARRATING" ? (
        <p className="live__note">
          Готово инсайтов: {done} из {total || "—"}. Сигналы появляются в списке по мере готовности.
        </p>
      ) : null}
    </div>
  );
}

/* ---------- сводка по выдаче ---------- */

function ScoreSpectrum({ items, activeId, onPick }: {
  items: ResultItemSummary[];
  activeId?: string;
  onPick?: (itemId: string) => void;
}) {
  const threshold = `${CONFIDENT_SCORE * 100}%`;
  return (
    <figure className="spectrum">
      <figcaption className="spectrum__caption">Оценки модели по рангу</figcaption>
      <div className="spectrum__plot" style={cssVars({ "--threshold": threshold })}>
        <span className="spectrum__line" aria-hidden="true">
          <span className="spectrum__line-label">75 %</span>
        </span>
        {items.map((item) => {
          const className = `spectrum__bar ${item.score >= CONFIDENT_SCORE ? "spectrum__bar--high" : ""} ${
            item.item_id === activeId ? "spectrum__bar--active" : ""
          }`;
          const style = cssVars({ "--h": Math.max(0.03, Math.min(1, item.score)).toFixed(3) });
          const title = `${item.rank}. ${item.title_ru}: ${percent(item.score)}`;
          return onPick ? (
            <button
              key={item.item_id}
              type="button"
              tabIndex={-1}
              className={className}
              style={style}
              title={title}
              aria-label={`Ранг ${item.rank}, оценка ${percent(item.score)}`}
              onClick={() => onPick(item.item_id)}
            />
          ) : (
            <span key={item.item_id} className={className} style={style} title={title} />
          );
        })}
      </div>
    </figure>
  );
}

export function StatsRow({ stats, items = [], activeId, onShowExcluded, onPick }: {
  stats: ResultStats;
  items?: ResultItemSummary[];
  activeId?: string;
  onShowExcluded?: () => void;
  onPick?: (itemId: string) => void;
}) {
  return (
    <div className="stats">
      <dl className="stats__grid">
        <Stat
          label="Кандидатов на слабый сигнал"
          value={formatNumber(stats.candidates_found)}
          hint={`Из них признаны слабыми сигналами: ${formatNumber(stats.weak_signals_total)}`}
          action={
            onShowExcluded ? (
              <button type="button" className="link-button" onClick={onShowExcluded}>
                <IconFilter size={16} />
                Показать исключённых
              </button>
            ) : undefined
          }
        />
        <Stat
          label="Обработано источников"
          value={formatNumber(stats.http_requests_total)}
          hint={`запросов к ${formatNumber(stats.sources_processed)} источникам, документов: ${formatNumber(stats.documents_collected)}`}
        />
        <Stat
          label="Уверенность модели выше 75 %"
          value={formatNumber(stats.weak_signals_confident)}
          hint="среди найденных слабых сигналов"
          accent
        />
        <Stat
          label="Версия модели"
          value={stats.model_version_id || "—"}
          text
          hint={
            stats.narratives_fallback
              ? `Экстрактивных инсайтов: ${formatNumber(stats.narratives_fallback)}`
              : "Все инсайты сгенерированы моделью"
          }
        />
      </dl>
      {items.length > 0 ? <ScoreSpectrum items={items} activeId={activeId} onPick={onPick} /> : null}
    </div>
  );
}

/* ---------- обозреватель сигналов ---------- */

function ScoreBar({ score }: { score: number }) {
  return (
    <span
      className={`score-bar ${score >= CONFIDENT_SCORE ? "score-bar--high" : ""}`}
      style={cssVars({ "--score": Math.max(0, Math.min(1, score)).toFixed(3) })}
      aria-hidden="true"
    >
      <span className="score-bar__fill" />
      <span className="score-bar__mark" />
    </span>
  );
}

export function PredictorChart({ features }: { features: Feature[] }) {
  if (features.length === 0) {
    return <p className="note">ключевые предикторы недоступны</p>;
  }
  const scale = Math.max(...features.map((feature) => Math.abs(feature.contribution)), 1e-6);
  const directions = Array.from(new Set(features.map((feature) => feature.direction)));
  return (
    <div className="predictors">
      <ul className="predictors__list">
        {features.map((feature) => (
          <li
            key={feature.feature_name}
            className={`predictor predictor--${toneOf(feature.direction)} ${
              feature.contribution < 0 ? "predictor--minus" : "predictor--plus"
            }`}
            style={cssVars({ "--share": (Math.abs(feature.contribution) / scale).toFixed(3) })}
          >
            <span className="predictor__label">{feature.label_ru}</span>
            <span className="predictor__track" aria-hidden="true">
              <span className="predictor__bar" />
            </span>
            <span className="predictor__value">
              {signed(feature.contribution)}
              <span className="visually-hidden">, {translate(DIRECTION_RU, feature.direction)}</span>
            </span>
          </li>
        ))}
      </ul>
      <ul className="legend" aria-hidden="true">
        {directions.map((direction) => (
          <li key={direction} className={`legend__item legend__item--${toneOf(direction)}`}>
            {translate(DIRECTION_RU, direction)}
          </li>
        ))}
      </ul>
    </div>
  );
}

function SignalDetail({ item, jobId, total }: { item: ResultItemSummary; jobId: string; total: number }) {
  return (
    <article className="detail">
      <header className="detail__head">
        <span className="detail__rank">
          Ранг {item.rank} из {total}
        </span>
        <h2 className="detail__title">{item.title_ru}</h2>
      </header>
      <div className="detail__score">
        <ScoreDial value={item.score} />
        <div className="detail__chips">
          <span className={confidenceChipClass(item.confidence_band)}>
            {translate(CONFIDENCE_BAND_RU, item.confidence_band)}
          </span>
          <span className={narrativeChipClass(item.narrative_status)}>
            {translate(NARRATIVE_STATUS_RU, item.narrative_status)}
          </span>
          <span className="chip">Источников: {item.source_count}</span>
        </div>
      </div>
      <section className="detail__section">
        <h3 className="detail__label">Ключевые предикторы</h3>
        <PredictorChart features={item.key_predictors} />
      </section>
      <section className="detail__section">
        <h3 className="detail__label">Объяснение решения</h3>
        <p className="detail__text">{item.decision_explanation_ru}</p>
      </section>
      <div className="detail__actions">
        <Link className="btn btn--primary" to={`/insight/${item.item_id}?job=${jobId}`}>
          Открыть инсайт
          <IconArrowRight size={18} />
        </Link>
      </div>
    </article>
  );
}

export function SignalExplorer({ items, jobId, selectedId, onSelect }: {
  items: ResultItemSummary[];
  jobId: string;
  selectedId?: string | null;
  onSelect: (itemId: string) => void;
}) {
  const rootRef = useRef<HTMLDivElement | null>(null);
  const detailRef = useRef<HTMLDivElement | null>(null);
  const buttons = useRef(new Map<string, HTMLButtonElement>());
  const userAction = useRef(false);
  const [open, setOpen] = useState(false);
  const { width } = useSize(rootRef);
  const split = width === 0 || width >= SPLIT_MIN_REM * rootFontSize();
  const selected = items.find((item) => item.item_id === selectedId) ?? items[0];
  const selectedKey = selected?.item_id;

  const shownKey = useRef(selectedKey);

  useEffect(() => {
    if (!selectedKey || shownKey.current === selectedKey) return;
    shownKey.current = selectedKey;
    const byUser = userAction.current;
    userAction.current = false;
    if (split) {
      detailRef.current?.scrollTo({ top: 0 });
      if (!byUser) buttons.current.get(selectedKey)?.scrollIntoView({ block: "nearest" });
    } else if (byUser) {
      buttons.current.get(selectedKey)?.scrollIntoView({ block: "nearest" });
    }
  }, [selectedKey, split]);

  if (!selected) return null;

  function choose(itemId: string, byKeyboard: boolean) {
    userAction.current = true;
    if (!split && !byKeyboard) {
      if (itemId === selected.item_id) {
        setOpen((value) => !value);
        return;
      }
      setOpen(true);
    }
    onSelect(itemId);
  }

  function onKeyDown(event: KeyboardEvent<HTMLOListElement>) {
    const target = event.target as HTMLElement;
    if (!target.classList.contains("signal-row")) return;
    const index = items.findIndex((item) => item.item_id === selected.item_id);
    const next =
      event.key === "ArrowDown" ? index + 1
        : event.key === "ArrowUp" ? index - 1
          : event.key === "Home" ? 0
            : event.key === "End" ? items.length - 1
              : null;
    if (next === null) return;
    event.preventDefault();
    const bounded = items[Math.max(0, Math.min(items.length - 1, next))];
    choose(bounded.item_id, true);
    buttons.current.get(bounded.item_id)?.focus();
  }

  return (
    <div ref={rootRef} className={`explorer ${split ? "explorer--split" : "explorer--stack"}`}>
      <ol className="explorer__list" aria-label="Слабые сигналы по рангу" onKeyDown={onKeyDown}>
        {items.map((item) => {
          const active = item.item_id === selected.item_id;
          const expanded = !split && active && open;
          return (
            <li key={item.item_id} className={`explorer__entry ${expanded ? "explorer__entry--open" : ""}`}>
              <button
                ref={(node) => {
                  if (node) buttons.current.set(item.item_id, node);
                  else buttons.current.delete(item.item_id);
                }}
                type="button"
                className={`signal-row ${active ? "signal-row--active" : ""}`}
                tabIndex={active ? 0 : -1}
                aria-current={split && active ? "true" : undefined}
                aria-expanded={split ? undefined : expanded}
                onClick={() => choose(item.item_id, false)}
              >
                <span className="signal-row__rank">{item.rank}</span>
                <span className="signal-row__title">{item.title_ru}</span>
                <span className="signal-row__score">{percent(item.score)}</span>
                <ScoreBar score={item.score} />
                <span className="signal-row__meta">
                  <span className={`signal-row__band signal-row__band--${item.confidence_band.toLowerCase()}`}>
                    {translate(CONFIDENCE_BAND_RU, item.confidence_band)}
                  </span>
                  <span>{item.narrative_status === "GENERATED" ? "инсайт сгенерирован" : "инсайт экстрактивный"}</span>
                  <span>источников: {item.source_count}</span>
                </span>
                {split ? null : (
                  <span className="signal-row__chevron" aria-hidden="true">
                    <IconChevron size={18} />
                  </span>
                )}
              </button>
              {expanded ? (
                <div className="explorer__inline">
                  <SignalDetail item={item} jobId={jobId} total={items.length} />
                </div>
              ) : null}
            </li>
          );
        })}
      </ol>
      {split ? (
        <div ref={detailRef} className="explorer__detail">
          <SignalDetail key={selected.item_id} item={selected} jobId={jobId} total={items.length} />
        </div>
      ) : null}
    </div>
  );
}

/* ---------- исключённые кандидаты ---------- */

export function ExcludedPanel({ excluded }: { excluded: ExcludedCandidate[] }) {
  return (
    <div className="excluded">
      <p className="excluded__intro">
        Технологии, которые не попали в выдачу: зрелые решения со сформированным рынком,
        маркетинговый хайп, информационный шум и кандидаты не по теме запроса.
      </p>
      {excluded.length === 0 ? (
        <p className="note">Ни один кандидат не был исключён.</p>
      ) : (
        <div className="table-scroll">
          <table className="xtable">
            <thead>
              <tr>
                <th>Технология</th>
                <th>Решение</th>
                <th>Причина</th>
                <th className="num">Скоринг</th>
                <th className="num">Документов</th>
                <th>Объяснение</th>
              </tr>
            </thead>
            <tbody>
              {excluded.map((candidate) => (
                <tr key={candidate.candidate_id}>
                  <td className="xtable__name" data-label="Технология">{candidate.title_auto}</td>
                  <td data-label="Решение">
                    <span className={`verdict verdict--${candidate.decision.toLowerCase()}`}>
                      {translate(DECISION_RU, candidate.decision)}
                    </span>
                  </td>
                  <td className="xtable__reason" data-label="Причина">
                    {translate(DECISION_REASON_RU, candidate.decision_reason)}
                  </td>
                  <td className="num" data-label="Скоринг">{percent(candidate.score)}</td>
                  <td className="num" data-label="Документов">{candidate.document_count}</td>
                  <td className="xtable__why" data-label="Объяснение">{candidate.decision_explanation_ru}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

/* ---------- признаки модели ---------- */

export function FeatureTable({ features }: { features: Feature[] }) {
  if (features.length === 0) {
    return <p className="note">Признаки не переданы.</p>;
  }
  const scale = Math.max(...features.map((feature) => Math.abs(feature.contribution)), 1e-6);
  return (
    <div className="table-scroll">
      <table className="ftable">
        <thead>
          <tr>
            <th>Признак</th>
            <th className="num">Значение</th>
            <th>Вклад</th>
            <th>Направление</th>
          </tr>
        </thead>
        <tbody>
          {features.map((feature) => (
            <tr
              key={feature.feature_name}
              className={`ftable__row ftable__row--${toneOf(feature.direction)} ${
                feature.contribution < 0 ? "ftable__row--minus" : "ftable__row--plus"
              }`}
              style={cssVars({ "--share": (Math.abs(feature.contribution) / scale).toFixed(3) })}
            >
              <td className="ftable__feature" data-label="Признак">
                <span className="ftable__name">{feature.label_ru}</span>
                <code className="ftable__code">{feature.feature_name}</code>
              </td>
              <td className="num" data-label="Значение">{feature.value.toFixed(3).replace(".", ",")}</td>
              <td className="ftable__contribution" data-label="Вклад">
                <span className="ftable__track" aria-hidden="true">
                  <span className="ftable__bar" />
                </span>
                <span className="num">{signed(feature.contribution, 3)}</span>
              </td>
              <td data-label="Направление">
                <span className={`direction direction--${toneOf(feature.direction)}`}>
                  {translate(DIRECTION_RU, feature.direction)}
                </span>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/* ---------- источники ---------- */

export function SourceList({ sources, caseDocumentId }: {
  sources: Source[];
  caseDocumentId?: string;
}) {
  if (sources.length === 0) {
    return <p className="note">Источники не переданы.</p>;
  }
  return (
    <ol className="sources">
      {sources.map((source, index) => {
        const isCase = source.document_id === caseDocumentId;
        return (
          <li
            key={source.document_id}
            className={`source ${isCase ? "source--case" : ""}`}
            id={`source-${source.document_id}`}
          >
            <span className="source__index" aria-hidden="true">{index + 1}</span>
            <div className="source__body">
              <h4 className="source__title">
                {isSafeLink(source.url) ? (
                  <a href={source.url} target="_blank" rel="noreferrer noopener">
                    {source.title}
                    <IconExternal size={15} />
                  </a>
                ) : (
                  <span>{source.title}</span>
                )}
              </h4>
              {linkHost(source.url) ? <span className="source__host">{linkHost(source.url)}</span> : null}
              <div className="source__meta">
                <span className="chip">{translate(SOURCE_TYPE_RU, source.source_type)}</span>
                <span className={trustChipClass(source.trust_level)}>
                  {translate(TRUST_LEVEL_RU, source.trust_level)}
                </span>
                <span className="chip">Язык оригинала: {translate(LANGUAGE_RU, source.language_code)}</span>
                <span className="chip">{formatDate(source.published_at)}</span>
                {isCase ? <span className="chip chip--accent">Источник кейс-примера</span> : null}
              </div>
              <p className="source__summary">
                {source.summary_ru}{" "}
                <span className="source__mark">({summaryMark(source.summary_kind)})</span>
              </p>
              {source.snippet ? <p className="source__snippet">{source.snippet}</p> : null}
            </div>
          </li>
        );
      })}
    </ol>
  );
}
