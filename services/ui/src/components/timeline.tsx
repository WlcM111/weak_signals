/** Журнал выполнения задания: реальные стадии конвейера и машинные данные по каждой. */

import type { Job, ResultStats } from "../api/types";
import { formatDate, formatNumber, isTerminal } from "../lib/format";
import { IconAlert, IconCheck } from "./icons";

type StageState = "pending" | "running" | "success" | "error" | "cancelled";

interface Stage {
  key: string;
  title: string;
  state: StageState;
  facts: string[];
}

const STATE_RU: Record<StageState, string> = {
  pending: "ожидает",
  running: "выполняется",
  success: "выполнено",
  error: "ошибка",
  cancelled: "отменено",
};

const ORDER = ["QUEUED", "COLLECTING", "ANALYZING", "NARRATING"];

function stateFor(index: number, job: Job): StageState {
  const current = ORDER.indexOf(job.status);
  if (job.status === "CANCELLED") return index === 0 ? "success" : "cancelled";
  if (job.status === "FAILED") {
    const failedAt = Math.max(0, ORDER.length - 1);
    if (index < failedAt) return "success";
    return index === failedAt ? "error" : "pending";
  }
  if (isTerminal(job.status)) return "success";
  if (current === -1) return "pending";
  if (index < current) return "success";
  if (index === current) return "running";
  return "pending";
}

function duration(from?: string | null, to?: string | null): string | null {
  if (!from || !to) return null;
  const ms = new Date(to).getTime() - new Date(from).getTime();
  if (!Number.isFinite(ms) || ms < 0) return null;
  return ms < 60_000 ? `${Math.round(ms / 1000)} с` : `${Math.round(ms / 60_000)} мин`;
}

export function RunTimeline({ job, stats }: { job: Job; stats?: ResultStats }) {
  const progress = job.progress;
  const total = duration(job.started_at, job.finished_at);
  const stages: Stage[] = [
    {
      key: "queued",
      title: "Приём запроса и постановка в очередь",
      state: stateFor(0, job),
      facts: [
        job.created_at ? `создано ${formatDate(job.created_at, true)}` : "",
        job.attempt > 0 ? `попытка № ${job.attempt + 1}` : "",
      ].filter(Boolean),
    },
    {
      key: "collecting",
      title: "Сбор документов из открытых источников",
      state: stateFor(1, job),
      facts: [
        `документов: ${formatNumber(progress.documents_collected)}`,
        `запросов к источникам: ${formatNumber(progress.http_requests_total)}`,
        progress.sources_processed ? `источников отработало: ${progress.sources_processed}` : "",
        total ? `всего по заданию: ${total}` : "",
      ].filter(Boolean),
    },
    {
      key: "analyzing",
      title: "Признаки, правила исключения и модель",
      state: stateFor(2, job),
      facts: [
        `кандидатов найдено: ${formatNumber(progress.candidates_found)}`,
        stats ? `признаны слабыми сигналами: ${formatNumber(stats.weak_signals_total)}` : "",
        stats?.model_version_id ? `модель ${stats.model_version_id}` : "",
      ].filter(Boolean),
    },
    {
      key: "narrating",
      title: "Формирование инсайтов и резюме источников",
      state: stateFor(3, job),
      facts: [
        `готово: ${progress.narratives_done} из ${progress.narratives_total || "—"}`,
        stats?.narratives_generated !== undefined
          ? `сгенерировано моделью: ${formatNumber(stats.narratives_generated)}`
          : "",
        stats?.narratives_fallback ? `экстрактивных: ${formatNumber(stats.narratives_fallback)}` : "",
      ].filter(Boolean),
    },
  ];

  return (
    <ol className="pipeline" aria-label="Ход выполнения задания">
      {stages.map((stage, index) => (
        <li key={stage.key} className={`pipeline__stage pipeline__stage--${stage.state}`}>
          <span className="pipeline__marker" aria-hidden="true">
            {stage.state === "success" ? <IconCheck size={16} /> : index + 1}
          </span>
          <div className="pipeline__body">
            <strong className="pipeline__title">{stage.title}</strong>
            <span className="pipeline__state">{STATE_RU[stage.state]}</span>
            {stage.facts.length > 0 ? (
              <ul className="pipeline__facts">
                {stage.facts.map((fact) => (
                  <li key={fact}>{fact}</li>
                ))}
              </ul>
            ) : (
              <p className="pipeline__empty">данных по этапу пока нет</p>
            )}
          </div>
        </li>
      ))}
      {job.error ? (
        <li className="pipeline__stage pipeline__stage--error pipeline__stage--reason">
          <span className="pipeline__marker" aria-hidden="true">
            <IconAlert size={18} />
          </span>
          <div className="pipeline__body">
            <strong className="pipeline__title">Причина остановки</strong>
            <p className="pipeline__reason">
              {job.error.code}: {job.error.message}
            </p>
          </div>
        </li>
      ) : null}
    </ol>
  );
}
