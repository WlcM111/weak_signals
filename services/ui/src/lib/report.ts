/** Сборка отчёта об инсайте в Markdown для кнопки «Скачать отчёт» (§7 HANDOFF_UI). */

import type { ResultItem } from "../api/types";
import {
  DIRECTION_RU, LANGUAGE_RU, NARRATIVE_STATUS_RU, SOURCE_TYPE_RU, STAGE_ORDINAL_RU,
  TREND_ORDINAL_RU, TRUST_LEVEL_RU, formatDate, percent, summaryMark, translate,
} from "./format";

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
    item.description_ru || "—",
    "",
    "## Потенциальное преимущество",
    "",
    item.advantage_ru || "—",
    "",
    "## Кейс-пример",
    "",
    item.case_example_ru || "—",
    "",
    "## Почему это слабый сигнал",
    "",
    item.explanation_ru || "—",
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
  const analytical = item.sources.filter((source) =>
    ["ANALYTICAL_REPORT", "GOVERNMENT", "STANDARD"].includes(source.source_type),
  );
  lines.push("", "## Оценки в аналитических отчётах", "");
  if (analytical.length === 0) {
    lines.push("Аналитические отчёты по теме среди найденных источников отсутствуют.");
  } else {
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

/** Имя файла отчёта: латиница и цифры, чтобы не зависеть от кодировки файловой системы. */
export function reportFileName(item: ResultItem): string {
  const slug = item.title_ru
    .toLowerCase()
    .replace(/[^a-z0-9а-яё]+/gi, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 48);
  return `insight-${item.rank}-${slug || item.item_id.slice(0, 8)}.md`;
}
