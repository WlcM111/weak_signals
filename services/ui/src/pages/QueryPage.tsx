/** Страница 1 «Запрос»: ввод открытого запроса и список последних заданий (§7 HANDOFF_UI). */

import { useEffect, useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { ApiCallError, api } from "../api/client";
import { JOB_STATUS_RU, STAGE_HINT_RU, formatDate, isTerminal, translate } from "../lib/format";
import { Banner, EmptyState, SkeletonList, StatusPill } from "../components/ui";
import { QueryForm } from "../components/QueryForm";
import { SignalField } from "../components/SignalField";

const STAGES = ["QUEUED", "COLLECTING", "ANALYZING", "NARRATING"];

export default function QueryPage() {
  const jobs = useQuery({
    queryKey: ["jobs"],
    queryFn: () => api.listJobs(20),
    refetchInterval: 5000,
  });

  useEffect(() => {
    document.title = "Новый запрос — Слабые сигналы";
  }, []);

  const runningCount = useMemo(
    () => (jobs.data?.items ?? []).filter((job) => !isTerminal(job.status)).length,
    [jobs.data],
  );

  return (
    <div className="page query">
      <section className="query__compose panel panel--raised" aria-labelledby="query-heading">
        <header className="query__head">
          <h1 id="query-heading" className="page-title">Найти слабые сигналы</h1>
          <p className="page-lead">
            Опишите направление своими словами. Система соберёт публикации, патенты, код и отраслевые новости,
            отсеет зрелые технологии и шум и покажет ранние технологии с объяснением каждого решения.
          </p>
        </header>

        <QueryForm running={runningCount} />

        <figure className="scope query__scope">
          <div className="scope__screen">
            <SignalField />
          </div>
          <figcaption className="scope__title">Этапы выполнения запроса</figcaption>
          <ol className="stages">
            {STAGES.map((stage, index) => (
              <li key={stage} className="stages__item">
                <span className="stages__index" aria-hidden="true">
                  {index + 1}
                </span>
                <strong className="stages__name">{translate(JOB_STATUS_RU, stage)}</strong>
                <span className="stages__hint">{translate(STAGE_HINT_RU, stage)}</span>
              </li>
            ))}
          </ol>
        </figure>
      </section>

      <section className="query__jobs panel" aria-labelledby="jobs-heading">
        <div className="panel__head">
          <h2 id="jobs-heading" className="panel__title">
            Последние задания
          </h2>
          {jobs.data ? <span className="panel__count">{jobs.data.items.length}</span> : null}
        </div>
        <div className="panel__scroll">
          {jobs.isLoading ? (
            <SkeletonList rows={4} />
          ) : jobs.isError ? (
            <Banner kind="error" title="Список заданий недоступен">
              {jobs.error instanceof ApiCallError ? jobs.error.message : "Повторите попытку позже"}
            </Banner>
          ) : (jobs.data?.items.length ?? 0) === 0 ? (
            <EmptyState title="Заданий пока нет" hint="Отправьте первый запрос — он появится в этом списке" />
          ) : (
            <ul className="job-list">
              {jobs.data?.items.map((job) => (
                <li key={job.job_id}>
                  <Link className="job-row" to={`/results/${job.job_id}`}>
                    <span className="job-row__query">{job.query_text}</span>
                    <span className="job-row__meta">
                      <StatusPill status={job.status} />
                      <span className="job-row__date">{formatDate(job.created_at, true)}</span>
                    </span>
                  </Link>
                </li>
              ))}
            </ul>
          )}
        </div>
      </section>
    </div>
  );
}
