






import threading
from pathlib import Path
from urllib.parse import quote
import json
import hashlib
import binascii
from datetime import datetime, timezone

import streamlit as st

from core import AutosarHackathonEngine, _parse_autosar_arxml_text


st.set_page_config(page_title="AUTOSAR AI MDE", layout="wide")

BASE_DIR = Path(__file__).resolve().parent
AUTH_FILE = BASE_DIR / "config" / "auth_users.json"
AUDIT_DIR = BASE_DIR / "audit"


def _load_auth_users():
    if not AUTH_FILE.exists():
        return {}
    data = json.loads(AUTH_FILE.read_text(encoding="utf-8"))
    users = {}
    for u in data.get("users", []):
        users[u.get("username", "")] = u
    return users


def _verify_password(password: str, user_record: dict) -> bool:
    try:
        salt = bytes.fromhex(user_record["salt"])
        iterations = int(user_record.get("iterations", 120000))
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
        return binascii.hexlify(digest).decode("utf-8") == user_record["password_hash"]
    except Exception:
        return False


def _audit_log(tenant: str, username: str, role: str, action: str, details: dict | None = None):
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "tenant": tenant,
        "username": username,
        "role": role,
        "action": action,
        "details": details or {},
    }
    logfile = AUDIT_DIR / f"{tenant}_audit.jsonl"
    with logfile.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")


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

auth_users = _load_auth_users()
if "auth" not in st.session_state:
    st.session_state.auth = {
        "is_authenticated": False,
        "username": "",
        "display_name": "",
        "tenant": "",
        "role": "",
    }

if not st.session_state.auth.get("is_authenticated"):
    st.subheader("Company Login")
    with st.form("login_form", clear_on_submit=False):
        username = st.text_input("Username")
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Sign in")
    if submitted:
        rec = auth_users.get(username)
        if rec and _verify_password(password, rec):
            st.session_state.auth = {
                "is_authenticated": True,
                "username": username,
                "display_name": rec.get("display_name", username),
                "tenant": rec.get("tenant", "public"),
                "role": rec.get("role", "viewer"),
            }
            _audit_log(
                rec.get("tenant", "public"),
                username,
                rec.get("role", "viewer"),
                "login_success",
                {},
            )
            st.rerun()
        else:
            st.error("Invalid credentials")
            _audit_log("unknown", username or "unknown", "unknown", "login_failed", {})
    st.stop()

auth_ctx = st.session_state.auth
st.caption(
    f"Signed in as {auth_ctx['display_name']} ({auth_ctx['role']}) | tenant: {auth_ctx['tenant']}"
)

if "model_name" not in st.session_state:
    st.session_state.model_name = "llama3.1"
