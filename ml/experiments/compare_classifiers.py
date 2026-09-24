import json, pathlib, warnings
import numpy as np
warnings.filterwarnings("ignore")
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.svm import LinearSVC
from sklearn.ensemble import RandomForestClassifier, GradientBoostingClassifier
from sklearn.naive_bayes import ComplementNB
from sklearn.dummy import DummyClassifier
from sklearn.pipeline import make_pipeline
from sklearn.model_selection import StratifiedShuffleSplit, RepeatedStratifiedKFold, cross_val_score
from sklearn.metrics import f1_score, accuracy_score, roc_auc_score, precision_score, recall_score

SEED = 20260920
rows = [json.loads(line) for line in
        pathlib.Path("ml/data/labels/dataset_ds-2026.09.19-v2.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()]
texts = [f"{row.get('title', '')} {row.get('description', '')}".strip() for row in rows]
labels = np.array([1 if row.get("label") in (1, "1", True, "weak_signal") else 0 for row in rows])

train_index, test_index = next(StratifiedShuffleSplit(n_splits=1, test_size=0.2, random_state=SEED).split(texts, labels))
x_train = [texts[i] for i in train_index]
x_test = [texts[i] for i in test_index]
y_train, y_test = labels[train_index], labels[test_index]

def vectorizer():
    return TfidfVectorizer(analyzer="word", ngram_range=(1, 2), min_df=2, sublinear_tf=True, max_features=20000)

candidates = {
    "B0 мажоритарный класс": DummyClassifier(strategy="most_frequent"),
    "B1 логистическая регрессия": make_pipeline(vectorizer(), LogisticRegression(max_iter=2000, C=4.0, class_weight="balanced", random_state=SEED)),
    "B2 линейный SVM": make_pipeline(vectorizer(), LinearSVC(C=0.6, class_weight="balanced", random_state=SEED)),
    "B3 ComplementNB": make_pipeline(vectorizer(), ComplementNB(alpha=0.4)),
    "B4 случайный лес": make_pipeline(vectorizer(), RandomForestClassifier(n_estimators=400, class_weight="balanced", random_state=SEED)),
    "B5 градиентный бустинг": make_pipeline(vectorizer(), GradientBoostingClassifier(random_state=SEED)),
}

cv = RepeatedStratifiedKFold(n_splits=5, n_repeats=5, random_state=SEED)
for name, model in candidates.items():
    scores = cross_val_score(model, x_train, y_train, cv=cv, scoring="f1")
    model.fit(x_train, y_train)
    predicted = model.predict(x_test)
    try:
        auc = roc_auc_score(y_test, model.predict_proba(x_test)[:, 1])
    except Exception:
        try:
            auc = roc_auc_score(y_test, model.decision_function(x_test))
        except Exception:
            auc = float("nan")
    print(f"{name:32} CV F1 {scores.mean():.3f} ± {scores.std():.3f} | "
          f"test F1 {f1_score(y_test, predicted):.3f} | acc {accuracy_score(y_test, predicted):.3f} | "
          f"prec {precision_score(y_test, predicted, zero_division=0):.3f} | "
          f"rec {recall_score(y_test, predicted, zero_division=0):.3f} | AUC {auc:.3f}")