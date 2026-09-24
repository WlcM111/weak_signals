/** Стартовая страница: назначение системы, прибор с показаниями модели, возможности и последние запросы. */

import { useEffect } from "react";
import type { ComponentType } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import { formatDate } from "../lib/format";
import { SignalField } from "../components/SignalField";
import { EmptyState, SkeletonList, StatusPill } from "../components/ui";
import {
  IconArrowRight, IconBook, IconCheck, IconFilter, IconModel, IconSearch, IconSource,
} from "../components/icons";

const CAPABILITIES: { title: string; text: string; Icon: ComponentType<{ size?: number }> }[] = [
  {
    title: "Сбор по открытым источникам",
    text: "Научные публикации, препринты, патенты, репозитории кода, отраслевые медиа и вакансии — с соблюдением лимитов каждого источника.",
    Icon: IconSource,
  },
  {
    title: "Отделение зрелого от раннего",
    text: "Восемь детерминированных правил отсекают массово внедрённые технологии, отраслевые стандарты, маркетинговый хайп и информационный шум.",
    Icon: IconFilter,
  },
  {
    title: "Объяснимая модель",
    text: "25 интерпретируемых признаков и калиброванная вероятность: по каждому решению видно, какие признаки и с каким вкладом на него повлияли.",
    Icon: IconModel,
  },
  {
    title: "Проверяемые источники",
    text: "У каждого сигнала — наименование, ссылка, дата, тип, язык оригинала и уровень доверенности; иноязычные материалы снабжены русским резюме с пометкой.",
    Icon: IconCheck,
  },
];

function metricText(value: number | string | undefined, digits = 3): string {
  if (typeof value !== "number") return value === undefined ? "—" : String(value);
  return value.toFixed(digits).replace(".", ",");
}

export default function WelcomePage() {
  useEffect(() => {
    document.title = "Слабые сигналы — поиск зарождающихся технологий";
  }, []);

  const model = useQuery({ queryKey: ["model"], queryFn: () => api.getModel(), retry: 0 });
  const jobs = useQuery({ queryKey: ["jobs"], queryFn: () => api.listJobs(5), retry: 0 });
  const lastJob = jobs.data?.items?.[0];
  const metrics = model.data?.metrics ?? {};

  const readings = [
    { label: "Признаков", value: model.data ? String(model.data.feature_names.length) : "—" },
    { label: "ROC-AUC", value: model.data ? metricText(metrics.roc_auc) : "—" },
    { label: "F1-мера", value: model.data ? metricText(metrics.f1) : "—" },
    { label: "Порог решения", value: model.data ? metricText(metrics.threshold) : "—" },
  ];

  return (
    <div className="page welcome">
      <section className="welcome__intro">
        <span className="eyebrow">Аналитическая панель слабых сигналов</span>
        <h1 className="welcome__title">Находим технологию до того, как о ней заговорят все</h1>
        <p className="welcome__lead">
          Система принимает запрос в свободной форме, собирает материалы по открытым источникам,
          отделяет зрелые решения и маркетинговый шум и формирует ранжированную выдачу
          зарождающихся научно-технологических трендов с объяснением каждого решения.
        </p>
        <div className="welcome__actions">
          <Link className="btn btn--primary btn--lg" to="/search">
            <IconSearch size={20} />
            Начать поиск
          </Link>
          {lastJob ? (
            <Link className="btn btn--ghost btn--lg" to={`/results/${lastJob.job_id}`}>
              Открыть последний результат
            </Link>
          ) : null}
        </div>
        <nav className="welcome__links" aria-label="О системе">
          <Link className="text-link" to="/methodology">
            <IconBook size={18} />
            Как это работает
          </Link>
          <Link className="text-link" to="/model">
            <IconModel size={18} />
            Модель и метрики
          </Link>
        </nav>
      </section>

      <figure className="scope welcome__scope">
        <div className="scope__screen">
          <SignalField label="порог модели" />
        </div>
        <dl className="scope__readings">
          {readings.map((reading) => (
            <div key={reading.label} className="scope__reading">
              <dt>{reading.label}</dt>
              <dd>{reading.value}</dd>
            </div>
          ))}
        </dl>
        <figcaption className="scope__caption">
          {model.isSuccess ? (
            <>
              Активная модель {model.data.model_version_id}, обучена {formatDate(model.data.trained_at)} на
              наборе {model.data.dataset_version}. Эмбеддер {model.data.embedding_model}, схема признаков{" "}
              {model.data.feature_schema_version}.
            </>
          ) : model.isError ? (
            "Сведения о модели недоступны: проверьте, запущены ли сервисы"
          ) : (
            "Проверяем готовность сервисов…"
          )}
        </figcaption>
      </figure>

      <section className="welcome__capabilities" aria-label="Возможности системы">
        {CAPABILITIES.map(({ title, text, Icon }) => (
          <div key={title} className="capability">
            <span className="capability__icon">
              <Icon size={22} />
            </span>
            <h2 className="capability__title">{title}</h2>
            <p className="capability__text">{text}</p>
          </div>
        ))}
      </section>

      <section className="welcome__recent panel">
        <div className="panel__head">
          <h2 className="panel__title">Последние запросы</h2>
          <Link className="text-link text-link--quiet" to="/search">
            Все задания
            <IconArrowRight size={16} />
          </Link>
        </div>
        {jobs.isLoading ? (
          <SkeletonList rows={3} />
        ) : jobs.isSuccess && jobs.data.items.length > 0 ? (
          <ul className="job-list">
            {jobs.data.items.slice(0, 5).map((job) => (
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
        ) : (
          <EmptyState
            title={jobs.isError ? "Список заданий недоступен" : "Запросов пока нет"}
            hint={jobs.isError ? "Проверьте, запущены ли сервисы" : "Первый запрос появится здесь сразу после отправки"}
          />
        )}
      </section>
    </div>
  );
}
