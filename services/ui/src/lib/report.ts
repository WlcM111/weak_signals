/** Сборка отчёта об инсайте и полного отчёта по исследованию в Markdown (§7 HANDOFF_UI). */

import type { EvidenceKind, Job, ResultItem, ResultItemSummary, Results } from "../api/types";
import {
  CONFIDENCE_BAND_RU, DECISION_RU, DIRECTION_RU, JOB_STATUS_RU, LANGUAGE_RU, NARRATIVE_STATUS_RU,
  SOURCE_TYPE_RU, STAGE_ORDINAL_RU, TREND_ORDINAL_RU, TRUST_LEVEL_RU, cleanRefs, decisionReasonText,
  formatDate, formatNumber, percent, predictorLabel, splitTitle, summaryMark, translate,
} from "./format";

const EVIDENCE_KIND_TEXT: Record<EvidenceKind, string> = {
  market: "рынок и практика",
  research: "исследование",
  patent: "патент",
};

/** Markdown-отчёт с теми же разделами, что и страница инсайта. */
export function buildInsightReport(item: ResultItem): string {
  const lines: string[] = [
    `# ${item.title_ru}`,
    "",
    `Оригинальное название кандидата: ${item.title_auto}`,
    `Поисковый запрос: ${item.query_text}`,
    `Статус: слабый сигнал · уверенность модели ${percent(item.score)} · ранг ${item.rank}`,
  ];
  if (item.predicted_stage) {
    lines.push(`Стадия развития: ${translate(STAGE_ORDINAL_RU as unknown as Record<string, string>, String(item.predicted_stage))}`);
  }
  if (item.predicted_trend) {
    lines.push(`Тренд упоминаний: ${translate(TREND_ORDINAL_RU as unknown as Record<string, string>, String(item.predicted_trend))}`);
  }
  lines.push(
    "",
    "## Описание технологии",
    "",
    cleanRefs(item.description_ru) || "—",
    "",
    "## Потенциальное преимущество",
    "",
    cleanRefs(item.advantage_ru) || "—",
    "",
    "## Кейс-пример",
    "",
    cleanRefs(item.case_example_ru) || "—",
    "",
    "## Почему это слабый сигнал",
    "",
    cleanRefs(item.explanation_ru) || "—",
    "",
    item.decision_explanation_ru || "",
    "",
    "### Признаки модели",
    "",
    "| Признак | Значение | Вклад | Направление |",
    "|---|---:|---:|---|",
  );
  for (const feature of item.features) {
    lines.push(
      `| ${feature.label_ru} | ${feature.value.toFixed(3)} | ${feature.contribution.toFixed(3)} | ${translate(DIRECTION_RU, feature.direction)} |`,
    );
  }
  const reports = item.sources.filter((source) =>
    ["ANALYTICAL_REPORT", "GOVERNMENT", "STANDARD"].includes(source.source_type),
  );
  const analytical = reports.length > 0 ? reports : item.sources.filter((source) => source.summary_ru);
  lines.push("", "## Оценки в аналитических отчётах", "");
  if (analytical.length === 0) {
    lines.push("Аналитические отчёты по теме среди найденных источников отсутствуют.");
  } else {
    if (reports.length === 0) {
      lines.push("Аналитических отчётов среди источников нет — ниже оценки по источникам карточки.", "");
    }
    for (const source of analytical) {
      lines.push(`- ${source.title} (${translate(SOURCE_TYPE_RU, source.source_type)}): ${source.summary_ru}`);
    }
  }

  lines.push("", "## Источники", "");
  item.sources.forEach((source, index) => {
    lines.push(
      `${index + 1}. **${source.title}**`,
      `   - Ссылка: ${source.url}`,
      `   - Дата публикации: ${formatDate(source.published_at)}`,
      `   - Тип источника: ${translate(SOURCE_TYPE_RU, source.source_type)}`,
      `   - Язык оригинала: ${translate(LANGUAGE_RU, source.language_code)}`,
      `   - Уровень доверенности: ${translate(TRUST_LEVEL_RU, source.trust_level)}`,
      `   - Резюме (${summaryMark(source.summary_kind)}): ${source.summary_ru}`,
      "",
    );
  });
  lines.push(
    "## Происхождение",
    "",
    `- Версия модели: ${item.provenance.model_version_id || "—"}`,
    `- Провайдер LLM: ${item.provenance.llm_provider || "—"}`,
    `- Модель LLM: ${item.provenance.llm_model || "—"}`,
    `- Версия промпта: ${item.provenance.prompt_version || "—"}`,
    `- Статус нарратива: ${translate(NARRATIVE_STATUS_RU, item.narrative_status)}`,
    "",
  );
  return lines.join("\n");
}

