/** Компоненты выдачи: ход задания, сводка, обозреватель сигналов, исключённые, признаки, источники. */

import { useEffect, useMemo, useRef, useState } from "react";
import type { KeyboardEvent } from "react";
import { Link } from "react-router-dom";
import type {
  Decision, EvidenceKind, ExcludedCandidate, Feature, JobProgress, JobStatus, ResultItemSummary, ResultStats, Source,
} from "../api/types";
import {
  CONFIDENCE_BAND_RU, CONFIDENT_SCORE, DECISION_RU, DIRECTION_RU, decisionReasonText, isConfident,
  JOB_STATUS_RU, LANGUAGE_RU, NARRATIVE_STATUS_RU, SOURCE_TYPE_RU, TRUST_LEVEL_RU,
  confidenceChipClass, formatDate, formatNumber, isSafeLink, jobProgressShare, linkHost,
  narrativeChipClass, percent, splitTitle, summaryMark, translate, trustChipClass,
} from "../lib/format";
import { cssVars } from "../lib/style";
import { rootFontSize, useSize } from "../lib/useSize";
import { IconArrowRight, IconChevron, IconExternal, IconFilter } from "./icons";
import { Progress, ScoreDial, Stat } from "./ui";

const SPLIT_MIN_REM = 40;
const RUBRIC_BATCH_SIZE = 20;
const MAX_BATCH_CELLS = 15;

/** Пояснение шкалы индекса уверенности. */
export const SCORE_HINT =
  "Индекс уверенности — шкала признанных сигналов; 75 % и выше — высокая уверенность. Подробнее — в «Методологии».";

function signed(value: number, digits = 2): string {
  const text = Math.abs(value).toFixed(digits).replace(".", ",");
  return value < 0 ? `−${text}` : `+${text}`;
}

function toneOf(direction: string): "signal" | "mature" | "neutral" {
  if (direction === "supports_weak_signal") return "signal";
  if (direction === "supports_mature") return "mature";
  return "neutral";
}

/** lang для нерусского текста. */
export function langOf(code?: string | null): string | undefined {
  return code && code !== "ru" ? code : undefined;
}

/** lang для латинского названия без кода языка. */
export function nameLang(text: string): string | undefined {
  return /[A-Za-z]/.test(text) && !/[А-Яа-яЁё]/.test(text) ? "en" : undefined;
}

/** Короткая подпись признака; полная — в подсказке. */
export function readableFeature(feature: Feature): { label: string; hint: string } {
  if (feature.feature_name.startsWith("sc_")) {
    const stem = feature.feature_name.slice(3);
    return { label: `«${stem}…»`, hint: `Основа слова в профиле технологии (${feature.feature_name})` };
  }
  const short = feature.label_ru
    .replace(/\s*\([^()]*\)\s*$/u, "")
    .replace(/:\s*\d\s*—.*$/u, "")
    .trim();
  return { label: short || feature.label_ru, hint: feature.label_ru };
}

/* ---------- ход задания ---------- */

function liveFactsText(status: JobStatus, done: number, total: number, progress?: JobProgress): string {
  if (!progress) return "";
  if (status === "COLLECTING") {
    const parts = [
      `документов: ${formatNumber(progress.documents_collected)}`,
      `запросов: ${formatNumber(progress.http_requests_total)}`,
      progress.sources_processed ? `источников: ${progress.sources_processed}` : "",
    ].filter(Boolean);
    return parts.join(" · ");
  }
  if (status === "ANALYZING") {
    return [
      `документов: ${formatNumber(progress.documents_collected)}`,
      `кандидатов: ${formatNumber(progress.candidates_found)}`,
    ].join(" · ");
  }
  if (status === "NARRATING") {
    return [
      progress.candidates_found ? `кандидатов: ${formatNumber(progress.candidates_found)}` : "",
      `готово инсайтов: ${done} из ${total || "—"}`,
    ]
      .filter(Boolean)
      .join(" · ");
  }
  return "";
}

export function JobProgressPanel({ status, done, total, progress, hint }: {
  status: JobStatus;
  done: number;
  total: number;
  progress?: JobProgress;
  hint?: string;
}) {
  const meta = hint ?? liveFactsText(status, done, total, progress);
  return (
    <div className="live">
      <div className="live__line">
        <span className="live__stage">
          <span className="live__pulse" aria-hidden="true" />
          {translate(JOB_STATUS_RU, status)}
        </span>
        {meta ? <span className="live__hint">{meta}</span> : null}
      </div>
      <Progress share={jobProgressShare(status, done, total)} label="Ход выполнения задания" />
    </div>
  );
}

