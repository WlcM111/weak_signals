/** Форматирование и русские названия значений контракта (§13 HANDOFF_UI). */

import type {
  ConfidenceBand, Direction, Feature, JobStatus, NarrativeStatus, SummaryKind, TrustLevel,
} from "../api/types";

export const CONFIDENT_SCORE = 0.75;

export const JOB_STATUS_RU: Record<string, string> = {
  QUEUED: "В очереди",
  COLLECTING: "Сбор источников",
  ANALYZING: "Анализ кандидатов",
  NARRATING: "Формирование инсайтов",
  COMPLETED: "Завершено",
  PARTIAL: "Завершено частично",
  FAILED: "Ошибка",
  CANCELLED: "Отменено",
};

export const STAGE_HINT_RU: Record<string, string> = {
  QUEUED: "Задание поставлено в очередь и скоро начнёт выполняться",
  COLLECTING: "Собираем публикации, препринты, патенты, репозитории и вакансии",
  ANALYZING: "Считаем 25 признаков, применяем правила исключения и модель",
  NARRATING: "Готовим описания, преимущества и кейс-примеры",
};

export const DECISION_RU: Record<string, string> = {
  WEAK_SIGNAL: "Слабый сигнал",
  MATURE: "Зрелая технология",
  HYPE_OR_NOISE: "Хайп или информационный шум",
  INSUFFICIENT_EVIDENCE: "Недостаточно доказательств",
  OFF_TOPIC: "Не по теме запроса",
};

export const DECISION_REASON_RU: Record<string, string> = {
  MODEL_SCORE: "Решение калиброванной модели",
  SEMANTIC_JUDGE: "Смысловая оценка кандидата моделью",
  LOW_QUERY_RELEVANCE: "Низкая релевантность запросу",
  NO_TRUSTED_SOURCE: "Нет ни одного доверенного источника",
  SINGLE_SOURCE: "Единственный источник",
  ENCYCLOPEDIA_MATURE: "Зрелая статья в энциклопедии",
  MARKET_LEADERS: "Сформированный рынок и выраженные лидеры",
  MATURITY_LEXICON: "Лексика массового внедрения",
  MARKETING_DOMINANT: "Преобладают маркетинговые публикации",
  HYPE_LEXICON: "Лексика хайпа без доверенных источников",
};

export const SOURCE_TYPE_RU: Record<string, string> = {
  SCIENTIFIC_PUBLICATION: "Научная публикация",
  PREPRINT: "Препринт",
  PATENT: "Патент",
  CODE_REPOSITORY: "Репозиторий кода",
  ENCYCLOPEDIA: "Энциклопедия",
  NEWS: "Новость",
  INDUSTRY_MEDIA: "Отраслевое медиа",
  ANALYTICAL_REPORT: "Аналитический отчёт",
  GOVERNMENT: "Государственный источник",
  STANDARD: "Стандарт",
  PRESS_RELEASE: "Пресс-релиз",
  CORPORATE_BLOG: "Корпоративный блог",
  VACANCY: "Вакансия",
  OTHER: "Другое",
};

export const TRUST_LEVEL_RU: Record<string, string> = {
  HIGH: "Высокая доверенность",
  MEDIUM: "Средняя доверенность",
  LOW: "Пониженная доверенность",
};

export const SUMMARY_KIND_RU: Record<string, string> = {
  ORIGINAL_RU: "оригинал на русском",
  GENERATIVE_SUMMARY: "резюме сгенерировано автоматически",
  EXTRACTIVE: "фрагмент оригинала без перевода",
};

export const NARRATIVE_STATUS_RU: Record<string, string> = {
  GENERATED: "Инсайт сгенерирован моделью",
  FALLBACK_EXTRACTIVE: "Инсайт экстрактивный: собран из источников без генеративной модели",
};

export const DIRECTION_SIGN: Record<string, string> = {
  supports_weak_signal: "↑",
  supports_mature: "↓",
  neutral: "•",
};

export const DIRECTION_RU: Record<string, string> = {
  supports_weak_signal: "за слабый сигнал",
  supports_mature: "против: признак зрелости",
  neutral: "нейтрально",
};

export const CONFIDENCE_BAND_RU: Record<string, string> = {
  High: "высокая уверенность",
  Medium: "средняя уверенность",
  Low: "низкая уверенность",
};

export const LANGUAGE_RU: Record<string, string> = {
  ru: "русский",
  en: "английский",
  de: "немецкий",
  fr: "французский",
  zh: "китайский",
  ja: "японский",
};

export const STAGE_ORDINAL_RU: Record<number, string> = {
  1: "исследования и концепция",
  2: "прототип",
  3: "пилотные внедрения",
  4: "раннее внедрение",
};

export const TREND_ORDINAL_RU: Record<number, string> = {
  1: "стабильный",
  2: "растёт",
  3: "быстро растёт",
};

const TERMINAL: ReadonlySet<string> = new Set(["COMPLETED", "PARTIAL", "FAILED", "CANCELLED"]);
const STAGE_ORDER: JobStatus[] = ["QUEUED", "COLLECTING", "ANALYZING", "NARRATING"];

/** Перевод значения перечисления; неизвестное показывается как есть (§13). */
export function translate(table: Record<string, string>, value?: string | null): string {
  if (!value) return "—";
  return table[value] ?? value;
}

/** Достигнут ли терминальный статус задания. */
export function isTerminal(status?: string | null): boolean {
  return TERMINAL.has(status ?? "");
}

/** Доля 0..1 в проценты с русской запятой. */
export function percent(value?: number | null, digits = 0): string {
  if (value === undefined || value === null || Number.isNaN(value)) return "—";
  return `${(value * 100).toFixed(digits).replace(".", ",")} %`;
}