function escapeTableCell(value: string): string {
  return value.replace(/\|/g, "\\|").replace(/\r?\n+/g, " ").trim() || "—";
}

function appendFullSignalSection(lines: string[], index: number, item: ResultItem, summary?: ResultItemSummary): void {
  const [titleRu, titleOrig] = splitTitle(item.title_ru);
  const origName = titleOrig || (item.title_auto !== item.title_ru ? item.title_auto : "");
  const stageText = item.predicted_stage
    ? translate(STAGE_ORDINAL_RU as unknown as Record<string, string>, String(item.predicted_stage))
    : "—";
  const trendText = item.predicted_trend
    ? translate(TREND_ORDINAL_RU as unknown as Record<string, string>, String(item.predicted_trend))
    : "—";
  const evidenceKind = item.evidence_kind ?? summary?.evidence_kind;

  lines.push(
    `### 3.${index + 1}. [Ранг ${item.rank}] ${titleRu}`,
    "",
    `- **Оригинальное название кандидата:** ${origName || item.title_auto || "—"}`,
    `- **Индекс уверенности:** ${percent(item.score)} (${translate(CONFIDENCE_BAND_RU, item.confidence_band)})`,
    `- **Стадия развития:** ${stageText} · **Тренд упоминаний:** ${trendText}`,
    `- **Основа сигнала:** ${evidenceKind ? EVIDENCE_KIND_TEXT[evidenceKind] : "—"} · **Источников:** ${item.sources.length}`,
    `- **Статус нарратива:** ${translate(NARRATIVE_STATUS_RU, item.narrative_status)}`,
    "",
    "#### Описание технологии",
    "",
    cleanRefs(item.description_ru) || "—",
    "",
    "#### Потенциальное преимущество",
    "",
    cleanRefs(item.advantage_ru) || "—",
    "",
    "#### Кейс-пример",
    "",
    cleanRefs(item.case_example_ru) || "—",
  );

  const caseSource = item.sources.find((source) => source.document_id === item.case_document_id);
  if (caseSource) {
    lines.push("", `_Источник кейс-примера:_ ${caseSource.title} (${caseSource.url})`);
  }

  const reports = item.sources.filter((source) =>
    ["ANALYTICAL_REPORT", "GOVERNMENT", "STANDARD"].includes(source.source_type),
  );
  const analytical = reports.length > 0 ? reports : item.sources.filter((source) => source.summary_ru);
  lines.push("", "#### Оценки в аналитических отчётах", "");
  if (analytical.length === 0) {
    lines.push("Аналитические отчёты по теме среди найденных источников отсутствуют.");
  } else {
    if (reports.length === 0) {
      lines.push("Аналитических отчётов среди источников нет — ниже оценки по доказательным источникам карточки:", "");
    }
    for (const source of analytical) {
      lines.push(`- **${source.title}** (${translate(SOURCE_TYPE_RU, source.source_type)}): ${source.summary_ru}`);
    }
  }

  lines.push(
    "",
    "#### Почему это слабый сигнал",
    "",
    cleanRefs(item.explanation_ru) || "—",
  );
  if (item.decision_explanation_ru && item.decision_explanation_ru !== item.explanation_ru) {
    lines.push("", item.decision_explanation_ru);
  }

  if (item.key_predictors.length > 0) {
    lines.push(
      "",
      "**Ключевые предикторы:** " + item.key_predictors.map((feature) => predictorLabel(feature)).join("; "),
    );
  }

  if (item.features.length > 0) {
    lines.push(
      "",
      "| Признак | Код | Значение | Вклад | Направление |",
      "|---|---|---:|---:|---|",
    );
    for (const feature of item.features) {
      lines.push(
        `| ${escapeTableCell(feature.label_ru)} | \`${feature.feature_name}\` | ${feature.value.toFixed(3)} | ${feature.contribution.toFixed(3)} | ${translate(DIRECTION_RU, feature.direction)} |`,
      );
    }
  }

  lines.push("", `#### Источники (${item.sources.length})`, "");
  item.sources.forEach((source, sIdx) => {
    lines.push(
      `${sIdx + 1}. **${source.title}**`,
      `   - Ссылка: ${source.url}`,
      `   - Дата публикации: ${formatDate(source.published_at)}`,
      `   - Тип источника: ${translate(SOURCE_TYPE_RU, source.source_type)}`,
      `   - Язык оригинала: ${translate(LANGUAGE_RU, source.language_code)}`,
      `   - Уровень доверенности: ${translate(TRUST_LEVEL_RU, source.trust_level)}`,
      `   - Резюме (${summaryMark(source.summary_kind)}): ${source.summary_ru}`,
    );
    if (source.snippet) {
      lines.push(`   - Фрагмент оригинала: «${source.snippet.replace(/\r?\n+/g, " ").trim()}»`);
    }
    lines.push("");
  });

  lines.push(
    "#### Происхождение",
    "",
    `- Модель ранжирования: ${item.provenance.model_version_id || "—"}`,
    `- Провайдер и модель LLM: ${item.provenance.llm_provider || "—"} / ${item.provenance.llm_model || "—"}`,
    `- Версия промпта: ${item.provenance.prompt_version || "—"}`,
    "",
  );
}