/** Пакетная рубричная проверка кандидатов (по пачкам из 20 шт.) и синтез инсайтов на 4-м этапе (Зона 1, Вариант 1А). */
export function CandidateMatrixStage({
  candidatesFound,
  sourcesProcessed = 0,
  done,
  total,
  compact = false,
}: {
  candidatesFound: number;
  sourcesProcessed?: number;
  done: number;
  total: number;
  compact?: boolean;
}) {
  const totalCandidates = candidatesFound > 0 ? candidatesFound : 300;
  const batchCount = Math.min(MAX_BATCH_CELLS, Math.max(1, Math.ceil(totalCandidates / RUBRIC_BATCH_SIZE)));
  const perBatch = Math.max(1, Math.ceil(totalCandidates / batchCount));
  const minBatchFromProgress =
    total > 0 && done > 0 ? Math.min(batchCount, Math.floor((done / total) * batchCount)) : 0;

  const [activeBatch, setActiveBatch] = useState(minBatchFromProgress);

  useEffect(() => {
    setActiveBatch((prev) => Math.max(prev, minBatchFromProgress));
  }, [minBatchFromProgress]);

  useEffect(() => {
    const timer = window.setInterval(() => {
      if (document.documentElement.hasAttribute("data-motion-paused")) return;
      // Не сбрасываем индекс в 0 по модулю (%), чтобы анимация по кандидатам не запускалась по кругу:
      // проходим пакеты последовательно и переходим к финальной сборке карточек ТОП выдачи.
      setActiveBatch((prev) => (prev < batchCount ? prev + 1 : prev));
    }, 950);
    return () => window.clearInterval(timer);
  }, [batchCount]);

  const allBatchesChecked = activeBatch >= batchCount;
  const targetLabel = total > 0 ? `ТОП-${total}` : "ТОП выдачи";
  const metaParts = [
    sourcesProcessed > 0 ? `Источников: ${formatNumber(sourcesProcessed)}` : "",
    `Кандидатов: ${formatNumber(totalCandidates)}`,
    `Отбор в ${targetLabel}${done > 0 ? ` (готово: ${done})` : ""}`,
  ].filter(Boolean);

  return (
    <div
      className={`scope matrix-stage ${compact ? "matrix-stage--compact" : ""}`}
      role="status"
      aria-live="polite"
    >
      <div className="matrix-stage__head">
        <span className="matrix-stage__title">
          <span className="live__pulse" aria-hidden="true" />
          {allBatchesChecked
            ? `Пакеты кандидатов проверены — формируем итоговые карточки (${targetLabel})…`
            : "Рубричная проверка и синтез инсайтов"}
        </span>
        <span className="matrix-stage__meta">{metaParts.join(" · ")}</span>
      </div>
      <div className="matrix-stage__grid" aria-hidden="true">
        {Array.from({ length: batchCount }, (_, index) => {
          const from = index * perBatch + 1;
          const to = Math.min(totalCandidates, (index + 1) * perBatch);
          let stateClass = "";
          let statusText = "ожидает";
          if (index < activeBatch) {
            stateClass = "matrix-cell--signal";
            statusText = "✓ проверен";
          } else if (index === activeBatch) {
            stateClass = "matrix-cell--scan";
            statusText = "● проверка…";
          }
          return (
            <div key={index} className={`matrix-cell ${stateClass}`}>
              <strong className="matrix-cell__range">
                № {from}–{to}
              </strong>
              <span className="matrix-cell__status">{statusText}</span>
            </div>
          );
        })}
      </div>
      <div className="matrix-stage__legend">
        <span className="matrix-stage__legend-item">
          <span className="matrix-stage__swatch matrix-stage__swatch--signal" aria-hidden="true" />
          Бирюзовый — пакет проверен (найдены сигналы)
        </span>
        <span className="matrix-stage__legend-item">
          <span className="matrix-stage__swatch matrix-stage__swatch--scan" aria-hidden="true" />
          Фиолетовый — проверяется моделью сейчас
        </span>
        <span className="matrix-stage__legend-item">
          <span className="matrix-stage__swatch matrix-stage__swatch--drop" aria-hidden="true" />
          Тёмный — ожидает очереди
        </span>
      </div>
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
      <figcaption className="spectrum__caption">Индекс уверенности по рангу</figcaption>
      <div className="spectrum__plot" style={cssVars({ "--threshold": threshold })}>
        <span className="spectrum__line" aria-hidden="true">
          <span className="spectrum__line-label">75 %</span>
        </span>
        {items.map((item) => {
          const className = `spectrum__bar ${isConfident(item.score) ? "spectrum__bar--high" : ""} ${
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
          hint={`признаны: ${formatNumber(stats.weak_signals_total)}, в выдаче после проверки: ${formatNumber(items.length)}`}
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
          value={formatNumber(stats.sources_processed)}
          hint={`${formatNumber(stats.http_requests_total)} запросов, ${formatNumber(stats.documents_collected)} документов`}
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
      className={`score-bar ${isConfident(score) ? "score-bar--high" : ""}`}
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
        {features.map((feature) => {
          const text = readableFeature(feature);
          return (
            <li
              key={feature.feature_name}
              className={`predictor predictor--${toneOf(feature.direction)} ${
                feature.contribution < 0 ? "predictor--minus" : "predictor--plus"
              }`}
              style={cssVars({ "--share": (Math.abs(feature.contribution) / scale).toFixed(3) })}
              title={text.hint}
            >
              <span className="predictor__label">{text.label}</span>
              <span className="predictor__track" aria-hidden="true">
                <span className="predictor__bar" />
              </span>
              <span className="predictor__value">
                {signed(feature.contribution)}
                <span className="visually-hidden">, {translate(DIRECTION_RU, feature.direction)}</span>
              </span>
            </li>
          );
        })}
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

/** Этап 5.1: ранние исследования — тоже слабые сигналы; пометка показывает, на чём держится сигнал. */
const EVIDENCE_KIND_RU: Record<EvidenceKind, string> = {
  market: "Рынок и практика",
  research: "Исследование",
  patent: "Патент",
};

function SignalDetail({ item, jobId, total }: { item: ResultItemSummary; jobId: string; total: number }) {
  const [title, original] = splitTitle(item.title_ru);
  return (
    <article className="detail">
      <header className="detail__head">
        <div className="detail__heading">
          <span className="detail__rank">
            Ранг {item.rank} из {total}
          </span>
          <h2 className="detail__title">{title}</h2>
          {original ? <span className="detail__orig" lang="en">{original}</span> : null}
        </div>
        <Link className="btn btn--primary detail__cta" to={`/insight/${item.item_id}?job=${jobId}`}>
          Открыть инсайт
          <IconArrowRight size={18} />
        </Link>
      </header>
      <div className="detail__score">
        <ScoreDial value={item.score} caption="индекс уверенности" hint={SCORE_HINT} />
        <div className="detail__chips">
          <span className={confidenceChipClass(item.confidence_band)}>
            {translate(CONFIDENCE_BAND_RU, item.confidence_band)}
          </span>
          {item.narrative_status === "GENERATED" ? null : (
            <span className={narrativeChipClass(item.narrative_status)}>
              {translate(NARRATIVE_STATUS_RU, item.narrative_status)}
            </span>
          )}
          <span className="chip">Источников: {item.source_count}</span>
          {item.evidence_kind ? <span className="chip">{EVIDENCE_KIND_RU[item.evidence_kind]}</span> : null}
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
          const [title, original] = splitTitle(item.title_ru);
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
                <span className="signal-row__title">{title}</span>
                {original ? <span className="signal-row__orig" lang="en">{original}</span> : null}
                <span className="signal-row__score">{percent(item.score)}</span>
                <ScoreBar score={item.score} />
                <span className="signal-row__meta">
                  <span className={`signal-row__band signal-row__band--${item.confidence_band.toLowerCase()}`}>
                    {translate(CONFIDENCE_BAND_RU, item.confidence_band)}
                  </span>
                  {item.narrative_status === "GENERATED" ? null : (
                    <span className="signal-row__flag">инсайт экстрактивный</span>
                  )}
                  <span>источников: {item.source_count}</span>
                  {item.evidence_kind ? <span>{EVIDENCE_KIND_RU[item.evidence_kind].toLowerCase()}</span> : null}
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

const DECISION_ORDER: Decision[] = ["OFF_TOPIC", "MATURE", "HYPE_OR_NOISE", "INSUFFICIENT_EVIDENCE"];

/** Исключённые: фильтр по решению и список. */
export function ExcludedPanel({ excluded }: { excluded: ExcludedCandidate[] }) {
  const counts = useMemo(() => {
    const byDecision = new Map<string, number>();
    excluded.forEach((candidate) => byDecision.set(candidate.decision, (byDecision.get(candidate.decision) ?? 0) + 1));
    return [...byDecision.entries()].sort(
      ([a], [b]) => DECISION_ORDER.indexOf(a as Decision) - DECISION_ORDER.indexOf(b as Decision),
    );
  }, [excluded]);
  const [filter, setFilter] = useState<string>("ALL");
  const [openId, setOpenId] = useState<string | null>(null);

  if (excluded.length === 0) {
    return (
      <div className="excluded">
        <p className="note">Ни один кандидат не был исключён.</p>
      </div>
    );
  }
  const shown = filter === "ALL" ? excluded : excluded.filter((candidate) => candidate.decision === filter);

  return (
    <div className="excluded">
      <p className="excluded__intro">
        Кандидаты, которые не попали в выдачу: зрелые решения, хайп, шум и темы не по запросу. Нажмите на
        строку, чтобы прочитать объяснение.
      </p>
      <div className="excluded__filters" role="group" aria-label="Фильтр по решению">
        <button
          type="button"
          className={`fchip ${filter === "ALL" ? "fchip--on" : ""}`}
          aria-pressed={filter === "ALL"}
          onClick={() => setFilter("ALL")}
        >
          Все <span className="fchip__count">{excluded.length}</span>
        </button>
        {counts.map(([decision, count]) => (
          <button
            key={decision}
            type="button"
            className={`fchip ${filter === decision ? "fchip--on" : ""}`}
            aria-pressed={filter === decision}
            onClick={() => setFilter(decision)}
          >
            {translate(DECISION_RU, decision)} <span className="fchip__count">{count}</span>
          </button>
        ))}
      </div>
      <ul className="xlist">
        {shown.map((candidate) => {
          const open = openId === candidate.candidate_id;
          return (
            <li key={candidate.candidate_id} className={`xlist__item ${open ? "xlist__item--open" : ""}`}>
              <button
                type="button"
                className="xlist__row"
                aria-expanded={open}
                onClick={() => setOpenId(open ? null : candidate.candidate_id)}
              >
                <span className="xlist__name" lang={nameLang(candidate.title_auto)}>
                  {candidate.title_auto}
                </span>
                <span className={`verdict verdict--${candidate.decision.toLowerCase()}`}>
                  {translate(DECISION_RU, candidate.decision)}
                </span>
                <span className="xlist__reason">{decisionReasonText(candidate.decision_reason)}</span>
                <span className="xlist__score">
                  <span className="visually-hidden">Скоринг: </span>
                  {percent(candidate.score)}
                </span>
                <span className="xlist__docs">{candidate.document_count} док.</span>
                <span className="xlist__chevron" aria-hidden="true">
                  <IconChevron size={16} />
                </span>
              </button>
              {open ? <p className="xlist__why">{candidate.decision_explanation_ru}</p> : null}
            </li>
          );
        })}
      </ul>
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
        const lang = langOf(source.language_code);
        return (
          <li
            key={source.document_id}
            className={`source ${isCase ? "source--case" : ""}`}
            id={`source-${source.document_id}`}
          >
            <span className="source__index" aria-hidden="true">{index + 1}</span>
            <div className="source__body">
              <h3 className="source__title" lang={lang}>
                {isSafeLink(source.url) ? (
                  <a href={source.url} target="_blank" rel="noreferrer noopener">
                    {source.title}
                    <IconExternal size={15} />
                  </a>
                ) : (
                  <span>{source.title}</span>
                )}
              </h3>
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
              {source.snippet ? (
                <details className="source__more">
                  <summary>Фрагмент оригинала</summary>
                  <p className="source__snippet" lang={lang}>{source.snippet}</p>
                </details>
              ) : null}
            </div>
          </li>
        );
      })}
    </ol>
  );
}
