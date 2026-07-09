






import threading
from pathlib import Path
from urllib.parse import quote

import streamlit as st

from core import AutosarHackathonEngine, _parse_autosar_arxml_text


st.set_page_config(page_title="AUTOSAR AI MDE", layout="wide")

BASE_DIR = Path(__file__).resolve().parent


def _warmup_vector_store(engine: AutosarHackathonEngine) -> None:
    engine.warm_up_vector_store()


def _svg_file_to_data_uri(file_path: Path) -> str:
    svg_text = file_path.read_text(encoding="utf-8")
    return f"data:image/svg+xml,{quote(svg_text, safe='')}"


slide_1_uri = _svg_file_to_data_uri(BASE_DIR / "static" / "autosar_slide_1.svg")
slide_2_uri = _svg_file_to_data_uri(BASE_DIR / "static" / "autosar_slide_2.svg")
slide_3_uri = _svg_file_to_data_uri(BASE_DIR / "static" / "autosar_slide_3.svg")
































































































hero_html = """
    <style>
    .autosar-hero {
        position: relative;
        overflow: hidden;
        border-radius: 20px;
        padding: 24px 24px 22px;
        margin-bottom: 20px;
        border: 1px solid #dbe7ff;
        background: linear-gradient(120deg, #f7fbff 0%, #f3f8ff 55%, #edf5ff 100%);
    }

    .autosar-hero h1 {
        position: relative;
        z-index: 2;
        margin: 0;
        font-size: clamp(1.5rem, 1.4rem + 1.2vw, 2.3rem);
        letter-spacing: 0.2px;
        color: #0f2942;
        font-weight: 750;
    }

    .autosar-hero p {
        position: relative;
        z-index: 2;
        margin: 10px 0 0;
        color: #274a68;
        font-size: 0.98rem;
    }

    .autosar-carousel-track {
        position: absolute;
        top: 0;
        left: -8%;
        width: 220%;
        height: 100%;
        display: flex;
        align-items: stretch;
        gap: 18px;
        opacity: 0.7;
        z-index: 1;
        animation: autosarSlide 26s linear infinite;
    }

    .autosar-slide {
        flex: 0 0 430px;
        height: 100%;
        border-radius: 18px;
        overflow: hidden;
        border: 1px solid rgba(138, 183, 234, 0.65);
        background: rgba(255, 255, 255, 0.45);
        box-shadow: 0 8px 26px rgba(27, 89, 155, 0.16);
    }

    .autosar-slide img {
        width: 100%;
        height: 100%;
        display: block;
        object-fit: cover;
    }

    @keyframes autosarSlide {
        0% { transform: translateX(0); }
        100% { transform: translateX(-38%); }
    }

    @media (prefers-reduced-motion: reduce) {
        .autosar-carousel-track {
            animation: none;
        }
    }
    </style>

    <section class="autosar-hero">
      <div class="autosar-carousel-track" aria-hidden="true">
                <div class="autosar-slide"><img src="__SLIDE1__" alt="Wireframe car with CAN network and gateway"></div>
                <div class="autosar-slide"><img src="__SLIDE2__" alt="SOME/IP and Ethernet automotive mesh"></div>
                <div class="autosar-slide"><img src="__SLIDE3__" alt="FuSa telltales and safety monitor network"></div>
                <div class="autosar-slide"><img src="__SLIDE1__" alt="Wireframe car with CAN network and gateway"></div>
                <div class="autosar-slide"><img src="__SLIDE2__" alt="SOME/IP and Ethernet automotive mesh"></div>
                <div class="autosar-slide"><img src="__SLIDE3__" alt="FuSa telltales and safety monitor network"></div>
      </div>
      <h1>AI-Augmented AUTOSAR MDE Website</h1>
      <p>Generate simplified AUTOSAR YAML from natural language, upload ARXML references, and compile to ARXML outputs.</p>
    </section>
    """

hero_html = (
    hero_html
    .replace("__SLIDE1__", slide_1_uri)
    .replace("__SLIDE2__", slide_2_uri)
    .replace("__SLIDE3__", slide_3_uri)
)

st.markdown(hero_html, unsafe_allow_html=True)

if "model_name" not in st.session_state:
    st.session_state.model_name = "llama3.1"
if (
    "engine" not in st.session_state
    or not hasattr(st.session_state.engine, "add_reference_documents")
):
    st.session_state.engine = AutosarHackathonEngine(
        model_name=st.session_state.model_name,
        load_pdfs=False,
    )
engine = st.session_state.engine

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


if "last_prompt" not in st.session_state:
    st.session_state.last_prompt = ""
if "reference_ingest_status" not in st.session_state:
    st.session_state.reference_ingest_status = ""
if "uploader_nonce" not in st.session_state:
    st.session_state.uploader_nonce = 0
if "auto_warmup" not in st.session_state:
    st.session_state.auto_warmup = True
if "vector_warmup_started" not in st.session_state:
    st.session_state.vector_warmup_started = False
if "vector_warmup_done" not in st.session_state:
    st.session_state.vector_warmup_done = False
if "vector_warmup_error" not in st.session_state:
    st.session_state.vector_warmup_error = ""
if "vector_warmup_thread" not in st.session_state:
    st.session_state.vector_warmup_thread = None