/** Число с узкими пробелами между разрядами. */
export function formatNumber(value?: number | null): string {
  if (value === undefined || value === null) return "—";
  return Math.round(value).toLocaleString("ru-RU").replace(/\u00a0/g, "\u202f");
}

/** ISO-дата → `дд.мм.гггг`; пустое значение — «дата не указана». */
export function formatDate(value?: string | null, withTime = false): string {
  if (!value) return "дата не указана";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  const date = parsed.toLocaleDateString("ru-RU", { day: "2-digit", month: "2-digit", year: "numeric" });
  if (!withTime) return date;
  return `${date} ${parsed.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" })}`;
}

/** Ссылка отображается только для схем http и https (§17 HANDOFF_UI). */
export function isSafeLink(url?: string | null): boolean {
  return Boolean(url && /^https?:\/\//i.test(url));
}

/** Домен ссылки для компактного показа рядом с названием источника. */
export function linkHost(url?: string | null): string {
  if (!isSafeLink(url)) return "";
  try {
    return new URL(url as string).host;
  } catch {
    return "";
  }
}

/** Подпись ключевого предиктора: направление, русское название, вклад. */
export function predictorLabel(feature: Feature): string {
  const sign = DIRECTION_SIGN[feature.direction as Direction] ?? "•";
  const contribution = feature.contribution.toFixed(2).replace(".", ",");
  const signedContribution = feature.contribution >= 0 ? `+${contribution}` : contribution;
  return `${sign} ${feature.label_ru} (${signedContribution})`;
}

/** Класс чипа по уверенности модели. */
export function confidenceChipClass(band: ConfidenceBand | string): string {
  if (band === "High") return "chip chip--green";
  if (band === "Medium") return "chip chip--amber";
  return "chip";
}

/** Класс чипа по уровню доверенности источника. */
export function trustChipClass(trust: TrustLevel | string): string {
  if (trust === "HIGH") return "chip chip--green";
  if (trust === "MEDIUM") return "chip chip--cyan";
  return "chip chip--amber";
}

/** Класс чипа по статусу нарратива. */
export function narrativeChipClass(status: NarrativeStatus | string): string {
  return status === "GENERATED" ? "chip chip--accent" : "chip chip--amber";
}

/** Отметка происхождения резюме источника (требование ТЗ). */
export function summaryMark(kind: SummaryKind | string): string {
  return SUMMARY_KIND_RU[kind] ?? kind;
}

/** Порядковый номер стадии задания для индикатора шагов. */
export function stageIndex(status?: string | null): number {
  const index = STAGE_ORDER.indexOf((status ?? "") as JobStatus);
  return index === -1 ? STAGE_ORDER.length : index;
}

/** Доля выполнения задания 0..1 для полосы прогресса. */
export function jobProgressShare(status: string | undefined, done: number, total: number): number {
  switch (status) {
    case "QUEUED":
      return 0.05;
    case "COLLECTING":
      return 0.28;
    case "ANALYZING":
      return 0.58;
    case "NARRATING":
      return Math.min(0.7 + 0.3 * (total > 0 ? done / total : 0), 0.99);
    default:
      return 1;
  }
}

/** Понятные строки из `PARTIAL:found=9<15;fallback_narratives=2` (§7 HANDOFF_UI). */
export function partialReasons(message?: string | null): string[] {
  if (!message) return [];
  if (!message.startsWith("PARTIAL:")) return [message];
  return message
    .slice("PARTIAL:".length)
    .split(";")
    .filter(Boolean)
    .map((part) => {
      if (part.startsWith("found=")) {
        const [found, requested] = part.slice("found=".length).split("<");
        return `Найдено технологий: ${found} из запрошенных ${requested} — по запросу нашлось меньше кандидатов, прошедших правила исключения`;
      }
      if (part.startsWith("fallback_narratives=")) {
        return `Инсайтов без генеративной модели: ${part.slice("fallback_narratives=".length)} — тексты собраны из найденных источников`;
      }
      if (part.startsWith("adapters_failed=")) {
        return `Источники, ответившие ошибкой: ${part.slice("adapters_failed=".length).replace(/,/g, ", ")}`;
      }
      if (part.startsWith("collection=")) {
        return `Статус сбора источников: ${part.slice("collection=".length)}`;
      }
      if (part === "deadline_reached") {
        return "Достигнут срок обработки запроса: часть инсайтов собрана из найденных источников без генеративной модели";
      }
      return part;
    });
}

export const JOB_ERROR_RU: Record<string, string> = {
  TOO_FEW_DOCUMENTS: "По запросу собрано слишком мало документов — уточните формулировку направления",
  COLLECTION_FAILED: "Сбор источников завершился ошибкой",
  ANALYSIS_FAILED: "Анализ кандидатов завершился ошибкой",
  UPSTREAM_UNAVAILABLE: "Внутренний сервис был недоступен",
  CANCELLED_BY_USER: "Задание отменено пользователем",
  LEASE_EXPIRED_MAX_ATTEMPTS: "Исполнитель терял задание слишком много раз подряд",
  POSTPONED_MAX_ATTEMPTS: "Задание откладывалось максимальное число раз",
  INTERNAL_ERROR: "Внутренняя ошибка сервиса",
  DEADLINE_EXCEEDED: "Запрос не начал обрабатываться до истечения срока в 20 минут",
};

/** Человекочитаемая причина отказа задания: код важнее пустого сообщения. */
export function jobErrorText(error?: { code?: string; message?: string } | null): string {
  if (!error) return "Причина не указана";
  const byCode = error.code ? JOB_ERROR_RU[error.code] : undefined;
  const detail = (error.message ?? "").trim();
  if (byCode && detail && detail !== byCode) return `${byCode}. Детали: ${detail}`;
  return byCode ?? detail ?? "Причина не указана";
}