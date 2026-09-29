/** Страница 1 «Запрос»: ввод открытого запроса и список последних заданий (§7 HANDOFF_UI). */

import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate } from "react-router-dom";
import { ApiCallError, api } from "../api/client";
import { JOB_STATUS_RU, STAGE_HINT_RU, formatDate, isTerminal, translate } from "../lib/format";
import { cssVars } from "../lib/style";
import { Banner, EmptyState, SkeletonList, StatusPill } from "../components/ui";
import { SignalField } from "../components/SignalField";
import { IconSearch } from "../components/icons";

const MIN_LENGTH = 2;
const MAX_LENGTH = 500;
const TOP_MIN = 1;
const TOP_MAX = 50;
const STAGES = ["QUEUED", "COLLECTING", "ANALYZING", "NARRATING"];
const EXAMPLES = [
  "слабые сигналы в области кибербезопасности",
  "перспективные решения в финтехе",
  "технологии в промышленном ИИ",
  "зарождающиеся направления в робототехнике",
];

export default function QueryPage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [text, setText] = useState("");
  const [topN, setTopN] = useState(15);
  const [idempotencyKey, setIdempotencyKey] = useState(() => crypto.randomUUID());

  const jobs = useQuery({
    queryKey: ["jobs"],
    queryFn: () => api.listJobs(20),
    refetchInterval: 5000,
  });

  const submit = useMutation({
    mutationFn: () => api.createQuery(text.trim(), topN, idempotencyKey),
    onSuccess: (accepted) => {
      setIdempotencyKey(crypto.randomUUID());
      queryClient.invalidateQueries({ queryKey: ["jobs"] });
      navigate(`/results/${accepted.job_id}`);
    },
  });

  const length = text.trim().length;
  const valid = length >= MIN_LENGTH && length <= MAX_LENGTH;
  const error = submit.error instanceof ApiCallError ? submit.error : null;

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
            Введите технологическое направление в свободной форме. Система соберёт публикации,
            препринты, патенты, репозитории кода и отраслевые новости из открытых источников, отделит зрелые
            технологии и маркетинговый шум и сформирует ранжированную выдачу с объяснением каждого
            решения.
          </p>
        </header>

        <div className="field">
          <label className="field__label" htmlFor="query">
            Технологическое направление
          </label>
          <textarea
            id="query"
            className="textarea query__input"
            value={text}
            maxLength={MAX_LENGTH + 50}
            placeholder="например: слабые сигналы в области кибербезопасности"
            onChange={(event) => setText(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && (event.metaKey || event.ctrlKey) && valid) {
                submit.mutate();
              }
            }}
          />
          <div className="field__foot">
            <span className={`counter ${length > MAX_LENGTH ? "counter--over" : ""}`}>
              {length} / {MAX_LENGTH} символов
            </span>
            <span className="field__hint">
              <kbd>Ctrl</kbd> + <kbd>Enter</kbd> — отправить
            </span>
          </div>
        </div>

        <div className="examples">
          <span className="examples__label" id="examples-label">
            Примеры запросов:
          </span>
          <div className="examples__list" role="group" aria-labelledby="examples-label">
            {EXAMPLES.map((example) => (
              <button
                key={example}
                type="button"
                className={`example ${text === example ? "example--active" : ""}`}
                onClick={() => setText(example)}
              >
                {example}
              </button>
            ))}
          </div>
        </div>

        <div className="query__controls">
          <div className="range-field">
            <div className="range-field__head">
              <label className="field__label" htmlFor="top-n">
                Размер выдачи
              </label>
              <output className="range-field__value" htmlFor="top-n">
                ТОП-{topN}
              </output>
            </div>
            <input
              id="top-n"
              className="range"
              type="range"
              min={TOP_MIN}
              max={TOP_MAX}
              value={topN}
              style={cssVars({ "--fill": `${((topN - TOP_MIN) / (TOP_MAX - TOP_MIN)) * 100}%` })}
              onChange={(event) => setTopN(Number(event.target.value))}
            />
            <span className="field__hint">По умолчанию ТОП-15, допустимо 1…50</span>
          </div>

          <div className="query__submit">
            <button
              type="button"
              className="btn btn--primary btn--lg"
              disabled={!valid || submit.isPending}
              onClick={() => submit.mutate()}
            >
              <IconSearch size={20} />
              {submit.isPending ? "Отправляем…" : "Найти слабые сигналы"}
            </button>
            {runningCount > 0 ? (
              <span className="query__running">
                <span className="query__running-dot" aria-hidden="true" />
                Выполняется заданий: {runningCount}
              </span>
            ) : null}
          </div>
        </div>

        {error ? (
          <Banner kind="error" title="Не удалось создать задание">
            {error.message}
          </Banner>
        ) : null}

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