if (
    "engine" not in st.session_state
    or not hasattr(st.session_state.engine, "add_reference_documents")
):
    st.session_state.engine = AutosarHackathonEngine(
        model_name=st.session_state.model_name,
        load_pdfs=False,
        tenant_id=auth_ctx["tenant"],
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
            tenant_id=auth_ctx["tenant"],
        )
        _audit_log(auth_ctx["tenant"], auth_ctx["username"], auth_ctx["role"], "reload_model", {"model": st.session_state.model_name})
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
        tenant_id=auth_ctx["tenant"],
    )
    _audit_log(auth_ctx["tenant"], auth_ctx["username"], auth_ctx["role"], "reset_uploaded_references", {})
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
    allow_fallback = st.checkbox("Allow fallback if Ollama is unavailable", value=True)
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
            try:
                yaml_data = engine.generate_simplified_structure(user_request, use_vector_retrieval=use_vector)
                st.session_state.yaml_data = yaml_data
                st.session_state.last_prompt = engine.get_last_prompt()
                _audit_log(auth_ctx["tenant"], auth_ctx["username"], auth_ctx["role"], "generate_yaml", {"use_vector": use_vector, "prompt_len": len(user_request or ""), "mode": "ollama"})
            except Exception as exc:
                if allow_fallback:
                    st.warning(f"Ollama generation failed ({exc}). Using deterministic fallback YAML.")
                    st.session_state.yaml_data = engine.generate_simplified_structure_fallback(user_request)
                    st.session_state.last_prompt = ""
                    _audit_log(auth_ctx["tenant"], auth_ctx["username"], auth_ctx["role"], "generate_yaml_fallback", {"error": str(exc), "prompt_len": len(user_request or "")})
                else:
                    st.error(f"YAML generation failed: {exc}")
                    _audit_log(auth_ctx["tenant"], auth_ctx["username"], auth_ctx["role"], "generate_yaml_failed", {"error": str(exc), "prompt_len": len(user_request or "")})

    if st.session_state.reference_ingest_status:
        st.caption(st.session_state.reference_ingest_status)

    if st.session_state.yaml_data:
        st.subheader("Generated YAML")
        st.code(st.session_state.yaml_data, language="yaml")
        baseline_profiles = engine.get_demo_baseline_profiles()
        default_profile = auth_ctx["tenant"] if auth_ctx["tenant"] in baseline_profiles else "default"
        selected_profile = st.selectbox(
            "Dummy baseline profile",
            baseline_profiles,
            index=baseline_profiles.index(default_profile) if default_profile in baseline_profiles else 0,
            help="Choose a profile to auto-fill missing service and signal sections.",
        )
        if st.button("Apply demo baseline data (CAN/SOME-IP + ECU defaults)"):
            try:
                st.session_state.yaml_data = engine.apply_demo_baseline_yaml(
                    st.session_state.yaml_data,
                    profile=selected_profile,
                )
                st.success(
                    f"Demo baseline profile '{selected_profile}' applied. "
                    "Missing CAN/SOME-IP and ECU defaults were auto-filled."
                )
                _audit_log(
                    auth_ctx["tenant"],
                    auth_ctx["username"],
                    auth_ctx["role"],
                    "apply_demo_baseline",
                    {"profile": selected_profile},
                )
                st.rerun()
            except Exception as exc:
                st.error(f"Could not apply demo baseline data: {exc}")
        st.caption("Tip: Use demo baseline data to auto-fill missing SOME/IP, CAN, ASIL, and hardware bindings for jury demos.")
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
            try:
                st.session_state.arxml_output = engine.compile_to_arxml(st.session_state.yaml_data)
                _audit_log(auth_ctx["tenant"], auth_ctx["username"], auth_ctx["role"], "compile_arxml", {"yaml_len": len(st.session_state.yaml_data or "")})
            except ValueError as exc:
                st.session_state.arxml_output = ""
                st.error(f"ARXML compile failed: {exc}")
                st.info("Tip: Regenerate YAML or adjust the prompt to request strict valid YAML only.")
                _audit_log(auth_ctx["tenant"], auth_ctx["username"], auth_ctx["role"], "compile_arxml_failed", {"error": str(exc)})
            except Exception as exc:
                st.session_state.arxml_output = ""
                st.error(f"ARXML compile failed unexpectedly: {exc}")
                _audit_log(auth_ctx["tenant"], auth_ctx["username"], auth_ctx["role"], "compile_arxml_failed", {"error": str(exc)})

if st.session_state.arxml_output:
    st.code(st.session_state.arxml_output, language="xml")
    st.download_button("Download ARXML", st.session_state.arxml_output, file_name="autosar_output.arxml", mime="application/xml")

st.subheader("4. Safety validation")
if st.button("Run safety check"):
    if not st.session_state.yaml_data:
        st.warning("Generate YAML first.")
    else:
        report = engine.safety_check_report(st.session_state.yaml_data)
        counts = report.get("counts", {})
        critical_count = int(counts.get("critical", 0))
        warning_count = int(counts.get("warning", 0))
        info_count = int(counts.get("info", 0))
        score = int(report.get("score", 0))

        _audit_log(
            auth_ctx["tenant"],
            auth_ctx["username"],
            auth_ctx["role"],
            "run_safety_check",
            {
                "critical": critical_count,
                "warning": warning_count,
                "info": info_count,
                "score": score,
            },
        )

        st.metric("Safety readiness score", f"{score}/100")
        st.caption(str(report.get("summary", "Validation completed")))

        findings = report.get("findings", [])
        critical_items = [f for f in findings if f.get("severity") == "critical"]
        warning_items = [f for f in findings if f.get("severity") == "warning"]
        info_items = [f for f in findings if f.get("severity") == "info"]

        if critical_items:
            st.error("Critical findings")
            for item in critical_items:
                st.write(f"- {item.get('message', '')}")

        if warning_items:
            st.warning("Warnings")
            for item in warning_items:
                st.write(f"- {item.get('message', '')}")
            st.info("Each warning line includes what is missing and what to add next.")

        if info_items:
            st.success("Checks passed")
            for item in info_items:
                st.write(f"- {item.get('message', '')}")

st.markdown("---")
st.markdown("#### Notes\n- Upload ARXML files on the sidebar to use them as reference material.\n- Generated ARXML is a simplified demonstration output.\n- The app uses local Ollama via the `ollama` CLI.")













































































































