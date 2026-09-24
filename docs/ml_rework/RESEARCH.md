# Первоисточники и как они использованы

| Источник | Что взято | Где применено |
|---|---|---|
| Kirkpatrick J. et al. Overcoming catastrophic forgetting in neural networks. PNAS 114(13):3521–3526, 2017. doi:10.1073/pnas.1611835114 | квадратичный штраф к параметрам прошлой задачи с весами из информации Фишера | `seq_laplace` (`ml/src/ml/seq/stages.py`) |
| Huszár F. Note on the quadratic penalties in elastic weight consolidation. PNAS 115(11), 2018. doi:10.1073/pnas.1717042115 | корректная форма — рекурсивное лапласовское приближение апостериорного распределения, а не сумма отдельных штрафов | гессиан Stage A в точке θ_A пересчитывается в пространстве Stage B и проверяется (`parent_hessian_recomputed_max_abs_diff`) |
| Li X., Grandvalet Y., Davoine F. Explicit inductive bias for transfer learning with CNNs (L2-SP). ICML 2018 | штраф к стартовой точке вместо к нулю | `seq_l2sp`. Ссылка по памяти, в этой сессии поиском не перепроверена |
| Mühlroth C., Grottke M. A systematic literature review of mining weak signals and trends for corporate foresight. J. Business Economics 88(5):643–687, 2018. doi:10.1007/s11573-018-0898-4 | обзор 91 работы: слабый сигнал = раннее, фрагментарное, но проверяемое свидетельство; важны источники и новизна | рубрика B (LABELING_PROTOCOL.md), группы признаков specificity/metadata |
| EC Joint Research Centre, Tools for Innovation Monitoring (TIM), Moro et al. | мониторинг по публикациям, патентам, проектам; типы источников как сигнал зрелости | доли источников в признаках `share_*` |
| Yoon J., Kim K. Detecting signals of new technological opportunities using semantic patent analysis and outlier detection. Scientometrics 90, 2012. doi:10.1007/s11192-011-0543-2 | ранние сигналы как выбросы относительно зрелого корпуса | обоснование `specificity` и отказ от центроидов «зрелое/слабое» |
| Signitrend (Schubert et al., KDD 2014); keyword network + GCN (Futures, 2023, S0016328723001064); arXiv:2205.05449 | обнаружение по временным рядам и графам терминов | не применены: нет временных рядов упоминаний; отмечено как направление |
| scikit-learn: Common pitfalls (https://scikit-learn.org/stable/common_pitfalls.html) | преобразования обучаются только на train, иначе утечка | стандартизация A — по A-train, признаков темы — по B-train фолда; PCA — только A-train |
| scikit-learn: LogisticRegression, `warm_start` | тёплый старт — только инициализация решателя | контроль `seq_finetune`: сходится к b_only (тест) |
| scikit-learn: SGDClassifier / `partial_fit` | возможный механизм инкрементального обучения | не выбран: без явного штрафа к θ_A не защищает знания A; L-BFGS + якорь даёт точную цель и гессиан |
| RFC 9110 §10.2.3 Retry-After | две формы: delta-seconds и HTTP-date | `collector/domain/retry_after.py` |
