# Реестр файлов патча

Относительно архива `weak_signals_stable.zip` (базовый коммит «base: weak_signals_stable.zip as delivered»). SHA-256 — первые 16 символов, полный манифест — `SHA256SUMS` поставки. Удалённых файлов нет. Не входят в патч: `.env`, результаты экспериментов (`artifacts/`, `ml/reports/experiments/`), кеши эмбеддингов.

| Изменение | Файл | Строки | SHA-256 |
|---|---|---|---|
| изменён | `.env.example` | +6 / −1 | d5ea3b9216a4b6bd |
| изменён | `.gitignore` | +4 / −0 | 5ca111c14ea31619 |
| изменён | `CHANGELOG_CONTRACTS.md` | +5 / −0 | 92b5c1ae0a831fdc |
| изменён | `Makefile` | +3 / −1 | 5f39bb7a5da6a7f7 |
| новый | `RUNBOOK_MACOS.md` | +261 / −0 | b55f7a4ee2650e4c |
| новый | `docs/ml/MODEL_CARD_v2.md` | +13 / −0 | 5e297b39764c3624 |
| новый | `docs/ml_rework/ARCHITECTURE.md` | +46 / −0 | 62cbcabff4713893 |
| новый | `docs/ml_rework/AUDIT.md` | +41 / −0 | 6c9739a58f090e84 |
| новый | `docs/ml_rework/CHANGELOG.md` | +8 / −0 | 97cf9a1accc13cbd |
| новый | `docs/ml_rework/HYPOTHESES.md` | +25 / −0 | a84c060450a3e978 |
| новый | `docs/ml_rework/MIGRATION_ROLLBACK.md` | +23 / −0 | 7883206359994495 |
| новый | `docs/ml_rework/PROGRESS.md` | +16 / −0 | e3a2a08db63907b0 |
| новый | `docs/ml_rework/PROTOCOL.md` | +57 / −0 | c7e9f24e0df37803 |
| новый | `docs/ml_rework/REQUIREMENTS_MATRIX.md` | +44 / −0 | 566ca26c0b36e16e |
| новый | `docs/ml_rework/RESEARCH.md` | +15 / −0 | 86c1eb0d36b3035c |
| новый | `docs/ml_rework/RESULTS.md` | +79 / −0 | 9138283cc7df0eea |
| новый | `ml/data/dataset_b/DATASET_CARD.md` | +112 / −0 | 8f87411c4d8016cd |
| новый | `ml/data/dataset_b/LABELING_PROTOCOL.md` | +23 / −0 | dcd39d04bfc947a1 |
| новый | `ml/data/dataset_b/dataset_b_v1.jsonl` | +153 / −0 | 0ffb9a91796ca555 |
| новый | `ml/data/dataset_b/dataset_b_v1_uncertain.jsonl` | +18 / −0 | f2f614f2d61ad704 |
| новый | `ml/data/dataset_b/duplicates_manifest_v1.jsonl` | +466 / −0 | e64d37be4964a61b |
| новый | `ml/data/dataset_b/evidence_manifest_v1.csv` | +567 / −0 | fa1f7086ae3ffee5 |
| новый | `ml/data/dataset_b/excluded_candidates_observed_v1.jsonl` | +1475 / −0 | e3882b90d27f8a86 |
| новый | `ml/data/dataset_b/labels_silver_v1.jsonl` | +159 / −0 | 9da8ed8481d13f88 |
| новый | `ml/data/dataset_b/splits_v1.json` | +202 / −0 | d568f18d3caec9d7 |
| новый | `ml/data/dataset_b/validation_report_v1.json` | +126 / −0 | f7a3b79f7e4a1462 |
| новый | `ml/data/dataset_b/web_observations_v1.jsonl` | +12 / −0 | 5a7f1cfd9ffabc37 |
| изменён | `ml/src/ml/cli.py` | +3 / −0 | c855ebe274f9c3cb |
| новый | `ml/src/ml/dataset_b/__init__.py` | +1 / −0 | ba35a70f97d800a9 |
| новый | `ml/src/ml/dataset_b/build.py` | +282 / −0 | f39b3c2822cf045f |
| новый | `ml/src/ml/dataset_b/grouping.py` | +63 / −0 | 36c1d714b2936321 |
| новый | `ml/src/ml/dataset_b/runs_parser.py` | +205 / −0 | d379912c5bee214a |
| новый | `ml/src/ml/seq/__init__.py` | +1 / −0 | 19dbfb6d6a0521ce |
| новый | `ml/src/ml/seq/cli.py` | +211 / −0 | b199f3cbbfdfc979 |
| новый | `ml/src/ml/seq/data.py` | +148 / −0 | 952089781f0adc84 |
| новый | `ml/src/ml/seq/experiments.py` | +428 / −0 | f29a8f491de92ddb |
| новый | `ml/src/ml/seq/export.py` | +128 / −0 | 7e5b0ace22db337d |
| новый | `ml/src/ml/seq/featurize.py` | +108 / −0 | a9aa8dd4fbf30608 |
| новый | `ml/src/ml/seq/linear.py` | +103 / −0 | f0009916c9c1e2c5 |
| новый | `ml/src/ml/seq/metrics.py` | +153 / −0 | aa5f906c95084312 |
| новый | `ml/src/ml/seq/stages.py` | +243 / −0 | 7ad64137fa43481f |
| новый | `ml/tests/test_seq_pipeline.py` | +237 / −0 | c38d169f7d28e44d |
| новый | `schemas/dataset_b_row.schema.json` | +220 / −0 | b133051a890ae725 |
| новый | `schemas/feature_registry_v2.json` | +450 / −0 | d92d40eb515a5a60 |
| новый | `schemas/model_manifest_v2.schema.json` | +77 / −0 | e51c412a8b245195 |
| новый | `services/analyzer/config/glossary_ru_en.yaml` | +61 / −0 | a03ab4956faded77 |
| изменён | `services/analyzer/src/analyzer/adapters/outbound/model_store.py` | +73 / −0 | 377cb85e22bbc2f9 |
| новый | `services/analyzer/src/analyzer/adapters/outbound/v2_model.py` | +72 / −0 | 270ea954fdfe41e2 |
| изменён | `services/analyzer/src/analyzer/application/dto.py` | +3 / −0 | 802b27ada2194ffe |
| изменён | `services/analyzer/src/analyzer/application/use_cases/activate_model.py` | +3 / −2 | c4dca32806950642 |
| изменён | `services/analyzer/src/analyzer/application/use_cases/run_analysis.py` | +55 / −0 | ea5c2dff872b31de |
| изменён | `services/analyzer/src/analyzer/application/use_cases/score_text.py` | +16 / −0 | b37ad73aabb5f4cf |
| изменён | `services/analyzer/src/analyzer/config.py` | +2 / −0 | 382e577dc160c4bc |
| изменён | `services/analyzer/src/analyzer/domain/feature_registry.py` | +8 / −5 | 55c99427ba3f203f |
| новый | `services/analyzer/src/analyzer/domain/features_v2.py` | +253 / −0 | a2ddcf367acdeaf1 |
| изменён | `services/analyzer/src/analyzer/main.py` | +15 / −1 | 6edabc420282e8b3 |
| изменён | `services/analyzer/tests/unit/test_registry_validation.py` | +7 / −1 | 7553dd13dfb9f9fa |
| изменён | `services/collector/src/collector/adapters/outbound/http_client.py` | +19 / −7 | c073564c27be42eb |
| изменён | `services/collector/src/collector/adapters/outbound/sources/arxiv.py` | +4 / −3 | 32105d25fe18ffb0 |
| новый | `services/collector/src/collector/domain/retry_after.py` | +30 / −0 | d3374407f2c1ef81 |
| изменён | `services/collector/tests/unit/test_arxiv_gate.py` | +4 / −2 | 546dfdcb455b7eb4 |
| новый | `services/collector/tests/unit/test_retry_after.py` | +51 / −0 | 5b4b050849dbdade |
| изменён | `services/orchestrator/src/orchestrator/application/stages/analyze.py` | +4 / −0 | 5a106f26c635290b |
| изменён | `services/orchestrator/src/orchestrator/application/stages/narrate.py` | +9 / −4 | a7b8eb2be84ec05b |
| изменён | `services/orchestrator/src/orchestrator/application/use_cases/run_job.py` | +41 / −5 | 64151f0773a17bc7 |
| изменён | `services/orchestrator/src/orchestrator/config.py` | +2 / −0 | 443d631a36429daf |
| изменён | `services/orchestrator/src/orchestrator/main.py` | +1 / −0 | 5bb84f64d14a3354 |
| изменён | `services/orchestrator/tests/unit/test_job_deadline.py` | +6 / −5 | 40dd17e4b403e952 |
| новый | `services/orchestrator/tests/unit/test_judge_order_and_deadline_caps.py` | +84 / −0 | 88dd5fa60a34d038 |
| новый | `tools/ml_rework/collect_results.sh` | +35 / −0 | 3bb6e2817f602fe4 |
| новый | `tools/ml_rework/render_results.py` | +121 / −0 | 5b711b449d46adeb |
| новый | `docs/ml_rework/FILE_REGISTRY.md` | этот файл | — |
