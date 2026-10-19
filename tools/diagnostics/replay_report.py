"""SQL-отчёт опыта R-012/R-020: для каждого эталонного сигнала из собранных документов — попал ли его документ в
кандидатов исходного анализа (150) и повторного (N), и на каком месте он в повторе по оценке модели и по близости
к запросу. Запуск на хосте: печатает SQL, который выполняется в postgres (только чтение).

Использование: python3 tools/diagnostics/replay_report.py <strategy_check_e2e_evidence.csv> <replay.tsv> <jobs.tsv>
replay.tsv — вывод replay_preselect.py; jobs.tsv — job_id, analysis_id исходного анализа, тема.
"""

from __future__ import annotations

import csv
import sys


def quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def main() -> int:
    if len(sys.argv) != 4:
        print(__doc__, file=sys.stderr)
        return 2
    evidence = list(csv.DictReader(open(sys.argv[1], encoding="utf-8-sig")))
    replay = {row[0]: row[1] for row in csv.reader(open(sys.argv[2], encoding="utf-8"), delimiter="\t") if row}
    jobs = {row[2]: (row[0], row[1]) for row in csv.reader(open(sys.argv[3], encoding="utf-8"), delimiter="\t") if row}
    values = []
    for item in evidence:
        job = jobs.get(item["topic"])
        if job is None or job[0] not in replay:
            continue
        values.append(f"({quote(item['row_id'])}, {quote(item['topic'][:40])}, {quote(item['url'])}, "
                      f"{quote(job[1])}::uuid, {quote(replay[job[0]])}::uuid)")
    if not values:
        ready = ", ".join(sorted(topic[:30] for topic, job in jobs.items() if job[0] in replay)) or "нет"
        print(f"-- нет строк: у готовых тем повтора ({ready}) нет эталонных сигналов в evidence.csv, "
              "либо темы evidence.csv не совпали с jobs.tsv", file=sys.stderr)
        return 3
    print(f"""
WITH ex(row_id, topic, url, original_id, replay_id) AS (VALUES {', '.join(values)}),
ranked AS (
  SELECT c.candidate_id, c.analysis_id, c.score, c.query_relevance, c.decision, c.decision_reason,
         rank() OVER (PARTITION BY c.analysis_id ORDER BY c.score DESC) AS rank_by_model,
         rank() OVER (PARTITION BY c.analysis_id ORDER BY c.query_relevance DESC) AS rank_by_relevance,
         count(*) OVER (PARTITION BY c.analysis_id) AS candidates
  FROM analyzer.candidates c WHERE c.analysis_id IN (SELECT original_id FROM ex UNION SELECT replay_id FROM ex)
), hits AS (
  SELECT ex.row_id, ex.topic, ex.url, r.analysis_id = ex.original_id AS in_original, r.analysis_id = ex.replay_id AS in_replay,
         r.rank_by_model, r.rank_by_relevance, r.candidates, round(r.score::numeric, 3) AS score,
         round(r.query_relevance::numeric, 3) AS relevance, r.decision, r.decision_reason
  FROM ex
  JOIN collector.documents d ON d.url = ex.url
  JOIN analyzer.candidate_documents cd ON cd.document_id = d.document_id
  JOIN ranked r ON r.candidate_id = cd.candidate_id AND r.analysis_id IN (ex.original_id, ex.replay_id)
)
SELECT ex.row_id, ex.topic,
       bool_or(h.in_original) IS TRUE AS in_original_150,
       bool_or(h.in_replay) IS TRUE AS in_replay,
       min(h.rank_by_model) FILTER (WHERE h.in_replay) AS replay_rank_by_model,
       min(h.rank_by_relevance) FILTER (WHERE h.in_replay) AS replay_rank_by_relevance,
       max(h.candidates) FILTER (WHERE h.in_replay) AS replay_candidates,
       max(h.score) FILTER (WHERE h.in_replay) AS score, max(h.relevance) FILTER (WHERE h.in_replay) AS relevance,
       string_agg(DISTINCT h.decision || '/' || h.decision_reason, ', ') FILTER (WHERE h.in_replay) AS replay_decision
FROM (SELECT DISTINCT row_id, topic FROM ex) ex LEFT JOIN hits h ON h.row_id = ex.row_id AND h.topic = ex.topic
GROUP BY ex.row_id, ex.topic ORDER BY ex.topic, ex.row_id;""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
