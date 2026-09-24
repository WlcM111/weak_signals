/**
 * Единственный адаптер интерфейса: HTTP-клиент orchestrator (§8 HANDOFF_UI).
 *
 * Запросы идут на относительный путь `/api/...`: в контейнере их проксирует nginx, подставляя
 * заголовок `X-API-Key` на сервере, в разработке — dev-сервер Vite. Ключ в браузер не попадает.
 */

import type {
  Job, JobAccepted, JobList, ModelInfo, Results, ResultItem, ScoreResponse,
} from "./types";

export const REQUEST_TIMEOUT_MS = 10_000;
export const SCORE_TIMEOUT_MS = 100_000;

/** Ошибка вызова API с человекочитаемым русским сообщением. */
export class ApiCallError extends Error {
  readonly status: number;
  readonly code: string;
  readonly requestId: string;

  constructor(message: string, status: number, code: string, requestId = "") {
    super(message);
    this.name = "ApiCallError";
    this.status = status;
    this.code = code;
    this.requestId = requestId;
  }
}

const MESSAGE_BY_CODE: Record<string, string> = {
  VALIDATION_ERROR: "Запрос заполнен неверно: проверьте текст и размер выдачи",
  UNAUTHORIZED: "Неверный API-ключ (проверьте переменную WS_API_KEY у сервиса ui)",
  NOT_FOUND: "Задание не найдено — возможно, оно было удалено",
  IDEMPOTENCY_CONFLICT: "Этот запрос уже отправлен с другим текстом, обновите страницу",
  JOB_NOT_CANCELLABLE: "Задание уже завершено, отменить его нельзя",
  RESULTS_NOT_READY: "Результат ещё формируется, подождите несколько секунд",
  QUEUE_FULL: "Очередь заданий заполнена, повторите примерно через 30 секунд",
  RATE_LIMITED: "Слишком много запросов, повторите примерно через 30 секунд",
  UPSTREAM_UNAVAILABLE: "Внутренний сервис недоступен, повторите попытку позже",
  TIMEOUT: "Превышено время ожидания ответа сервиса",
  NETWORK_ERROR: "Сервис недоступен: проверьте, запущен ли orchestrator-api",
};

const MESSAGE_BY_STATUS: Record<number, string> = {
  400: "Запрос заполнен неверно",
  401: "Неверный API-ключ (проверьте переменную WS_API_KEY у сервиса ui)",
  404: "Запрошенные данные не найдены",
  409: "Действие недоступно в текущем состоянии задания",
  429: "Сервис перегружен, повторите примерно через 30 секунд",
  500: "Внутренняя ошибка сервиса",
  502: "Внутренний сервис недоступен, повторите попытку позже",
  503: "Сервис временно недоступен",
};

/** Человекочитаемое сообщение по коду ошибки и статусу HTTP (§14 HANDOFF_UI). */
export function messageFor(code: string, status: number, fallback = ""): string {
  return (
    MESSAGE_BY_CODE[code] ??
    MESSAGE_BY_STATUS[status] ??
    (fallback || "Не удалось выполнить запрос")
  );
}

async function request<T>(
  path: string,
  init: RequestInit = {},
  timeoutMs = REQUEST_TIMEOUT_MS,
): Promise<T> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  let response: Response;
  try {
    response = await fetch(path, {
      ...init,
      signal: controller.signal,
      headers: { Accept: "application/json", ...(init.headers ?? {}) },
    });
  } catch (error) {
    clearTimeout(timer);
    const aborted = error instanceof DOMException && error.name === "AbortError";
    const code = aborted ? "TIMEOUT" : "NETWORK_ERROR";
    throw new ApiCallError(messageFor(code, 0), 0, code);
  }
  clearTimeout(timer);

  if (response.status === 204) return undefined as T;
  const payload: unknown = await response.json().catch(() => null);

  if (!response.ok) {
    const body = (payload ?? {}) as { code?: string; message?: string; request_id?: string };
    throw new ApiCallError(
      messageFor(body.code ?? "", response.status, body.message ?? ""),
      response.status,
      body.code ?? String(response.status),
      body.request_id ?? "",
    );
  }
  return payload as T;
}

/** Клиент HTTP API orchestrator: по одному методу на путь контракта. */
export const api = {
  /** `POST /api/v1/queries` — постановка задания в очередь. */
  createQuery(queryText: string, topN: number, idempotencyKey: string): Promise<JobAccepted> {
    return request<JobAccepted>("/api/v1/queries", {
      method: "POST",
      headers: { "Content-Type": "application/json", "Idempotency-Key": idempotencyKey },
      body: JSON.stringify({ query_text: queryText, top_n: topN }),
    });
  },

  /** `GET /api/v1/jobs` — последние задания. */
  listJobs(limit = 20): Promise<JobList> {
    return request<JobList>(`/api/v1/jobs?limit=${limit}`);
  },

  /** `GET /api/v1/jobs/{job_id}` — состояние задания. */
  getJob(jobId: string): Promise<Job> {
    return request<Job>(`/api/v1/jobs/${encodeURIComponent(jobId)}`);
  },

  /** `POST /api/v1/jobs/{job_id}/cancel` — отмена задания. */
  cancelJob(jobId: string): Promise<Job> {
    return request<Job>(`/api/v1/jobs/${encodeURIComponent(jobId)}/cancel`, { method: "POST" });
  },

  /** `GET /api/v1/jobs/{job_id}/results` — снимок результата. */
  getResults(jobId: string): Promise<Results> {
    return request<Results>(`/api/v1/jobs/${encodeURIComponent(jobId)}/results`);
  },

  /** `GET /api/v1/results/items/{item_id}` — инсайт целиком. */
  getResultItem(itemId: string): Promise<ResultItem> {
    return request<ResultItem>(`/api/v1/results/items/${encodeURIComponent(itemId)}`);
  },

  /** `GET /api/v1/model` — сведения об активной модели. */
  getModel(): Promise<ModelInfo> {
    return request<ModelInfo>("/api/v1/model");
  },

  /** `POST /api/v1/score` — прямая оценка описания технологии. */
  scoreText(title: string, description: string, withEnrichment: boolean): Promise<ScoreResponse> {
    return request<ScoreResponse>(
      "/api/v1/score",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title, description, with_enrichment: withEnrichment }),
      },
      SCORE_TIMEOUT_MS,
    );
  },

  /** `GET /readyz` — доступность API для баннера в шапке. */
  async readiness(): Promise<boolean> {
    try {
      const response = await fetch("/readyz", { signal: AbortSignal.timeout(5000) });
      return response.ok;
    } catch {
      return false;
    }
  },
};
