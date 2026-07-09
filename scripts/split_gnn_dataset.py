import argparse
import csv
import random
from pathlib import Path


DEFAULT_INPUT = Path("data/gnn_training_seed.csv")
DEFAULT_TRAIN = Path("data/gnn_train.csv")
DEFAULT_VAL = Path("data/gnn_val.csv")
DEFAULT_TEST = Path("data/gnn_test.csv")


def read_rows(path: Path):
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fieldnames = reader.fieldnames or []
    return rows, fieldnames


def write_rows(path: Path, rows, fieldnames):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def group_by_label(rows):
    buckets = {}
    for row in rows:
        label = str(row.get("label", "")).strip()
        buckets.setdefault(label, []).append(row)
    return buckets


def split_bucket(items, train_ratio, val_ratio, rng):
    data = list(items)
    rng.shuffle(data)

    n = len(data)
    train_n = int(round(n * train_ratio))
    val_n = int(round(n * val_ratio))

    # Clamp to valid bounds and keep test non-negative.
    train_n = max(0, min(train_n, n))
    val_n = max(0, min(val_n, n - train_n))

    train_rows = data[:train_n]
    val_rows = data[train_n:train_n + val_n]
    test_rows = data[train_n + val_n:]
    return train_rows, val_rows, test_rows


def split_stratified(rows, train_ratio, val_ratio, seed):
    if train_ratio <= 0 or val_ratio < 0 or train_ratio + val_ratio >= 1:
        raise ValueError("Ratios must satisfy: train > 0, val >= 0, and train + val < 1")

    rng = random.Random(seed)
    grouped = group_by_label(rows)

    train_all = []
    val_all = []
    test_all = []

    for _, items in grouped.items():
        train_rows, val_rows, test_rows = split_bucket(items, train_ratio, val_ratio, rng)
        train_all.extend(train_rows)
        val_all.extend(val_rows)
        test_all.extend(test_rows)

    rng.shuffle(train_all)
    rng.shuffle(val_all)
    rng.shuffle(test_all)
    return train_all, val_all, test_all


def summarize(rows, name):
    total = len(rows)
    labels = {}
    for row in rows:
        label = str(row.get("label", "")).strip()
        labels[label] = labels.get(label, 0) + 1
    print(f"{name}: {total} rows | label distribution: {labels}")


def main():
    parser = argparse.ArgumentParser(description="Split GNN mapping CSV into train/val/test sets.")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--train", type=Path, default=DEFAULT_TRAIN)
    parser.add_argument("--val", type=Path, default=DEFAULT_VAL)
    parser.add_argument("--test", type=Path, default=DEFAULT_TEST)
    parser.add_argument("--train-ratio", type=float, default=0.7)
    parser.add_argument("--val-ratio", type=float, default=0.15)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    rows, fieldnames = read_rows(args.input)
    if not rows:
        raise RuntimeError(f"No rows found in {args.input}")

    train_rows, val_rows, test_rows = split_stratified(
        rows,
        train_ratio=args.train_ratio,
        val_ratio=args.val_ratio,
        seed=args.seed,
    )

    write_rows(args.train, train_rows, fieldnames)
    write_rows(args.val, val_rows, fieldnames)
    write_rows(args.test, test_rows, fieldnames)

    print(f"Input: {args.input}")
    summarize(rows, "All")
    summarize(train_rows, "Train")
    summarize(val_rows, "Val")
    summarize(test_rows, "Test")
    print(f"Wrote: {args.train}")
    print(f"Wrote: {args.val}")
    print(f"Wrote: {args.test}")


if __name__ == "__main__":
    main()
