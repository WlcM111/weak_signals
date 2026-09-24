#!/usr/bin/env bash
# Сбор результатов A → B в архив с SHA-256 и без секретов.
# Запуск из корня репозитория: bash tools/ml_rework/collect_results.sh <run_id> [<run_id> ...]
set -euo pipefail
cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
ts=$(date +%Y%m%d-%H%M%S)
base="${WS_RESULTS_DIR:-$HOME/ws-results}"
out="$base/ws-ml-results-$ts"
mkdir -p "$out"
for run in "$@"; do
  if [ -d "ml/reports/experiments/$run" ]; then cp -R "ml/reports/experiments/$run" "$out/experiments-$run"; fi
  if [ -d "ml/reports/seq/$run" ]; then cp -R "ml/reports/seq/$run" "$out/seq-$run"; fi
done
if [ -f ml/reports/experiments/holdout_usage.jsonl ]; then cp ml/reports/experiments/holdout_usage.jsonl "$out/"; fi
# Живые прогоны за последние 3 суток (analytics, own-topics, relevance).
find . -maxdepth 1 \( -name 'analytics-*.txt' -o -name 'own-topics-*.json' -o -name 'relevance-*.csv' \) -mtime -3 \
  -exec cp {} "$out/" \;
# Только несекретные настройки.
grep -E '^WS_(CANDIDATE_JUDGE_(ORDER|ENABLED|POOL)|ML_SCORE_ABLATION|JOB_DEADLINE[A-Z_]*|MIN_CANDIDATE_QUERY_SIM|EMBEDDING_MODEL|LLM_PRIMARY_PROVIDER)=' \
  .env > "$out/env_public.txt" 2>/dev/null || true
git rev-parse HEAD > "$out/git_head.txt" 2>/dev/null || true
git status --short > "$out/git_status.txt" 2>/dev/null || true
git diff --stat > "$out/git_diff_stat.txt" 2>/dev/null || true
docker compose ps > "$out/compose_ps.txt" 2>/dev/null || true
docker compose images > "$out/compose_images.txt" 2>/dev/null || true
{ uname -a; sw_vers 2>/dev/null || true; docker version --format '{{.Server.Os}}/{{.Server.Arch}} {{.Server.Version}}' 2>/dev/null || true; } > "$out/system.txt"
# Защита от утечки: похожие на ключи/пароли строки блокируют создание архива.
if grep -rIlE '(sk-[A-Za-z0-9]{20,}|AQVN[A-Za-z0-9_-]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY|(API_KEY|PASSWORD|SECRET|TOKEN)=[^[:space:]]+)' "$out"; then
  echo "Найдены строки, похожие на секреты (файлы выше). Архив не создан: удалите их из $out и повторите." >&2
  exit 1
fi
(cd "$out" && find . -type f ! -name SHA256SUMS -print0 | sort -z | xargs -0 shasum -a 256 > SHA256SUMS)
tar czf "$out.tar.gz" -C "$base" "$(basename "$out")"
shasum -a 256 "$out.tar.gz" | tee "$out.tar.gz.sha256"
echo "Готово: $out.tar.gz"
