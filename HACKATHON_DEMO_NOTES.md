# AUTOSAR AI MDE Hackathon Demo Notes

## Slide 1: Title
Talk track:
This project is an AI-augmented AUTOSAR Model-Driven Engineering workflow for Software-Defined Vehicles. The key message is that we turn natural-language intent into structured AUTOSAR artifacts, validate them, and demonstrate the result through both engineering outputs and an HMI layer.

## Slide 2: Problem We Are Solving
Talk track:
AUTOSAR design is difficult because teams work across Classic CAN and Adaptive SOME/IP ecosystems while also needing safety evidence. In practice, architecture work starts from informal requirements and gets translated manually into multiple artifacts. That is slow, error-prone, and hard to validate early.

## Slide 3: What This Project Delivers
Talk track:
We built a pipeline that generates AUTOSAR YAML from natural language, compiles it into both Adaptive and Classic ARXML, and runs safety checks on top. The app also supports ARXML upload, retrieval, AI suggestions, and a demo HMI cluster to connect backend artifacts with user-visible behavior.

## Slide 4: End-to-End Workflow
Talk track:
The workflow is prompt to retrieval, retrieval to YAML, YAML to ARXML, and then ARXML plus YAML to safety and demo outputs. This makes the system easy to explain to judges because each step has a visible output and each output can be validated or exported.

## Slide 5: How AI Is Helping
Talk track:
AI is not used here as a generic chatbot. It is grounded with AUTOSAR references through retrieval, then used to structure the architecture into system, ECUs, services, signals, and safety. We also use an AI suggestion stage and an ML baseline to improve or block risky mappings.

## Slide 6: What Is In The Repo
Talk track:
The codebase is split cleanly between core logic and the Streamlit UI. The repo also includes local AUTOSAR references, example ARXML, ML scripts and metrics, tenant-scoped audit logs, and a demo HMI page. This is important because it shows the project is not just mock UI; it has real logic and artifacts behind it.

## Slide 7: Safety, Governance, and Validation
Talk track:
A major differentiator is that we do not stop at generation. We validate structure, ASIL coverage, hardware bindings, services, signals, and mapping risk. We also enforce tenant-scoped access and audit logs, which makes the demo more realistic for automotive environments.

## Slide 8: Current Evidence From The Project
Talk track:
The ML baseline already shows usable signal-to-service mapping performance, with strong validation and acceptable test results for a hackathon baseline. The current end-to-end results show the main limitation is model runtime reliability, especially Ollama timeout behavior, not the pipeline structure itself.

## Slide 9: Live Demo Story
Talk track:
In the demo, log in as a tenant user, load a prompt, generate YAML, compile both Adaptive and Classic ARXML, and run the safety check. Then show the HMI cluster page, where ARXML-driven telltale authorization demonstrates how engineering outputs can connect to a user-facing validation artifact.

## Slide 10: Demo Screenshots
Talk track:
This slide gives judges a quick visual of the two most important demo surfaces. On the left is the authenticated Streamlit console driving generation and review. On the right is the HMI cluster that uses ARXML-driven logic for telltale control.

## Slide 11: Compile and Safety Evidence
Talk track:
This screenshot proves the app is generating more than one artifact. We can show both Classic and Adaptive ARXML outputs and then immediately run safety validation. That combination of generation plus governance is the main technical strength of the demo.

## Slide 12: Why This Matters
Talk track:
The value is speed, consistency, and earlier safety visibility. Teams can explore AUTOSAR architectures faster, across both Classic and Adaptive concerns, while keeping a clearer link to validation and runtime communication concerns.

## Slide 13: Judging Summary
Talk track:
If I summarize this for judging: it is innovative because it integrates multiple AI-assisted steps into one AUTOSAR flow; it is technically credible because it produces actual artifacts and checks them; it is impactful because it reduces design friction; and it is demoable because every stage is visible in the UI.

## Slide 14: Next Steps
Talk track:
The next step is to deepen the AUTOSAR specificity: richer schema support, more robust local inference, larger mapping datasets, stronger safety semantics, and enterprise authentication. The hackathon result proves the workflow direction; the next phase is industrialization.

## Suggested 3-Minute Flow
1. Open with the problem: AUTOSAR is fragmented, manual, and safety-heavy.
2. Show the solution flow: prompt to YAML to dual ARXML to safety to HMI.
3. Highlight what AI does: grounded generation, suggestions, mapping pre-check.
4. Show visual proof: Streamlit screenshot and HMI screenshot.
5. Close with impact: faster architecture iteration and earlier safety insight.