if engine.is_vector_store_ready():
    st.session_state.vector_warmup_done = True
    st.session_state.vector_warmup_started = False

if (
    st.session_state.auto_warmup
    and not st.session_state.vector_warmup_done
    and not st.session_state.vector_warmup_started
):
    warmup_thread = threading.Thread(target=_warmup_vector_store, args=(engine,), daemon=True)
    st.session_state.vector_warmup_thread = warmup_thread
    st.session_state.vector_warmup_started = True
    warmup_thread.start()


with st.sidebar:
    st.header("Controls")
    st.markdown("Upload AUTOSAR ARXML reference files and run local Ollama generation.")
    model_name = st.text_input("Ollama model", st.session_state.model_name)
    if st.button("Reload model"):
        st.session_state.model_name = model_name.strip() or "llama3.1"
        st.session_state.engine = AutosarHackathonEngine(
            model_name=st.session_state.model_name,
            load_pdfs=False,
        )
        engine = st.session_state.engine
        st.session_state.vector_warmup_started = False
        st.session_state.vector_warmup_done = False
        st.session_state.vector_warmup_error = ""
        st.session_state.vector_warmup_thread = None
        st.success(f"Reloaded model: {st.session_state.model_name}")
    st.session_state.auto_warmup = st.checkbox(
        "Warm retrieval model in background",
        value=st.session_state.auto_warmup,
    )
    if st.button("Warm up retrieval model now"):
        with st.spinner("Warming retrieval model..."):
            ok = engine.warm_up_vector_store()
            st.session_state.vector_warmup_done = ok
            st.session_state.vector_warmup_started = False
            st.session_state.vector_warmup_error = engine.vector_warmup_error or ""
            if ok:
                st.success("Retrieval model is ready.")
            else:
                st.error(f"Warm-up failed: {st.session_state.vector_warmup_error}")

    if engine.is_vector_store_ready() or st.session_state.vector_warmup_done:
        st.caption("Retrieval model status: ready")
    elif st.session_state.vector_warmup_started:
        warmup_thread = st.session_state.vector_warmup_thread
        if warmup_thread is not None and not warmup_thread.is_alive():
            if engine.is_vector_store_ready():
                st.session_state.vector_warmup_done = True
                st.session_state.vector_warmup_started = False
                st.caption("Retrieval model status: ready")
            else:
                st.session_state.vector_warmup_started = False
                st.session_state.vector_warmup_error = engine.vector_warmup_error or "Unknown warm-up error"
                st.caption("Retrieval model status: failed")
        else:
            st.caption("Retrieval model status: warming up...")
    else:
        st.caption("Retrieval model status: not warmed yet")
    if st.button("Load TempSensor demo prompt"):
        st.session_state.user_request = "Create a Software Component named TempSensor with a sender port CurrentTemp of type float32."
    if st.button("Load CAN→SOME/IP demo prompt"):
        st.session_state.user_request = engine.get_example_prompt("can_to_someip")
    if st.button("Compare retrieval modes"):
        st.session_state.vector_docs = engine.retrieve_documents(st.session_state.user_request, use_vector=True)
        st.session_state.keyword_docs = engine.retrieve_documents(st.session_state.user_request, use_vector=False)

st.sidebar.markdown("---")
st.sidebar.header("Upload Reference ARXML")
uploaded_files = st.sidebar.file_uploader(
    "Select AUTOSAR ARXML files",
    type=["arxml"],
    accept_multiple_files=True,
    key=f"reference_uploader_{st.session_state.uploader_nonce}",
)
if uploaded_files:
    uploaded_texts = []
    for file in uploaded_files:
        content = file.read().decode("utf-8", errors="replace")
        parsed_text = _parse_autosar_arxml_text(content, source_name=file.name)
        uploaded_texts.append({"name": file.name, "text": parsed_text})
    st.session_state.uploaded_docs = uploaded_texts
    st.sidebar.success(f"Loaded {len(uploaded_texts)} ARXML file(s) for reference.")

if st.sidebar.button("Reset uploaded references"):
    st.session_state.uploaded_docs = []
    st.session_state.reference_ingest_status = ""
    st.session_state.vector_docs = []
    st.session_state.keyword_docs = []
    st.session_state.engine = AutosarHackathonEngine(
        model_name=st.session_state.model_name,
        load_pdfs=False,
    )
    st.session_state.vector_warmup_started = False
    st.session_state.vector_warmup_done = False
    st.session_state.vector_warmup_error = ""
    st.session_state.vector_warmup_thread = None
    st.session_state.uploader_nonce += 1
    st.rerun()

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
                ingest_result = engine.add_reference_documents(reference_docs)
                st.session_state.reference_ingest_status = (
                    "Reference ingest: "
                    f"added {ingest_result['added']}, "
                    f"already present {ingest_result['already_present']}."
                )
            else:
                st.session_state.reference_ingest_status = ""
            yaml_data = engine.generate_simplified_structure(user_request, use_vector_retrieval=use_vector)
            st.session_state.yaml_data = yaml_data
            st.session_state.last_prompt = engine.get_last_prompt()

    if st.session_state.reference_ingest_status:
        st.caption(st.session_state.reference_ingest_status)

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













































































































