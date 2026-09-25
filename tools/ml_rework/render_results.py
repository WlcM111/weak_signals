#!/usr/bin/env python3
"""Отчёт RESULTS.md по каталогу прогона seq-experiments: python3 tools/ml_rework/render_results.py <run_dir> [out.md]."""
import json
import platform
import sys
from pathlib import Path


def f(x):
    return "—" if x is None else f"{x:.3f}".replace(".", ",")


def ci(c):
    return "—" if not c or c[0] is None else f"[{f(c[0])}; {f(c[1])}]"


def params(p):
    p = json.loads(p) if isinstance(p, str) else p
    return ", ".join(f"{k}={v:g}" for k, v in p.items())


def main() -> int:
    run = Path(sys.argv[1])
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else run / "RESULTS.md"
    m = json.loads((run / "metrics.json").read_text(encoding="utf-8"))
    cv, fg, fin, bd = m["cv_best"], m["forgetting_a_dev"], m["final"]["systems"], m["baselines_dev"]
    ch, acc, cnt = m["chosen_on_dev"], m["acceptance"], m["data"]["counts"]
    hashing = m["data"]["embedding_model"].startswith("hashing")
    rows = []

    def row(label, dev, devci, adev, hold, atest):
        rows.append(f"| {label} | {f(dev.get('pr_auc'))} {devci} | {f(dev.get('roc_auc'))} | {f(dev.get('p_at_3'))} | "
                    f"{f(adev)} | {f(hold.get('pr_auc'))} {ci(hold.get('pr_auc_ci_rows'))} | {f(hold.get('roc_auc'))} | "
                    f"{f(hold.get('p_at_3'))} | {f(atest)} |")

    row("Производственный score (baseline)", bd["production_score"], "", None, fin["production_score"]["b_holdout"], None)
    row("e5-близость к запросу из журналов", bd["production_emb_sim_query"], "", None,
        fin["production_emb_sim_query"]["b_holdout"], None)
    hy = bd["stage_a_plus_emb_sim_query_equal_z"]
    rows.append(f"| Гибрид Stage A + e5-близость, без обучения на B | {f(hy['pr_auc'])} | {f(hy['roc_auc'])} | "
                f"{f(hy['p_at_3'])} | — | — | — | — | — |")
    labels = {"stage_a": "Stage A (zero-shot)", "b_only": "B-only", "seq_finetune_early": "A→B без защиты, 5 итераций",
              "seq_finetune": "A→B без защиты, до сходимости", "seq_l2sp": "A→B L2-SP", "seq_laplace": "A→B Лаплас/EWC",
              "seq_laplace_replay": "A→B Лаплас + replay A-train", "joint": "Joint A+B"}
    for key, label in labels.items():
        text = label + (f" ({params(cv[key]['params'])})" if cv[key].get("params") else "")
        text += " **— выбран на dev**" if key == ch else ""
        row(text, cv[key], ci(m["dev_topic_bootstrap_pr_auc"][key]), fg[key]["roc_auc"], fin[key]["b_holdout"],
            fin[key]["a_test"]["roc_auc"])
    pair = "\n".join(f"| {k} | {f(v['pr_auc']['delta'])} [{f(v['pr_auc']['ci_low'])}; {f(v['pr_auc']['ci_high'])}] | "
                     f"{f(v['roc_auc']['delta'])} [{f(v['roc_auc']['ci_low'])}; {f(v['roc_auc']['ci_high'])}] |"
                     for k, v in m["final"]["paired_vs_production"].items())
    dvp = acc["dev_vs_production"]
    abl = "\n".join(f"| {r['ablation']} | {f(r.get('pr_auc'))} | {f(r.get('roc_auc'))} |" for r in m["ablations_dev"])
    lc = {}
    for r in m["learning_curve_dev"]:
        lc.setdefault((r["system"], r["fraction"]), []).append(r["pr_auc"] or 0)
    mean = lambda v: sum(v) / len(v)  # noqa: E731
    lct = "\n".join(f"| {int(fr * 100)} % | {f(mean(lc[('candidate', fr)]))} | {f(mean(lc[('b_only', fr)]))} |"
                    for fr in (0.25, 0.5, 0.75, 1.0))
    nz = {}
    for r in m["label_noise_dev"]:
        nz.setdefault(r["system"], []).append(r["pr_auc"] or 0)
    cal = m["calibration"][ch]
    note = ("**Главное ограничение:** хеширующий эмбеддер — признаки релевантности и эмбеддинга v2 не отражают смысл "
            "(темы на русском, названия на английском); единственный e5-сигнал — `rel_cluster_query_v1` из журналов. "
            "Прогон проверяет механику и протокол; качество на e5 определяет прогон на Mac.") if hashing else (
            "Эмбеддер производственный: результаты сопоставимы с работой analyzer.")
    text = f"""# Результаты экспериментов A → B — прогон `{m['run']}`

## Что запускалось
Эмбеддер `{m['data']['embedding_model']}`, seed {m['data']['seed']}, время {m['duration_seconds']} с, Python {platform.python_version()} (машина отчёта). Файлы прогона: `metrics.json`, `cv_table.csv`, `predictions_dev_oof.csv`, `predictions_holdout.csv`, `checkpoints/`, `data_manifest.json`, `run_manifest.json` (SHA-256).

{note}

## Данные
Датасет B: `{m['data']['b_file'].rsplit('/', 1)[-1]}`, SHA-256 `{m['data']['b_sha256'][:12]}…`.

A-train {cnt['a_train']['rows']} ({cnt['a_train']['positives']} поз.), A-dev {cnt['a_dev']['rows']} ({cnt['a_dev']['positives']}), A-test {cnt['a_test']['rows']} ({cnt['a_test']['positives']}); B-dev {cnt['b_dev']['rows']} ({cnt['b_dev']['positives']} поз., {cnt['b_dev']['topics']} тем, 5 фолдов по темам); B-holdout {cnt['b_holdout']['rows']} ({cnt['b_holdout']['positives']} поз., {cnt['b_holdout']['topics']} темы). Доля позитивов dev (PR-AUC случайного порядка) = {f(cnt['b_dev']['positives'] / cnt['b_dev']['rows'])}.

## Baseline → candidate
Dev — внефолдовые оценки, ДИ 95 % — бутстрэп по темам; производственные базовые линии — только живые строки ({dvp['rows']} на dev). Holdout — ДИ по группам. A-dev — забывание после Stage B; A-test — финальная оценка на A.

| Система | dev PR-AUC [ДИ] | dev ROC-AUC | dev P@3 | A-dev ROC-AUC | holdout PR-AUC [ДИ] | holdout ROC-AUC | holdout P@3 | A-test ROC-AUC |
|---|---|---|---|---|---|---|---|---|
{chr(10).join(rows)}

Парные разности с производственным score на holdout ({m['final']['holdout_live_rows']} живых строк):

| Разность | ΔPR-AUC [ДИ] | ΔROC-AUC [ДИ] |
|---|---|---|
{pair}

Dev, `{ch}` − производственный score ({dvp['rows']} строк, бутстрэп по темам): ΔPR-AUC {f(dvp['pr_auc']['delta'])} [{f(dvp['pr_auc']['ci_low'])}; {f(dvp['pr_auc']['ci_high'])}], ΔROC-AUC {f(dvp['roc_auc']['delta'])} [{f(dvp['roc_auc']['ci_low'])}; {f(dvp['roc_auc']['ci_high'])}].

## Сохранение знаний A
Stage A: λ = {m['stage_a']['params']['l2']:g}, ROC-AUC A-dev {f(m['stage_a']['a_dev']['roc_auc'])}. После Stage B: без защиты {f(fg['seq_finetune']['roc_auc'])}, B-only {f(fg['b_only']['roc_auc'])}; L2-SP {f(fg['seq_l2sp']['roc_auc'])}, Лаплас {f(fg['seq_laplace']['roc_auc'])}, Лаплас + replay {f(fg['seq_laplace_replay']['roc_auc'])}, joint {f(fg['joint']['roc_auc'])}. Длина названия одна: ROC-AUC {f(m['a_style_diagnostic']['a_dev_roc_auc_title_length_only'])} на A-dev (стиль авторов A).

## Абляции (dev, `{ch}`)
| Абляция | PR-AUC | ROC-AUC |
|---|---|---|
{abl}

## Кривая обучения и шум меток (dev PR-AUC, среднее по сидам)
| Доля групп B-train | `{ch}` | B-only |
|---|---|---|
{lct}

10 % перевёрнутых меток: `{ch}` {f(mean(nz['candidate']))}, B-only {f(mean(nz['b_only']))}.

## Калибровка `{ch}`
Платт: наклон {f(cal['platt']['slope'])}, сдвиг {f(cal['platt']['offset'])}; порог max-F1 {f(cal['threshold'])}; ECE dev {f(cal['dev_ece_5bins'])} (те же данные); holdout: ECE {f(fin[ch]['b_holdout']['ece_5bins'])}, Brier {f(fin[ch]['b_holdout']['brier'])}.

## Решение приёмки
`{acc['decision']}` — {', '.join(f'{k} = {v}' for k, v in acc['checks'].items())}. Вычислений этого holdout (тот же набор строк) в каталоге отчётов, включая этот прогон: {m['final']['holdout_uses_including_this_run']}.
"""
    out.write_text(text, encoding="utf-8")
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
