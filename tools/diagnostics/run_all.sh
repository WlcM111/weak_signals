#!/usr/bin/env bash
# Единая точка запуска диагностик патча proven_fixes (DIAGNOSTICS_ALL.md). Только чтение БД и журналов.
# Запуск из корня проекта: bash tools/diagnostics/run_all.sh [N_заданий=6] [период_журнала=24h]
# Каждая проверка пишет <ID>.txt; summary.tsv различает «проверка выполнена» (exit 0), «нет данных» (exit 3) и
# технический сбой самой проверки (иной код). Сбой одной проверки не останавливает остальные.
set -u
N="${1:-6}"
SINCE="${2:-24h}"
OUT="diag_$(date +%Y%m%d-%H%M%S)"
mkdir -p "$OUT"
printf 'id\texit_code\tstatus\tfile\n' > "$OUT/summary.tsv"

record() {  # id, exit_code
  local status="ok"
  if [ "$2" -eq 3 ]; then status="нет данных"; elif [ "$2" -ne 0 ]; then status="СБОЙ ПРОВЕРКИ"; fi
  printf '%s\t%s\t%s\t%s\n' "$1" "$2" "$status" "$1.txt" >> "$OUT/summary.tsv"
}

sql() {  # id, file
  docker compose exec -T postgres psql -U postgres -d weaksignals -v ON_ERROR_STOP=1 -v n="$N" -f - \
    < "tools/diagnostics/sql/$2" > "$OUT/$1.txt" 2>&1
  record "$1" $?
}

logs() {  # id, script
  docker compose logs analyzer --since "$SINCE" --no-color > "$OUT/analyzer.log" 2>&1
  python3 "tools/diagnostics/$2" < "$OUT/analyzer.log" > "$OUT/$1.txt" 2>&1
  record "$1" $?
}

sql D-U06-jobs u06_jobs.sql
sql D-U06-adapters u06_adapters.sql
sql D-U03-decisions u03_decisions.sql
sql D-U02-output u02_output.sql
sql D-U02-excluded u02_excluded.sql
logs D-U01-shares u01_cluster_shares.py
logs D-U04-preselect u04_preselect.py

# Флаги и параметры патча в работающих контейнерах (только имена и булевы/числовые значения; секретов здесь нет).
{
  for service in orchestrator-worker analyzer collector; do
    echo "== $service"
    docker compose exec -T "$service" sh -c 'env | grep -E "^WS_(SELECTION_MODE|RUBRIC_POOL|RUBRIC_POOL_RESEARCH_SINGLES|RUBRIC_POOL_EXCLUDE_MODEL_MATURE|RUBRIC_DOCS_RETRY|RUBRIC_FINALIZE_RETRY_SMALL|FINAL_CHECK_FAIL_AS_UNCERTAIN|FINAL_SPECIFICITY_CHECK|NARRATE_TRACE_EVIDENCE|TRACE_API|PRESELECT_BY_MODEL|PRESELECT_MODEL_SHARE|MATURE_FIELD_FULL_TITLE|CLUSTER_[A-Z_]+|COLLECTOR_TECH_FILTER_WORD_LEVEL|COLLECTOR_TARGETED_PER_TECH_ERRORS|COLLECTOR_BROAD_FAILURE_KEEPS_TARGETED|QUERY_FALLBACK_STRICT|SOURCE_ENABLED_OVERRIDE|REQUIRE_HIGH_TRUST_SOURCE)=" | sort' || exit 1
  done
} > "$OUT/D-FLAGS.txt" 2>&1
record D-FLAGS $?

echo "Готово: $OUT"
cat "$OUT/summary.tsv"
