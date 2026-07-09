import argparse
import csv
import json
import pickle
from pathlib import Path


DEFAULT_MODEL = Path("models/gnn_mapping_baseline.pkl")

REQUIRED_FIELDS = [
    "signal_name",
    "service_name",
    "source_protocol",
    "target_protocol",
    "data_type",
    "cycle_time_ms",
    "asil",
    "source_ecu",
    "target_ecu",
    "expected_latency_ms",
]


def load_model(path: Path):
    if not path.exists():
        raise FileNotFoundError(f"Model file not found: {path}")
    with path.open("rb") as f:
        return pickle.load(f)


def normalize_row(row: dict) -> dict:
    normalized = {}
    for key in REQUIRED_FIELDS:
        value = row.get(key, "")
        if key in {"cycle_time_ms", "expected_latency_ms"}:
            try:
                normalized[key] = float(value)
            except Exception:
                normalized[key] = 0.0
        else:
            normalized[key] = str(value).strip()
    return normalized


def read_first_csv_row(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"CSV file not found: {path}")
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            return row
    raise RuntimeError(f"No rows found in CSV: {path}")


def build_row_from_args(args) -> dict:
    return {
        "signal_name": args.signal_name,
        "service_name": args.service_name,
        "source_protocol": args.source_protocol,
        "target_protocol": args.target_protocol,
        "data_type": args.data_type,
        "cycle_time_ms": args.cycle_time_ms,
        "asil": args.asil,
        "source_ecu": args.source_ecu,
        "target_ecu": args.target_ecu,
        "expected_latency_ms": args.expected_latency_ms,
    }


def main():
    parser = argparse.ArgumentParser(description="Predict mapping validity using trained baseline model.")
    parser.add_argument("--model", type=Path, default=DEFAULT_MODEL)

    parser.add_argument("--input-csv", type=Path, default=None, help="CSV file; first row is used for prediction")

    parser.add_argument("--signal-name", default="WheelSpeed_FL")
    parser.add_argument("--service-name", default="VehicleMotionService")
    parser.add_argument("--source-protocol", default="CAN")
    parser.add_argument("--target-protocol", default="SOME/IP")
    parser.add_argument("--data-type", default="float32")
    parser.add_argument("--cycle-time-ms", type=float, default=10.0)
    parser.add_argument("--asil", default="D")
    parser.add_argument("--source-ecu", default="ECU_WheelSensor")
    parser.add_argument("--target-ecu", default="ECU_CentralCompute")
    parser.add_argument("--expected-latency-ms", type=float, default=15.0)

    args = parser.parse_args()

    model = load_model(args.model)

    if args.input_csv is not None:
        row = read_first_csv_row(args.input_csv)
    else:
        row = build_row_from_args(args)

    features = normalize_row(row)

    prediction = int(model.predict([features])[0])
    label = "valid_mapping" if prediction == 1 else "invalid_or_unsafe_mapping"

    confidence = None
    if hasattr(model, "predict_proba"):
        probs = model.predict_proba([features])[0]
        confidence = float(probs[prediction])

    output = {
        "prediction": prediction,
        "label": label,
        "confidence": round(confidence, 4) if confidence is not None else None,
        "features": features,
    }

    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
