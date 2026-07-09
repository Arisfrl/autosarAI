import csv
import json
import re
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = BASE_DIR / "data"
RAW_OUT = DATA_DIR / "public_seed_raw.jsonl"
SEED_OUT = DATA_DIR / "gnn_training_seed.csv"
MOCK_IN = DATA_DIR / "gnn_mappings_mock.csv"

PUBLIC_SOURCES = [
    {
        "name": "cogu_autosar_readme",
        "url": "https://raw.githubusercontent.com/cogu/autosar/master/README.md",
        "domain": "autosar",
    },
    {
        "name": "covesa_vsomeip_readme",
        "url": "https://raw.githubusercontent.com/COVESA/vsomeip/master/README.md",
        "domain": "someip",
    },
    {
        "name": "commaai_opendbc_readme",
        "url": "https://raw.githubusercontent.com/commaai/opendbc/master/README.md",
        "domain": "can",
    },
    {
        "name": "autosar_site_home",
        "url": "https://www.autosar.org/",
        "domain": "autosar",
    },
]

OUTPUT_COLUMNS = [
    "mapping_id",
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
    "label",
    "source_ref",
    "notes",
]

SIGNAL_HINTS = [
    "Speed",
    "Brake",
    "Temp",
    "Torque",
    "Steering",
    "Yaw",
    "Accel",
    "Battery",
    "Voltage",
    "Current",
    "Door",
    "Lane",
]

SERVICE_HINTS = [
    "Service",
    "Interface",
    "Method",
    "Event",
    "Gateway",
    "Routing",
]


def fetch_text(url: str, timeout: int = 20) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "autosar-ai-seed-collector/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def extract_candidates(text: str):
    token_pattern = re.compile(r"\b[A-Za-z][A-Za-z0-9_]{3,}\b")
    tokens = token_pattern.findall(text)

    signals = []
    services = []

    for token in tokens:
        if any(hint in token for hint in SIGNAL_HINTS):
            signals.append(token)
        if any(hint in token for hint in SERVICE_HINTS):
            services.append(token)

    # Keep order, remove duplicates
    signals = list(dict.fromkeys(signals))[:12]
    services = list(dict.fromkeys(services))[:12]
    return signals, services


def build_rows_from_source(source_name: str, domain: str, signals, services, start_index: int):
    rows = []
    if not signals:
        signals = ["VehicleSpeed", "BrakePressure", "SteeringAngle"]
    if not services:
        services = ["VehicleMotionService", "SafetyGatewayService", "DiagnosticsService"]

    source_protocol = "CAN" if domain in {"can", "autosar"} else "Ethernet"
    target_protocol = "SOME/IP"

    pair_count = min(len(signals), len(services), 6)
    for i in range(pair_count):
        signal_name = signals[i]
        service_name = services[i]
        idx = start_index + i

        valid = 1
        notes = "Public-source-derived weak label"
        latency = 30
        asil = "B"
        if "Brake" in signal_name or "Yaw" in signal_name:
            asil = "D"
            latency = 15
        if "Audio" in service_name or "Media" in service_name:
            valid = 0
            notes = "Likely domain mismatch (auto-generated negative)"
            latency = 120

        rows.append(
            {
                "mapping_id": f"pub_{idx:04d}",
                "signal_name": signal_name,
                "service_name": service_name,
                "source_protocol": source_protocol,
                "target_protocol": target_protocol,
                "data_type": "float32",
                "cycle_time_ms": 10,
                "asil": asil,
                "source_ecu": "ECU_SourceDomain",
                "target_ecu": "ECU_CentralCompute",
                "expected_latency_ms": latency,
                "label": valid,
                "source_ref": source_name,
                "notes": notes,
            }
        )

    return rows


def load_mock_rows(path: Path):
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        return list(reader)


def write_csv(path: Path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    raw_items = []
    generated_rows = []
    next_index = 1

    for source in PUBLIC_SOURCES:
        name = source["name"]
        url = source["url"]
        domain = source["domain"]
        try:
            text = fetch_text(url)
        except Exception as exc:
            raw_items.append({"source": name, "url": url, "error": str(exc)})
            continue

        signals, services = extract_candidates(text)
        raw_items.append(
            {
                "source": name,
                "url": url,
                "signal_candidates": signals,
                "service_candidates": services,
            }
        )

        rows = build_rows_from_source(name, domain, signals, services, next_index)
        generated_rows.extend(rows)
        next_index += len(rows)

    with RAW_OUT.open("w", encoding="utf-8") as f:
        for item in raw_items:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")

    mock_rows = load_mock_rows(MOCK_IN)

    # Keep existing mock mappings and append weakly labeled public rows.
    merged_rows = mock_rows + generated_rows
    write_csv(SEED_OUT, merged_rows)

    print(f"Wrote raw source summary to: {RAW_OUT}")
    print(f"Wrote training seed CSV to: {SEED_OUT}")
    print(f"Rows from mock: {len(mock_rows)}")
    print(f"Rows from public sources: {len(generated_rows)}")
    print(f"Total rows: {len(merged_rows)}")


if __name__ == "__main__":
    main()
