import argparse
import csv
import json
import pickle
from pathlib import Path

from sklearn.feature_extraction import DictVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, f1_score, precision_score, recall_score
from sklearn.pipeline import Pipeline


DEFAULT_TRAIN = Path("data/gnn_train.csv")
DEFAULT_VAL = Path("data/gnn_val.csv")
DEFAULT_TEST = Path("data/gnn_test.csv")
DEFAULT_MODEL_OUT = Path("models/gnn_mapping_baseline.pkl")
DEFAULT_METRICS_OUT = Path("models/gnn_mapping_baseline_metrics.json")

NUMERIC_COLS = ["cycle_time_ms", "expected_latency_ms"]
CATEGORICAL_COLS = [
    "signal_name",
    "service_name",
    "source_protocol",
    "target_protocol",
    "data_type",
    "asil",
    "source_ecu",
    "target_ecu",
]


def read_csv(path: Path):
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        return list(reader)


def as_float(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return default


def prepare_xy(rows):
    x = []
    y = []
    for row in rows:
        features = {}
        for col in NUMERIC_COLS:
            features[col] = as_float(row.get(col, 0.0), default=0.0)
        for col in CATEGORICAL_COLS:
            features[col] = str(row.get(col, "")).strip()

        label_raw = str(row.get("label", "")).strip()
        if label_raw not in {"0", "1"}:
            continue

        x.append(features)
        y.append(int(label_raw))
    return x, y


def build_pipeline():
    model = LogisticRegression(
        max_iter=1500,
        class_weight="balanced",
        solver="liblinear",
        random_state=42,
    )

    return Pipeline(
        steps=[
            ("vectorize", DictVectorizer(sparse=False)),
            ("model", model),
        ]
    )


def evaluate(name, pipeline, x, y):
    preds = pipeline.predict(x)
    return {
        "split": name,
        "rows": len(y),
        "accuracy": round(float(accuracy_score(y, preds)), 4),
        "precision": round(float(precision_score(y, preds, zero_division=0)), 4),
        "recall": round(float(recall_score(y, preds, zero_division=0)), 4),
        "f1": round(float(f1_score(y, preds, zero_division=0)), 4),
    }


def main():
    parser = argparse.ArgumentParser(description="Train a tabular baseline for Signal-to-Service mapping labels.")
    parser.add_argument("--train", type=Path, default=DEFAULT_TRAIN)
    parser.add_argument("--val", type=Path, default=DEFAULT_VAL)
    parser.add_argument("--test", type=Path, default=DEFAULT_TEST)
    parser.add_argument("--model-out", type=Path, default=DEFAULT_MODEL_OUT)
    parser.add_argument("--metrics-out", type=Path, default=DEFAULT_METRICS_OUT)
    args = parser.parse_args()

    train_rows = read_csv(args.train)
    val_rows = read_csv(args.val)
    test_rows = read_csv(args.test)

    x_train, y_train = prepare_xy(train_rows)
    x_val, y_val = prepare_xy(val_rows)
    x_test, y_test = prepare_xy(test_rows)

    if not x_train:
        raise RuntimeError("Training data is empty after parsing labels.")

    pipeline = build_pipeline()
    pipeline.fit(x_train, y_train)

    metrics = {
        "train": evaluate("train", pipeline, x_train, y_train),
        "val": evaluate("val", pipeline, x_val, y_val) if x_val else None,
        "test": evaluate("test", pipeline, x_test, y_test) if x_test else None,
    }

    args.model_out.parent.mkdir(parents=True, exist_ok=True)
    with args.model_out.open("wb") as f:
        pickle.dump(pipeline, f)

    args.metrics_out.parent.mkdir(parents=True, exist_ok=True)
    with args.metrics_out.open("w", encoding="utf-8") as f:
        json.dump(metrics, f, indent=2)

    print(f"Model saved: {args.model_out}")
    print(f"Metrics saved: {args.metrics_out}")
    print(json.dumps(metrics, indent=2))


if __name__ == "__main__":
    main()
