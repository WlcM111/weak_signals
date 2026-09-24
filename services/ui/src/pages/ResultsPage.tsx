/** Страница 2 «Результаты»: ход задания, сводка, ТОП-N сигналов и исключённые кандидаты как единый объект. */

import { useEffect, useRef, useState } from "react";
import type { KeyboardEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import { ApiCallError, api } from "../api/client";
import type { Results } from "../api/types";
import { JOB_STATUS_RU, isTerminal, jobErrorText, partialReasons, translate } from "../lib/format";
import { Banner, EmptyState, SkeletonList, StatusPill } from "../components/ui";
import { ExcludedPanel, JobProgressPanel, SignalExplorer, StatsRow } from "../components/signals";
import { RunTimeline } from "../components/timeline";
import { SignalField } from "../components/SignalField";
import { IconPlus, IconStop } from "../components/icons";
import { scrollBehavior } from "../lib/style";

const POLL_MS = 3000;
const RESULTS_READY_STATUSES = new Set(["NARRATING", "COMPLETED", "PARTIAL", "CANCELLED"]);

type Tab = "signals" | "excluded";

export default function ResultsPage() {
  const { jobId = "" } = useParams();
  const queryClient = useQueryClient();
  const [tab, setTab] = useState<Tab>("signals");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const tabsRef = useRef<HTMLDivElement | null>(null);
  const tabButtons = useRef<Record<Tab, HTMLButtonElement | null>>({ signals: null, excluded: null });

  const job = useQuery({
    queryKey: ["job", jobId],
    queryFn: () => api.getJob(jobId),
    refetchInterval: (query) => (isTerminal(query.state.data?.status) ? false : POLL_MS),
    enabled: Boolean(jobId),
  });

  const status = job.data?.status;
  const resultsReady = Boolean(status && RESULTS_READY_STATUSES.has(status));

  const results = useQuery<Results>({
    queryKey: ["results", jobId],
    queryFn: () => api.getResults(jobId),
    enabled: resultsReady,
    refetchInterval: () => (isTerminal(status) ? false : POLL_MS),
  });

  // Снимок результата дописывается на стадии finalize — уже после того, как статус стал
  // терминальным и опрос выключился. Без этого экран остаётся с промежуточными данными.
  useEffect(() => {
    if (isTerminal(status)) {
      void results.refetch();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status]);

  const cancel = useMutation({
    mutationFn: () => api.cancelJob(jobId),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["job", jobId] }),
  });

  useEffect(() => {
    document.title = job.data ? `${job.data.query_text} — результаты` : "Результаты";
  }, [job.data]);

  function openTab(next: Tab, reveal = false) {
    setTab(next);
    if (reveal) {
      requestAnimationFrame(() => tabsRef.current?.scrollIntoView({ behavior: scrollBehavior(), block: "nearest" }));
    }
  }

  function pickSignal(itemId: string) {
    setSelectedId(itemId);
    openTab("signals", true);
  }

  function onTabsKey(event: KeyboardEvent<HTMLDivElement>) {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    if (!results.data) return;
    event.preventDefault();
    const next: Tab = tab === "signals" ? "excluded" : "signals";
    setTab(next);
    tabButtons.current[next]?.focus();
  }

  if (job.isLoading) {
    return (
      <div className="page page--narrow">
        <SkeletonList rows={4} />
      </div>
    );
  }
  if (job.isError) {
    return (
      <div className="page page--narrow">
        <Banner kind="error" title="Задание недоступно">
          {job.error instanceof ApiCallError ? job.error.message : "Повторите попытку позже"}
        </Banner>
      </div>
    );
  }

  const data = job.data!;
  const terminal = isTerminal(data.status);
  const partial = partialReasons(data.error?.message ?? null);
  const items = results.data?.items ?? [];
  const excluded = results.data?.excluded ?? [];
  const activeId = items.find((item) => item.item_id === selectedId)?.item_id ?? items[0]?.item_id;

  return (
    <div className={`page results ${terminal ? "results--done" : "results--running"}`}>
      <header className="results__head">
        <div className="results__query">
          <span className="eyebrow">Поисковый запрос</span>
          <h1 className="results__title" title={data.query_text}>
            {data.query_text}
          </h1>
          <div className="results__meta">
            <StatusPill status={data.status} />
            {data.attempt > 0 ? <span className="chip">попытка № {data.attempt + 1}</span> : null}
          </div>
        </div>
        <div className="results__actions">
          <Link className="btn btn--ghost" to="/search">
            <IconPlus size={18} />
            Новый запрос
          </Link>
          {!terminal ? (
            <button
              type="button"
              className="btn btn--danger"
              disabled={cancel.isPending || data.cancel_requested}
              onClick={() => cancel.mutate()}
            >
              <IconStop size={18} />
              {data.cancel_requested ? "Отмена запрошена" : "Отменить задание"}
            </button>
          ) : null}
        </div>
        {!terminal ? (
          <div className="results__live">
            <JobProgressPanel
              status={data.status}
              done={data.progress.narratives_done}
              total={data.progress.narratives_total}
            />
          </div>
        ) : null}
      </header>

      <aside className="results__run" aria-label="Сводка по заданию">
        <div className="run">
          {results.data ? (
            <StatsRow
              stats={results.data.stats}
              items={items}
              activeId={activeId}
              onShowExcluded={() => openTab("excluded", true)}
              onPick={pickSignal}
            />
          ) : null}

          {data.status === "FAILED" ? (
            <Banner kind="error" title="Задание завершилось ошибкой">
              {jobErrorText(data.error)}
            </Banner>
          ) : null}

          {data.status === "PARTIAL" && partial.length > 0 ? (
            <Banner kind="warn" title="Результат неполный">
              <ul className="banner__list">
                {partial.map((reason) => (
                  <li key={reason}>{reason}</li>
                ))}
              </ul>
            </Banner>
          ) : null}

          <details className="run__log" open={!terminal || !results.data || items.length === 0}>
            <summary className="run__summary">
              Ход задания
              <span className="run__summary-state">{translate(JOB_STATUS_RU, data.status)}</span>
            </summary>
            <RunTimeline job={data} stats={results.data?.stats} />
          </details>
        </div>
      </aside>

      <section className="results__body" aria-label="Выдача">
        <div className="tabs" role="tablist" aria-label="Выдача по заданию" ref={tabsRef} onKeyDown={onTabsKey}>
          <button
            ref={(node) => {
              tabButtons.current.signals = node;
            }}
            type="button"
            role="tab"
            id="tab-signals"
            aria-controls="panel-signals"
            aria-selected={tab === "signals"}
            tabIndex={tab === "signals" ? 0 : -1}
            className="tabs__tab"
            onClick={() => openTab("signals")}
          >
            Найденные слабые сигналы
            {results.data ? <span className="tabs__count">{items.length}</span> : null}
          </button>
          <button
            ref={(node) => {
              tabButtons.current.excluded = node;
            }}
            type="button"
            role="tab"
            id="tab-excluded"
            aria-controls="panel-excluded"
            aria-selected={tab === "excluded"}
            tabIndex={tab === "excluded" ? 0 : -1}
            className="tabs__tab"
            disabled={!results.data}
            onClick={() => openTab("excluded")}
          >
            Исключённые кандидаты
            {results.data ? <span className="tabs__count">{excluded.length}</span> : null}
          </button>
        </div>

        <div
          className="results__panel"
          role="tabpanel"
          id="panel-signals"
          aria-labelledby="tab-signals"
          hidden={tab !== "signals"}
        >
          {!resultsReady ? (
            terminal ? (
              <EmptyState
                title="Выдача не сформирована"
                hint={`Задание завершилось со статусом «${translate(JOB_STATUS_RU, data.status)}» до этапа подготовки инсайтов. ${jobErrorText(data.error)}`}
                action={
                  <Link className="btn btn--ghost" to="/search">
                    <IconPlus size={18} />
                    Новый запрос
                  </Link>
                }
              />
            ) : (
              <div className="waiting">
                <div className="scope waiting__scope">
                  <SignalField mode="scan" />
                </div>
                <p className="waiting__text">
                  Выдача появится, как только начнётся формирование инсайтов. Страница обновляется
                  автоматически каждые 3 секунды — обновлять вручную не нужно.
                </p>
              </div>
            )
          ) : results.isLoading ? (
            <SkeletonList rows={4} />
          ) : items.length === 0 ? (
            <EmptyState
              title="Слабых сигналов пока нет"
              hint={
                isTerminal(data.status)
                  ? "По этому запросу ни один кандидат не прошёл правила исключения. Причины — на вкладке исключённых кандидатов."
                  : "Инсайты ещё формируются — сигналы появятся здесь по мере готовности"
              }
              action={
                isTerminal(data.status) && excluded.length > 0 ? (
                  <button type="button" className="btn btn--ghost" onClick={() => openTab("excluded")}>
                    Показать исключённых
                  </button>
                ) : undefined
              }
            />
          ) : (
            <SignalExplorer items={items} jobId={jobId} selectedId={activeId} onSelect={setSelectedId} />
          )}
        </div>

        <div
          className="results__panel results__panel--excluded"
          role="tabpanel"
          id="panel-excluded"
          aria-labelledby="tab-excluded"
          hidden={tab !== "excluded"}
        >
          {results.data ? <ExcludedPanel excluded={excluded} /> : null}
        </div>
      </section>
    </div>
  );
}