function appendSummarySignalSection(lines: string[], index: number, summary: ResultItemSummary): void {
  const [titleRu, titleOrig] = splitTitle(summary.title_ru);
  lines.push(
    `### 3.${index + 1}. [Ранг ${summary.rank}] ${titleRu}`,
    "",
    `- **Оригинальное название:** ${titleOrig || "—"}`,
    `- **Индекс уверенности:** ${percent(summary.score)} (${translate(CONFIDENCE_BAND_RU, summary.confidence_band)})`,
    `- **Основа сигнала:** ${summary.evidence_kind ? EVIDENCE_KIND_TEXT[summary.evidence_kind] : "—"} · **Источников:** ${summary.source_count}`,
    `- **Статус нарратива:** ${translate(NARRATIVE_STATUS_RU, summary.narrative_status)}`,
    `- **Объяснение решения:** ${summary.decision_explanation_ru || "—"}`,
    `- **Ключевые предикторы:** ${summary.key_predictors.map((f) => predictorLabel(f)).join("; ") || "—"}`,
    "",
  );
}

/** Полное аналитическое досье по всему исследованию в Markdown для нейросетей (Вариант 1А). */
export function buildResearchReport(
  job: Job,
  results: Results,
  fullItems: (ResultItem | null)[],
  took?: string | null,
): string {
  const stats = results.stats;
  const items = results.items;
  const excluded = results.excluded;
  const byId = new Map<string, ResultItem>();
  fullItems.forEach((entry) => {
    if (entry) byId.set(entry.item_id, entry);
  });

  const safeQuery = job.query_text.replace(/"/g, '\\"');
  const lines: string[] = [
    "---",
    "report_type: weak_signals_full_research",
    `job_id: "${job.job_id}"`,
    `query: "${safeQuery}"`,
    `status: ${job.status}`,
    `created_at: "${formatDate(job.created_at, true)}"`,
    `finished_at: "${formatDate(job.finished_at, true)}"`,
    `duration: "${took || "—"}"`,
    `model_version: "${stats.model_version_id || "—"}"`,
    `signals_in_output: ${items.length}`,
    `weak_signals_total: ${stats.weak_signals_total}`,
    `weak_signals_confident: ${stats.weak_signals_confident}`,
    `candidates_found: ${stats.candidates_found}`,
    `documents_collected: ${stats.documents_collected}`,
    `sources_processed: ${stats.sources_processed}`,
    `http_requests_total: ${stats.http_requests_total}`,
    "---",
    "",
    `# Отчёт по исследованию слабых сигналов: «${job.query_text}»`,
    "",
    "> **Контекст для нейросети (LLM):** Данный документ представляет собой полную структурированную выгрузку результатов аналитического конвейера выявления зарождающихся научно-технологических трендов (слабых сигналов) по открытым источникам. Раздел 1 содержит метрики воронки отбора, Раздел 2 — сводную матрицу ранжированных слабых сигналов, Раздел 3 — детальные досье каждой найденной технологии с доказательной базой и признаками классификатора, Раздел 4 — реестр отсеянных кандидатов (зрелые решения, хайп, шум, нерелевантные темы) для понимания границ предметной области. Индекс уверенности 75 % и выше означает высокую уверенность модели.",
    "",
    "## 1. Сводка и воронка отбора",
    "",
    `- **Поисковый запрос:** ${job.query_text}`,
    `- **Статус выполнения:** ${translate(JOB_STATUS_RU, job.status)} (${formatDate(job.created_at, true)}${took ? `, длительность: ${took}` : ""})`,
    `- **Обработано источников:** ${formatNumber(stats.sources_processed)} (${formatNumber(stats.documents_collected)} документов собрано, ${formatNumber(stats.http_requests_total)} HTTP-запросов)`,
    `- **Кандидатов на слабый сигнал:** ${formatNumber(stats.candidates_found)} (признаны слабыми сигналами: ${formatNumber(stats.weak_signals_total)}, в итоговой выдаче: ${formatNumber(items.length)})`,
    `- **Уверенность модели выше 75 %:** ${formatNumber(stats.weak_signals_confident)} среди найденных слабых сигналов`,
    `- **Активная модель:** ${stats.model_version_id || "—"}${stats.narratives_generated !== undefined ? ` (сгенерировано LLM: ${formatNumber(stats.narratives_generated)}, экстрактивных: ${formatNumber(stats.narratives_fallback ?? 0)})` : ""}`,
    "",
    "## 2. Сводная матрица слабых сигналов",
    "",
  ];

  if (items.length === 0) {
    lines.push("По данному запросу ни один кандидат не прошёл отбор в итоговую выдачу.", "");
  } else {
    lines.push(
      "| Ранг | Технология | Оригинальное название | Уверенность | Уровень | Стадия | Тренд | Основа | Источников |",
      "|---:|---|---|---:|---|---|---|---|---:|",
    );
    for (const summary of items) {
      const full = byId.get(summary.item_id);
      const [titleRu, titleOrig] = splitTitle(summary.title_ru);
      const orig = titleOrig || (full && full.title_auto !== full.title_ru ? full.title_auto : "—");
      const stage = full?.predicted_stage
        ? translate(STAGE_ORDINAL_RU as unknown as Record<string, string>, String(full.predicted_stage))
        : "—";
      const trend = full?.predicted_trend
        ? translate(TREND_ORDINAL_RU as unknown as Record<string, string>, String(full.predicted_trend))
        : "—";
      const kind = summary.evidence_kind ? EVIDENCE_KIND_TEXT[summary.evidence_kind] : "—";
      lines.push(
        `| ${summary.rank} | ${escapeTableCell(titleRu)} | ${escapeTableCell(orig)} | ${percent(summary.score)} | ${translate(CONFIDENCE_BAND_RU, summary.confidence_band)} | ${stage} | ${trend} | ${kind} | ${summary.source_count} |`,
      );
    }
    lines.push("");
  }

  lines.push("## 3. Детальные досье слабых сигналов", "");
  if (items.length === 0) {
    lines.push("Детальные карточки отсутствуют.", "");
  } else {
    items.forEach((summary, index) => {
      const full = byId.get(summary.item_id);
      if (full) {
        appendFullSignalSection(lines, index, full, summary);
      } else {
        appendSummarySignalSection(lines, index, summary);
      }
    });
  }

  lines.push("## 4. Исключённые кандидаты (контекст границ темы)", "");
  if (excluded.length === 0) {
    lines.push("Ни один кандидат не был исключён.", "");
  } else {
    lines.push(
      `Всего отсеяно кандидатов: ${formatNumber(excluded.length)}.`,
      "",
      "| Кандидат | Решение | Причина | Скоринг | Док. | Объяснение |",
      "|---|---|---|---:|---:|---|",
    );
    for (const candidate of excluded) {
      lines.push(
        `| ${escapeTableCell(candidate.title_auto)} | ${escapeTableCell(translate(DECISION_RU, candidate.decision))} | ${escapeTableCell(decisionReasonText(candidate.decision_reason))} | ${percent(candidate.score)} | ${candidate.document_count} | ${escapeTableCell(candidate.decision_explanation_ru)} |`,
      );
    }
    lines.push("");
  }

  return lines.join("\n");
}

const TRANSLIT: Record<string, string> = {
  а: "a", б: "b", в: "v", г: "g", д: "d", е: "e", ё: "e", ж: "zh", з: "z", и: "i", й: "y", к: "k", л: "l",
  м: "m", н: "n", о: "o", п: "p", р: "r", с: "s", т: "t", у: "u", ф: "f", х: "kh", ц: "ts", ч: "ch", ш: "sh",
  щ: "shch", ъ: "", ы: "y", ь: "", э: "e", ю: "yu", я: "ya",
};

function slugify(text: string): string {
  return [...text.toLowerCase()]
    .map((char) => TRANSLIT[char] ?? char)
    .join("")
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 48)
    .replace(/-+$/g, "");
}

/** Имя файла отчёта: латиница и цифры, чтобы не зависеть от кодировки файловой системы. */
export function reportFileName(item: ResultItem): string {
  const slug = slugify(item.title_ru);
  return `insight-${item.rank}-${slug || item.item_id.slice(0, 8)}.md`;
}

/** Имя файла полного отчёта по исследованию. */
export function researchReportFileName(job: Job): string {
  const slug = slugify(job.query_text);
  return `research-${slug || job.job_id.slice(0, 8)}.md`;
}
