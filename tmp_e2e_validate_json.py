import json
import time
from core import AutosarHackathonEngine

prompts = [
    "Design an AUTOSAR architecture for brake-by-wire with ASIL D safety goals, dual-channel sensing, and independent safety monitoring ECU.",
    "Create a steering control system with ASIL decomposition (D -> B+B), including freedom-from-interference strategy and watchdog supervision.",
    "Model a fail-operational powertrain controller with degraded torque mode, limp-home behavior, and diagnostic coverage targets.",
    "Build a zonal E/E architecture where safety-critical CAN traffic is protected during Ethernet backbone congestion and gateway faults.",
    "Generate an AUTOSAR design for emergency braking (AEB) with sensor plausibility checks, safe-state transitions, and end-to-end safety mechanisms.",
    "Create a battery management architecture with ASIL C/D paths, cell-monitor redundancy, thermal runaway detection, and safe shutdown logic.",
    "Design a safety communication concept with E2E protection profiles, timeout handling, sequence counters, and CRC strategies across ECUs.",
    "Model an ADAS lane-keeping function with safety monitor arbitration, fallback to warning-only mode, and diagnostic event logging.",
    "Build a central diagnostics and safety evidence pipeline that maps faults to safety goals, ASIL levels, and confirmation tests.",
    "Create a mixed Classic + Adaptive architecture where a safety island supervises SOME/IP services and enforces safe fallback on service failure.",
]

results = {
    "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    "model": "llama3.1",
    "total": len(prompts),
    "cases": [],
}

engine = AutosarHackathonEngine(model_name="llama3.1", load_pdfs=False)

for idx, prompt in enumerate(prompts, start=1):
    row = {
        "index": idx,
        "prompt": prompt,
        "status": "FAIL",
        "yaml_len": 0,
        "arxml_len": 0,
        "safety_issues": 0,
        "error": "",
    }
    try:
        yaml_text = engine.generate_simplified_structure(prompt, use_vector_retrieval=True)
        arxml_text = engine.compile_to_arxml(yaml_text)
        issues = engine.safety_check(yaml_text)
        row["status"] = "PASS"
        row["yaml_len"] = len(yaml_text)
        row["arxml_len"] = len(arxml_text)
        row["safety_issues"] = len(issues)
    except Exception as exc:
        row["error"] = f"{type(exc).__name__}: {str(exc)}"

    results["cases"].append(row)

pass_count = sum(1 for c in results["cases"] if c["status"] == "PASS")
results["pass_count"] = pass_count
results["fail_count"] = results["total"] - pass_count
results["ended_at"] = time.strftime("%Y-%m-%d %H:%M:%S")

with open("e2e_results.json", "w", encoding="utf-8") as f:
    json.dump(results, f, indent=2)

print(json.dumps({
    "total": results["total"],
    "pass_count": results["pass_count"],
    "fail_count": results["fail_count"],
}, indent=2))
