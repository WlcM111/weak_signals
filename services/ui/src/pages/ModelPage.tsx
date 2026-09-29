/** Страница 4 «Модель»: сведения об активной модели и оценка произвольного описания. */

import { useEffect, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { ApiCallError, api } from "../api/client";
import { DECISION_REASON_RU, DECISION_RU, formatDate, translate } from "../lib/format";
import { cssVars } from "../lib/style";
import { Banner, ScoreDial, SkeletonList } from "../components/ui";
import { FeatureTable } from "../components/signals";
import { IconModel } from "../components/icons";

const METRIC_RU: Record<string, string> = {
  accuracy: "Accuracy (доля верных решений)",
  precision: "Precision (точность)",
  recall: "Recall (полнота)",
  f1: "F1-мера",
  roc_auc: "ROC-AUC",
  threshold: "Порог решения",
  test_size: "Размер отложенного теста",
};

const SHARE_METRICS = new Set(["accuracy", "precision", "recall", "f1", "roc_auc", "threshold"]);

function metricValue(value: number | string): string {
  if (typeof value !== "number") return String(value);
  return Number.isInteger(value) ? String(value) : value.toFixed(3).replace(".", ",");
}

export default function ModelPage() {
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [withEnrichment, setWithEnrichment] = useState(false);

  const model = useQuery({ queryKey: ["model"], queryFn: () => api.getModel() });
  const score = useMutation({
    mutationFn: () => api.scoreText(title.trim(), description.trim(), withEnrichment),
  });

  useEffect(() => {
    document.title = "Модель — Слабые сигналы";
  }, []);

  const scoreError = score.error instanceof ApiCallError ? score.error : null;

  return (
    <div className="page model">
      <section className="model__info panel" aria-labelledby="model-heading">
        <header className="model__head">
          <h1 id="model-heading" className="page-title">
            Модель и оценка описания
          </h1>
          <p className="page-lead">
            Модель analyzer: её версия, метрики на отложенном тесте и форма для оценки
            произвольного описания тем же кодом, которым analyzer упорядочивает кандидатов. Решение о слабом
            сигнале в итоговой выдаче принимает рубрика языковой модели, а уверенность задаёт локальный
            классификатор — подробнее в разделе «Методология».
          </p>
        </header>

        <div className="panel__scroll">
          {model.isLoading ? (
            <SkeletonList rows={3} />
          ) : model.isError ? (
            <Banner kind="error" title="Сведения о модели недоступны">
              {model.error instanceof ApiCallError ? model.error.message : "Повторите попытку позже"}
            </Banner>
          ) : (
            <div className="model__body">
              <dl className="passport">
                <div className="passport__item">
                  <dt>Версия модели</dt>
                  <dd className="passport__value">{model.data!.model_version_id}</dd>
                  <dd className="passport__hint">Семейство: {model.data!.model_family}</dd>
                </div>
                <div className="passport__item">
                  <dt>Датасет</dt>
                  <dd className="passport__value">{model.data!.dataset_version}</dd>
                  <dd className="passport__hint">Схема признаков: {model.data!.feature_schema_version}</dd>
                </div>
                <div className="passport__item">
                  <dt>Обучена</dt>
                  <dd className="passport__value">{formatDate(model.data!.trained_at)}</dd>
                  <dd className="passport__hint">Эмбеддер: {model.data!.embedding_model}</dd>
                </div>
                <div className="passport__item">
                  <dt>Признаков в реестре</dt>
                  <dd className="passport__value">{model.data!.feature_names.length}</dd>
                  <dd className="passport__hint">Все признаки интерпретируемы</dd>
                </div>
              </dl>

              <section className="gauges" aria-labelledby="metrics-heading">
                <h2 id="metrics-heading" className="section-title">
                  Метрики на отложенном тесте
                </h2>
                <ul className="gauges__list">
                  {Object.entries(model.data!.metrics).map(([key, value]) => {
                    const share = typeof value === "number" && SHARE_METRICS.has(key) ? value : null;
                    return (
                      <li
                        key={key}
                        className={`gauge ${share === null ? "gauge--plain" : ""} ${key === "threshold" ? "gauge--threshold" : ""}`}
                        style={share === null ? undefined : cssVars({ "--value": Math.max(0, Math.min(1, share)).toFixed(4) })}
                      >
                        <span className="gauge__label">{METRIC_RU[key] ?? key}</span>
                        <span className="gauge__value">{metricValue(value)}</span>
                        {share === null ? null : (
                          <span className="gauge__track" aria-hidden="true">
                            <span className="gauge__fill" />
                          </span>
                        )}
                      </li>
                    );
                  })}
                </ul>
                <p className="note">
                  Протокол: отложенный тест 20 % использован один раз, выбор гиперпараметров и порога —
                  на повторной стратифицированной кросс-валидации по обучающей части.
                </p>
              </section>

              <details className="disclosure">
                <summary className="disclosure__summary">
                  Список признаков ({model.data!.feature_names.length})
                </summary>
                <div className="disclosure__body">
                  <ul className="feature-names">
                    {model.data!.feature_names.map((name) => (
                      <li key={name}>
                        <code>{name}</code>
                      </li>
                    ))}
                  </ul>
                </div>
              </details>
            </div>
          )}
        </div>
      </section>

      <section className="model__score panel panel--raised" aria-labelledby="score-heading">
        <div className="panel__head">
          <h2 id="score-heading" className="panel__title">
            Оценить описание технологии
          </h2>
        </div>
        <div className="panel__scroll">
          <div className="score-form">
            <div className="field">
              <label className="field__label" htmlFor="score-title">
                Название технологии
              </label>
              <input
                id="score-title"
                className="input"
                value={title}
                maxLength={300}
                placeholder="например: нейроморфные чипы для периферийного инференса"
                onChange={(event) => setTitle(event.target.value)}
              />
            </div>
            <div className="field">
              <label className="field__label" htmlFor="score-description">
                Описание
              </label>
              <textarea
                id="score-description"
                className="textarea"
                value={description}
                maxLength={4000}
                placeholder="Кратко опишите технологию: что сделано, кем, на какой стадии"
                onChange={(event) => setDescription(event.target.value)}
              />
              <span className="field__hint">{description.length} / 4000 символов</span>
            </div>
            <label className="check">
              <input
                type="checkbox"
                className="check__input"
                checked={withEnrichment}
                onChange={(event) => setWithEnrichment(event.target.checked)}
              />
              <span className="check__box" aria-hidden="true" />
              <span className="check__text">Обогатить открытыми источниками (займёт до 90 секунд)</span>
            </label>
            <div className="score-form__actions">
              <button
                type="button"
                className="btn btn--primary"
                disabled={title.trim().length < 2 || score.isPending}
                onClick={() => score.mutate()}
              >
                <IconModel size={18} />
                {score.isPending ? "Считаем…" : "Оценить"}
              </button>
              {score.isPending ? <span className="score-form__wait" aria-hidden="true" /> : null}
            </div>
            {scoreError ? (
              <Banner kind="error" title="Оценка не выполнена">
                {scoreError.code === "TIMEOUT"
                  ? "Оценка заняла слишком долго — попробуйте без обогащения"
                  : scoreError.message}
              </Banner>
            ) : null}
          </div>

          {score.data ? (
            <section className="score-result" aria-labelledby="score-result-heading">
              <h3 id="score-result-heading" className="section-title">
                Результат оценки
              </h3>
              <div className="score-result__head">
                <ScoreDial value={score.data.score} caption="скоринг" />
                <div className="score-result__chips">
                  <span className="chip chip--accent">{translate(DECISION_RU, score.data.decision)}</span>
                  <span className="chip">{translate(DECISION_REASON_RU, score.data.decision_reason)}</span>
                  <span className={score.data.enrichment_applied ? "chip chip--green" : "chip chip--amber"}>
                    {score.data.enrichment_applied
                      ? "Обогащение применено"
                      : "Без обогащения: коллекционные признаки взяты по умолчанию"}
                  </span>
                  <span className="chip chip--code">{score.data.model_version_id}</span>
                </div>
              </div>
              <FeatureTable features={score.data.features} />
            </section>
          ) : null}
        </div>
      </section>
    </div>
  );
}
