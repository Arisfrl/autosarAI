# AUTOSAR AI MDE Hackathon Project

Technical Title
Al-Augmented Model-Driven Engineering (MDE) for Software-Defined Vehicles (SDVs)

## Overview
This project demonstrates a hackathon-ready pipeline that blends local Ollama inference with retrieval-augmented generation to produce simplified AUTOSAR architecture artifacts.

Features
- RAG-backed prompt generation using local AUTOSAR reference material
- YAML output for simplified architecture and service binding
- Compile path from YAML to a standard ARXML-like file
- Safety checklist rules for ISO 26262 / ASIL compliance
- Demo prompt builder for CAN-to-SOME/IP conversion
- Streamlit UI for interactive exploration

## Setup
1. Install Ollama and pull the model:
```bash
ollama pull llama3.1
```
2. Create a Python environment and install requirements:
```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```
3. Run the Streamlit app:
```bash
streamlit run streamlit_app.py
```

## Chroma Retrieval
The project now includes a local Chroma vector store for retrieval-augmented generation. The first app run creates a small embedding database from `data/autosar_reference.md`.

## Demo prompt builder
A sidebar button loads a sample CAN-to-SOME/IP prompt so you can generate a realistic AUTOSAR YAML architecture quickly.
A second sidebar button loads a load optimization demo prompt for multi-core ECU scheduling and ASIL-aware partitioning.

## Usage
- Use the sidebar to reload the Ollama model if needed.
- Press `Load CAN→SOME/IP demo prompt` to populate the request field with a hackathon-ready example.
- Toggle `Use Chroma vector retrieval` to compare vector retrieval against simple keyword retrieval.
- Use the download buttons to export generated YAML and ARXML artifacts from the UI.

## How it works
1. The app loads local AUTOSAR reference notes from `data/autosar_reference.md`.
2. A lightweight retrieval function selects the most relevant sections for a user request.
3. Ollama generates a simplified YAML structure from the combined prompt.
4. The generated YAML is compiled into an ARXML-style output using Jinja2.
5. A basic safety validator checks compliance hints and flags potential issues.

## Notes
- This is a hackathon scaffold, not a production-grade AUTOSAR generator.
- The `ollama` CLI must be available on the system path.
- The generated ARXML is a simplified demonstration artifact.

## Optional improvements
- Add a real vector store with embeddings and Chroma
- Expand ISO 26262 rule checks
- Add support for real AUTOSAR ARXML packages and XML schema validation
- Plug in official AUTOSAR documents as reference sources
