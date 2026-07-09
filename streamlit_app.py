import re
import html
import streamlit as st
from core import AutosarHackathonEngine, _parse_autosar_arxml_text
import json
import subprocess
import time

ansi_escape = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")

def clean_model_text(text: str) -> str:
    text = ansi_escape.sub("", text)
    # Remove control characters except newline
    return "".join(ch for ch in text if ch == "\n" or ord(ch) >= 32)


def render_chat_html(messages):
    html_parts = [
        '<div class="chat-panel">',
        '<div class="chat-header">Chat with AUTOSAR Assistant</div>',
        '<div class="chat-body">',
    ]
    for m in messages:
        role = m.get("role", "user")
        text = html.escape(m.get("text", "")).replace("\n", "<br>")
        bubble_class = "user" if role == "user" else "assistant"
        title = "You" if role == "user" else "Assistant"
        html_parts.append(
            f'<div class="chat-bubble {bubble_class}"><strong>{title}:</strong><br>{text}</div>'
        )
    html_parts.append("</div>")
    html_parts.append("</div>")
    return "".join(html_parts)


st.set_page_config(page_title="AUTOSAR AI MDE", layout="wide")
st.markdown(
    """
    <style>
    .chat-panel {
        background: #f7f9ff;
        padding: 18px;
        border-radius: 22px;
        box-shadow: 0 16px 40px rgba(15, 23, 42, 0.08);
    }
    .chat-header {
        font-size: 18px;
        font-weight: 700;
        margin-bottom: 16px;
        color: #102a43;
    }
    .chat-body {
        max-height: 520px;
        min-height: 320px;
        overflow-y: auto;
        padding-right: 8px;
        margin-bottom: 14px;
    }
    .chat-bubble {
        padding: 14px 16px;
        margin: 10px 0;
        max-width: 100%;
        border-radius: 20px;
        line-height: 1.6;
        font-size: 14px;
        word-wrap: break-word;
        box-shadow: 0 8px 20px rgba(15, 23, 42, 0.06);
    }
    .chat-bubble.user {
        background: #dbe9ff;
        color: #0f172a;
        margin-left: auto;
        border-bottom-right-radius: 6px;
    }
    .chat-bubble.assistant {
        background: #ffffff;
        color: #0f172a;
        margin-right: auto;
        border-bottom-left-radius: 6px;
    }
    .chat-button {
        background: #1d4ed8;
        color: white;
        border: none;
        border-radius: 14px;
        padding: 10px 16px;
    }
    .chat-toggle {
        background: #1d4ed8;
        color: white;
        border: none;
        border-radius: 14px;
        padding: 10px 12px;
        width: 100%;
        margin-bottom: 16px;
        cursor: pointer;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

st.title("AI-Augmented AUTOSAR MDE Website")
st.write("Generate simplified AUTOSAR YAML from natural language, upload ARXML references, and compile to ARXML outputs.")

engine = AutosarHackathonEngine(load_pdfs=False)

if "user_request" not in st.session_state:
    st.session_state.user_request = (
        "Create a Software Component named TempSensor with a sender port CurrentTemp of type float32."
    )
if "yaml_data" not in st.session_state:
    st.session_state.yaml_data = ""
if "arxml_output" not in st.session_state:
    st.session_state.arxml_output = ""
if "vector_docs" not in st.session_state:
    st.session_state.vector_docs = []
if "keyword_docs" not in st.session_state:
    st.session_state.keyword_docs = []
if "uploaded_docs" not in st.session_state:
    st.session_state.uploaded_docs = []
if "chat_messages" not in st.session_state:
    st.session_state.chat_messages = []

with st.sidebar:
    st.header("Controls")
    st.markdown("Upload AUTOSAR ARXML reference files and run local Ollama generation.")
    model_name = st.text_input("Ollama model", engine.model_name)
    if st.button("Reload model"):
        engine = AutosarHackathonEngine(model_name=model_name, load_pdfs=False)
    if st.button("Load TempSensor demo prompt"):
        st.session_state.user_request = "Create a Software Component named TempSensor with a sender port CurrentTemp of type float32."
    if st.button("Load CAN→SOME/IP demo prompt"):
        st.session_state.user_request = engine.get_example_prompt("can_to_someip")
    if st.button("Compare retrieval modes"):
        st.session_state.vector_docs = engine.retrieve_documents(st.session_state.user_request, use_vector=True)
        st.session_state.keyword_docs = engine.retrieve_documents(st.session_state.user_request, use_vector=False)

st.sidebar.markdown("---")
st.sidebar.header("Upload Reference ARXML")
uploaded_files = st.sidebar.file_uploader("Select AUTOSAR ARXML files", type=["arxml"], accept_multiple_files=True)
if uploaded_files:
    uploaded_texts = []
    for file in uploaded_files:
        content = file.read().decode("utf-8", errors="replace")
        parsed_text = _parse_autosar_arxml_text(content, source_name=file.name)
        uploaded_texts.append({"name": file.name, "text": parsed_text})
    st.session_state.uploaded_docs = uploaded_texts
    st.sidebar.success(f"Loaded {len(uploaded_texts)} ARXML file(s) for reference.")

with st.expander("Loaded AUTOSAR reference files"):
    if st.session_state.uploaded_docs:
        for doc in st.session_state.uploaded_docs:
            st.markdown(f"**{doc['name']}**")
            st.write(doc["text"][:400] + ("..." if len(doc["text"]) > 400 else ""))
    else:
        st.info("No uploaded AUTOSAR references yet.")

col1, col2 = st.columns([2, 1])
with col1:
    st.subheader("1. Natural language request")
    user_request = st.text_area("Describe the AUTOSAR component or gateway behavior", value=st.session_state.user_request, height=180)
    st.session_state.user_request = user_request

    st.subheader("2. Generate simplified AUTOSAR YAML")
    use_vector = st.checkbox("Use Chroma vector retrieval", value=True)
    show_raw_prompt = st.checkbox("Show raw Ollama prompt", value=False)
    if st.button("Generate YAML"):
        with st.spinner("Running local Ollama inference..."):
            reference_docs = st.session_state.uploaded_docs if st.session_state.uploaded_docs else None
            if reference_docs:
                extra_texts = [doc["text"] for doc in reference_docs]
                engine.docs.extend([{"name": doc["name"], "text": doc["text"]} for doc in reference_docs])
            yaml_data = engine.generate_simplified_structure(user_request, use_vector_retrieval=use_vector)
            st.session_state.yaml_data = yaml_data
            st.session_state.last_prompt = engine.get_last_prompt()

    if st.session_state.yaml_data:
        st.subheader("Generated YAML")
        st.code(st.session_state.yaml_data, language="yaml")
        st.download_button("Download YAML", st.session_state.yaml_data, file_name="autosar_architecture.yaml", mime="text/yaml")
        if show_raw_prompt and st.session_state.last_prompt:
            st.subheader("Raw Ollama prompt")
            st.text_area("Prompt", value=st.session_state.last_prompt, height=260)
    else:
        st.info("Generate YAML to see the output here.")

with col2:
    st.subheader("Reference retrieval")
    if st.button("Compare retrieval modes"):
        st.session_state.vector_docs = engine.retrieve_documents(st.session_state.user_request, use_vector=True)
        st.session_state.keyword_docs = engine.retrieve_documents(st.session_state.user_request, use_vector=False)

    st.markdown("**Vector retrieval**")
    for doc in st.session_state.vector_docs:
        st.write(doc[:300] + ("..." if len(doc) > 300 else ""))
    st.markdown("**Keyword retrieval**")
    for doc in st.session_state.keyword_docs:
        st.write(doc[:300] + ("..." if len(doc) > 300 else ""))

st.subheader("3. Compile to ARXML")
if st.button("Compile ARXML"):
    if not st.session_state.yaml_data:
        st.warning("Generate YAML first.")
    else:
        with st.spinner("Compiling YAML to ARXML..."):
            st.session_state.arxml_output = engine.compile_to_arxml(st.session_state.yaml_data)

if st.session_state.arxml_output:
    st.code(st.session_state.arxml_output, language="xml")
    st.download_button("Download ARXML", st.session_state.arxml_output, file_name="autosar_output.arxml", mime="application/xml")

st.subheader("4. Safety validation")
if st.button("Run safety check"):
    if not st.session_state.yaml_data:
        st.warning("Generate YAML first.")
    else:
        issues = engine.safety_check(st.session_state.yaml_data)
        if issues:
            st.error("Safety issues found")
            for issue in issues:
                st.write(f"- {issue}")
        else:
            st.success("No high-level safety issues detected.")

st.markdown("---")
st.markdown("#### Notes\n- Upload ARXML files on the sidebar to use them as reference material.\n- Generated ARXML is a simplified demonstration output.\n- The app uses local Ollama via the `ollama` CLI.")

st.markdown("---")
col_main, col_chat = st.columns([2, 1])

if "chat_visible" not in st.session_state:
    st.session_state.chat_visible = True

with col_main:
    st.subheader("Chatbot — Ask the model")
    st.write("Ask the AUTOSAR assistant about your generated YAML, AUTOSAR references, or design questions.")

with col_chat:
    if st.button("Toggle chat window"):
        st.session_state.chat_visible = not st.session_state.chat_visible

    if st.session_state.chat_visible:
        st.markdown(render_chat_html(st.session_state.chat_messages), unsafe_allow_html=True)

        with st.form("chat_form", clear_on_submit=True):
            user_msg = st.text_input("Type a message...", key="chat_input")
            submitted = st.form_submit_button("Send")
    else:
        st.info("Chat is closed. Click the button to open it.")
        submitted = False
        user_msg = ""

    if st.session_state.chat_visible and submitted and user_msg:
        st.session_state.chat_messages.append({"role": "user", "text": user_msg})

        context_parts = []
        if st.session_state.uploaded_docs:
            context_parts.append("Reference materials:")
            for doc in st.session_state.uploaded_docs:
                context_parts.append(f"--- {doc['name']} ---")
                context_parts.append(doc["text"])

        convo = []
        for m in st.session_state.chat_messages[-6:]:
            role = m.get("role")
            text = m.get("text")
            prefix = "User:" if role == "user" else "Assistant:"
            convo.append(f"{prefix} {text}")

        prompt = (
            "You are a helpful AUTOSAR and general assistant. "
            "Answer user questions directly and concisely. "
            "Use reference material only when it is clearly relevant. "
            "If the user asks a general knowledge question, answer from general knowledge.\n\n"
            "Respond with the assistant's answer only, without repeating the instructions.\n\n"
        )
        if context_parts:
            prompt += "Reference materials:\n"
            prompt += "\n\n".join(context_parts)
            prompt += "\n\n"
        prompt += "Conversation:\n"
        prompt += "\n".join(convo)
        prompt += "\nAssistant:"

        placeholder = st.empty()
        engine_response = ""
        try:
            proc = subprocess.Popen([
                "ollama",
                "run",
                engine.model_name,
                prompt,
            ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=False, bufsize=0)
            while True:
                chunk = proc.stdout.read(1024)
                if chunk:
                    try:
                        decoded = chunk.decode("utf-8")
                    except Exception:
                        decoded = chunk.decode("utf-8", errors="replace")
                    decoded = clean_model_text(decoded)
                    engine_response += decoded
                    placeholder.markdown(f"**Assistant:** {engine_response}")
                else:
                    if proc.poll() is not None:
                        rest = proc.stdout.read()
                        if rest:
                            try:
                                rest_decoded = rest.decode("utf-8")
                            except Exception:
                                rest_decoded = rest.decode("utf-8", errors="replace")
                            rest_decoded = clean_model_text(rest_decoded)
                            engine_response += rest_decoded
                            placeholder.markdown(f"**Assistant:** {engine_response}")
                        break
                    time.sleep(0.05)
            stderr = proc.stderr.read() or b""
            try:
                stderr_decoded = stderr.decode("utf-8")
            except Exception:
                stderr_decoded = stderr.decode("utf-8", errors="replace")
            if proc.returncode != 0 and stderr_decoded:
                stderr_decoded = clean_model_text(stderr_decoded)
                engine_response += f"\n(Error: {stderr_decoded.strip()})"
                placeholder.markdown(f"**Assistant:** {engine_response}")
        except Exception as e:
            engine_response = f"(Error calling model) {e}"
            placeholder.markdown(f"**Assistant:** {engine_response}")

        st.session_state.chat_messages.append({"role": "assistant", "text": engine_response})

    if st.session_state.chat_messages:
        st.download_button("Download chat JSON", json.dumps(st.session_state.chat_messages, indent=2), file_name="chat_history.json", mime="application/json")
    elif not st.session_state.chat_messages:
        st.info("No chat messages yet. Send a question to start the conversation.")
