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

engine = AutosarHackathonEngine(model_name="llama3.1", load_pdfs=False)

print("E2E validation started")
print("prompt_index|status|yaml_len|arxml_len|safety_issues|error")

for idx, prompt in enumerate(prompts, start=1):
    try:
        yaml_text = engine.generate_simplified_structure(prompt, use_vector_retrieval=True)
        arxml_text = engine.compile_to_arxml(yaml_text)
        issues = engine.safety_check(yaml_text)
        print(f"{idx}|PASS|{len(yaml_text)}|{len(arxml_text)}|{len(issues)}|")
    except Exception as exc:
        print(f"{idx}|FAIL|0|0|0|{type(exc).__name__}: {str(exc).replace('\n', ' ')[:240]}")

print("E2E validation finished")
