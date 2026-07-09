# AUTOSAR Reference Notes

## Classic Platform
- Classic Platform uses ECUs with AUTOSAR OS, COM, and RTE.
- Signals are often bit-packed over CAN and LIN buses.
- Traditional communication stacks use PDU, PDUR, COM, and CANIF.

## Adaptive Platform
- Adaptive Platform uses SOME/IP services, ara::com, and C++14.
- Service interfaces are described with service IDs and payload schemas.
- Ethernet and IP networking are common.

## ISO 26262 Safety
- ASIL levels A, B, C, D define functional safety requirements.
- Safety mechanisms include redundancy, watchdogs, end-to-end checks, and fault containment.
- Safety validation should identify signal routing deadlocks, invalid data paths, and incorrect service partitioning.

## RAG Guidance
- Retrieval-Augmented Generation uses local reference docs and the user request to ground the model.
- Use a simplified YAML abstraction for architecture design, then compile into concrete artifacts.
