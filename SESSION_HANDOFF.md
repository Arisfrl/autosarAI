# AUTOSAR AI Session Handoff

This file preserves the current working state for continuing the same session in this folder.

## Current project state
- Streamlit app: `streamlit_app.py`
- Core logic: `core.py`
- Local reference docs: `data/` and `AUTOSAR_WorkflowExample/`
- Vector/database storage: `chroma_db/`

## Recent work completed
- Added a chat UI to the Streamlit app
- Implemented local chat responses via Ollama subprocess
- Styled the chat as a compact, scrollable bubble panel
- Added a toggle button to open/close the chat window
- Cleaned response output to remove ANSI escape codes
- Adjusted prompting so general questions are answered directly and concisely

## Main files to continue from
- `streamlit_app.py` — app UI, chat UI, chat handling
- `core.py` — ingestion, retrieval, prompt generation, parsing logic

## Suggested next step
Run the app locally with:

```bash
streamlit run streamlit_app.py
```

## Notes
- The app depends on a local Ollama installation and a usable model.
- If you want to switch providers later, Claude/Anthropic can be added as a separate option.
