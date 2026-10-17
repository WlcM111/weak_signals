/** Форма запроса: направление, примеры, размер выдачи, отправка. */

import { useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { ApiCallError, api } from "../api/client";
import { Banner } from "./ui";
import { IconSearch } from "./icons";

const MIN_LENGTH = 2;
const MAX_LENGTH = 500;
const TOP_CHOICES = [5, 10, 15, 25, 50];
const EXAMPLES = [
  "слабые сигналы в области кибербезопасности",
  "перспективные решения в финтехе",
  "технологии в промышленном ИИ",
  "зарождающиеся направления в робототехнике",
];

const IS_MAC = typeof navigator !== "undefined" && /Mac|iPhone|iPad/.test(navigator.userAgent);

export function QueryForm({ running = 0 }: { running?: number }) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [text, setText] = useState("");
  const [topN, setTopN] = useState(15);
  const [idempotencyKey, setIdempotencyKey] = useState(() => crypto.randomUUID());
  const [tried, setTried] = useState(false);
  const input = useRef<HTMLTextAreaElement | null>(null);

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
  const invalidShown = tried && !valid;
  const error = submit.error instanceof ApiCallError ? submit.error : null;

  return (
    <form
      className="qform"
      noValidate
      onSubmit={(event) => {
        event.preventDefault();
        if (!valid) {
          setTried(true);
          input.current?.focus();
          return;
        }
        if (!submit.isPending) submit.mutate();
      }}
    >
      <div className="field">
        <label className="field__label" htmlFor="query">
          Технологическое направление
        </label>
        <textarea
          ref={input}
          id="query"
          className="textarea qform__input"
          aria-invalid={invalidShown ? true : undefined}
          aria-describedby={invalidShown ? "query-error" : undefined}
          rows={4}
          value={text}
          maxLength={MAX_LENGTH + 50}
          placeholder="например: слабые сигналы в области кибербезопасности"
          onChange={(event) => setText(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
              event.preventDefault();
              event.currentTarget.form?.requestSubmit();
            }
          }}
        />
        <div className="field__foot">
          <span className={`counter ${length > MAX_LENGTH ? "counter--over" : ""}`}>
            {length} / {MAX_LENGTH} символов
          </span>
          <span className="field__hint qform__keys">
            <kbd>{IS_MAC ? "⌘" : "Ctrl"}</kbd> + <kbd>Enter</kbd> — отправить
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

      <div className="qform__row">
        <fieldset className="segmented">
          <legend className="field__label">Размер выдачи</legend>
          <div className="segmented__options">
            {TOP_CHOICES.map((choice) => (
              <label key={choice} className={`segmented__option ${topN === choice ? "segmented__option--on" : ""}`}>
                <input
                  type="radio"
                  name="top-n"
                  className="visually-hidden"
                  value={choice}
                  checked={topN === choice}
                  onChange={() => setTopN(choice)}
                />
                ТОП-{choice}
              </label>
            ))}
          </div>
        </fieldset>

        <div className="qform__submit">
          <button type="submit" className="btn btn--primary btn--lg" disabled={submit.isPending}>
            <IconSearch size={20} />
            {submit.isPending ? "Отправляем…" : "Найти слабые сигналы"}
          </button>
          {running > 0 ? (
            <span className="query__running">
              <span className="query__running-dot" aria-hidden="true" />
              Выполняется заданий: {running}
            </span>
          ) : null}
        </div>
      </div>

      {invalidShown ? (
        <p className="qform__error" id="query-error" role="alert">
          {length > MAX_LENGTH
            ? `Сократите запрос до ${MAX_LENGTH} символов.`
            : "Опишите направление хотя бы двумя символами, например «квантовые сенсоры»."}
        </p>
      ) : null}

      {error ? (
        <Banner kind="error" title="Не удалось создать задание">
          {error.message}
        </Banner>
      ) : null}
    </form>
  );
}
