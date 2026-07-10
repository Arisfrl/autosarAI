






import threading
import subprocess
import importlib
import urllib.request
import urllib.parse
import base64
import hmac
import os
import html
from pathlib import Path
from urllib.parse import quote
import json
import hashlib
import binascii
import inspect
from datetime import datetime, timezone

import streamlit as st
import streamlit.components.v1 as components

import core as core_module
from core import AutosarHackathonEngine, _parse_autosar_arxml_text


st.set_page_config(page_title="AUTOSAR AI MDE", layout="wide")

BASE_DIR = Path(__file__).resolve().parent
AUTH_FILE = BASE_DIR / "config" / "auth_users.json"
AUDIT_DIR = BASE_DIR / "audit"
AUTH_SESSION_TTL_SECONDS = 8 * 60 * 60


def _auth_secret() -> str:
    env_secret = (os.getenv("AUTOSAR_AUTH_SECRET") or "").strip()
    if env_secret:
        return env_secret
    # Local-development fallback; set AUTOSAR_AUTH_SECRET in production.
    return "autosar_ai_mde_auth_secret_v1"


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


def _create_auth_token(auth_payload: dict) -> str:
    payload_json = json.dumps(auth_payload, separators=(",", ":"), ensure_ascii=False)
    payload_b64 = base64.urlsafe_b64encode(payload_json.encode("utf-8")).decode("utf-8").rstrip("=")
    signature = hmac.new(
        _auth_secret().encode("utf-8"),
        payload_b64.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{payload_b64}.{signature}"


def _verify_auth_token(token: str) -> dict | None:
    token = (token or "").strip()
    if not token or "." not in token:
        return None
    payload_b64, received_sig = token.rsplit(".", 1)
    expected_sig = hmac.new(
        _auth_secret().encode("utf-8"),
        payload_b64.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected_sig, received_sig):
        return None
    try:
        padded = payload_b64 + ("=" * (-len(payload_b64) % 4))
        payload_json = base64.urlsafe_b64decode(padded.encode("utf-8")).decode("utf-8")
        payload = json.loads(payload_json)
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    expires_at = int(payload.get("exp", 0) or 0)
    now_ts = int(datetime.now(timezone.utc).timestamp())
    if expires_at <= now_ts:
        return None
    return payload


def _clear_auth_session() -> None:
    st.session_state.auth = {
        "is_authenticated": False,
        "username": "",
        "display_name": "",
        "tenant": "",
        "role": "",
    }
    for state_key in [
        "engine",
        "yaml_data",
        "arxml_output",
        "arxml_output_adaptive",
        "arxml_output_classic",
        "vector_docs",
        "keyword_docs",
        "uploaded_docs",
        "last_prompt",
        "reference_ingest_status",
    ]:
        if state_key in st.session_state:
            del st.session_state[state_key]
    if "auth" in st.query_params:
        del st.query_params["auth"]
    if "action" in st.query_params:
        del st.query_params["action"]


def _get_query_param_str(name: str) -> str:
    value = st.query_params.get(name, "")
    if isinstance(value, list):
        return str(value[0]).strip() if value else ""
    return str(value).strip()


def _classify_arxml_platform(xml_text: str) -> dict:
    classifier = getattr(core_module, "classify_arxml_platform", None)
    if callable(classifier):
        return classifier(xml_text)

    # Fallback in case hot-reload exposes a stale core module object.
    payload = (xml_text or "").strip()
    if not payload:
        return {
            "classification": "invalid",
            "classic_score": 0,
            "adaptive_score": 0,
            "reason": "empty content",
        }
    if "ara::" in payload.lower() or "adaptive" in payload.lower():
        return {
            "classification": "adaptive",
            "classic_score": 0,
            "adaptive_score": 1,
            "reason": "adaptive marker detected",
        }
    return {
        "classification": "classic",
        "classic_score": 1,
        "adaptive_score": 0,
        "reason": "fallback classic classification",
    }


def _warmup_vector_store(engine: AutosarHackathonEngine) -> None:
    engine.warm_up_vector_store()


def _create_engine(model_name: str, tenant_id: str, model_provider: str, api_key: str) -> AutosarHackathonEngine:
    engine_class = AutosarHackathonEngine
    try:
        reloaded = importlib.reload(core_module)
        engine_class = getattr(reloaded, "AutosarHackathonEngine", AutosarHackathonEngine)
    except Exception:
        engine_class = AutosarHackathonEngine

    kwargs = {
        "model_name": model_name,
        "load_pdfs": False,
        "tenant_id": tenant_id,
    }
    try:
        params = inspect.signature(engine_class).parameters
        if "model_provider" in params:
            kwargs["model_provider"] = model_provider
        if "api_key" in params:
            kwargs["api_key"] = api_key
    except (TypeError, ValueError):
        pass
    return engine_class(**kwargs)


def _list_ollama_models() -> list[str]:
    try:
        result = subprocess.run(
            ["ollama", "list"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=12,
        )
    except Exception:
        return []

    if result.returncode != 0:
        return []

    lines = [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]
    if len(lines) <= 1:
        return []

    model_names: list[str] = []
    for line in lines[1:]:
        parts = line.split()
        if not parts:
            continue
        name = parts[0].strip()
        if name and name.lower() != "name":
            model_names.append(name)
    return model_names


def _list_gemini_models(api_key: str) -> list[str]:
    token = (api_key or "").strip()
    if not token:
        return []

    endpoint = (
        "https://generativelanguage.googleapis.com/v1beta/models"
        f"?key={urllib.parse.quote(token, safe='')}"
    )
    request = urllib.request.Request(endpoint, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = response.read().decode("utf-8", errors="replace")
        payload = json.loads(body)
    except Exception:
        return []

    model_names: list[str] = []
    for item in payload.get("models", []):
        if not isinstance(item, dict):
            continue
        methods = item.get("supportedGenerationMethods", []) or []
        if "generateContent" not in methods:
            continue
        name = str(item.get("name", "")).strip()
        if name.startswith("models/"):
            name = name.split("/", 1)[1]
        if name:
            model_names.append(name)

    # Preserve order while removing duplicates.
    seen = set()
    ordered = []
    for name in model_names:
        if name in seen:
            continue
        seen.add(name)
        ordered.append(name)
    return ordered


def _svg_file_to_data_uri(file_path: Path) -> str:
    svg_text = file_path.read_text(encoding="utf-8")
    return f"data:image/svg+xml,{quote(svg_text, safe='')}"


def _avatar_initials(display_name: str) -> str:
    parts = [p for p in (display_name or "").strip().split() if p]
    if not parts:
        return "AU"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return f"{parts[0][0]}{parts[-1][0]}".upper()


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

auth_users = _load_auth_users()
if "auth" not in st.session_state:
    st.session_state.auth = {
        "is_authenticated": False,
        "username": "",
        "display_name": "",
        "tenant": "",
        "role": "",
    }

requested_action = _get_query_param_str("action").lower()
if requested_action == "signout":
    prior_auth = st.session_state.get("auth", {})
    if prior_auth.get("is_authenticated"):
        _audit_log(
            prior_auth.get("tenant", "unknown"),
            prior_auth.get("username", "unknown"),
            prior_auth.get("role", "unknown"),
            "logout",
            {"source": "main_menu"},
        )
    _clear_auth_session()
    st.rerun()

if not st.session_state.auth.get("is_authenticated"):
    auth_token = _get_query_param_str("auth")
    restored = _verify_auth_token(auth_token) if auth_token else None
    if restored:
        restored_username = str(restored.get("username", "")).strip()
        rec = auth_users.get(restored_username)
        if rec:
            st.session_state.auth = {
                "is_authenticated": True,
                "username": restored_username,
                "display_name": rec.get("display_name", restored_username),
                "tenant": rec.get("tenant", "public"),
                "role": rec.get("role", "viewer"),
            }
            _audit_log(
                rec.get("tenant", "public"),
                restored_username,
                rec.get("role", "viewer"),
                "session_restored",
                {},
            )
        else:
            if "auth" in st.query_params:
                del st.query_params["auth"]
    elif auth_token:
        if "auth" in st.query_params:
            del st.query_params["auth"]

if not st.session_state.auth.get("is_authenticated"):
    st.markdown(hero_html, unsafe_allow_html=True)
    st.subheader("Company Login")
    st.markdown(
        """
        <style>
        .login-note {
            margin-top: 0.2rem;
            margin-bottom: 0.8rem;
            color: #294966;
            font-size: 0.95rem;
        }
        @media (max-width: 840px) {
            .autosar-hero {
                padding: 18px 16px 16px;
            }
        }
        </style>
        <div class="login-note">Use your company credentials to access the AUTOSAR AI MDE Console.</div>
        """,
        unsafe_allow_html=True,
    )
    _, login_col, _ = st.columns([1, 2, 1])
    with login_col:
        with st.form("login_form", clear_on_submit=False):
            username = st.text_input("Username", help="Enter your company username")
            password = st.text_input("Password", type="password", help="Enter your account password")
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
            expires_at = int(datetime.now(timezone.utc).timestamp()) + AUTH_SESSION_TTL_SECONDS
            st.query_params["auth"] = _create_auth_token(
                {
                    "username": username,
                    "tenant": rec.get("tenant", "public"),
                    "role": rec.get("role", "viewer"),
                    "exp": expires_at,
                }
            )
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
st.markdown(
    """
    <style>
    .topbar-wrap {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 14px;
        padding: 10px 0 6px;
    }
    .topbar-title {
        color: #0f2942;
        font-weight: 800;
        letter-spacing: 0.1px;
        font-size: clamp(1.05rem, 0.95rem + 0.8vw, 1.38rem);
    }
    .profile-chip {
        display: flex;
        align-items: center;
        gap: 10px;
        justify-content: flex-end;
        width: 100%;
        border: 1px solid #c9dbf8;
        background: #f5f9ff;
        border-radius: 999px;
        padding: 7px 12px;
        color: #0f2942;
    }
    .avatar-circle {
        width: 30px;
        height: 30px;
        border-radius: 999px;
        background: #1f5ea9;
        color: #ffffff;
        display: inline-flex;
        align-items: center;
        justify-content: center;
        font-size: 0.76rem;
        font-weight: 700;
    }
    .profile-text {
        font-size: 0.84rem;
        line-height: 1.2;
        text-align: right;
    }
    .tenant-badge {
        display: inline-block;
        margin-left: 6px;
        padding: 2px 8px;
        border-radius: 999px;
        border: 1px solid #9fc1ef;
        background: #e8f2ff;
        font-size: 0.73rem;
        font-weight: 700;
        letter-spacing: 0.3px;
        text-transform: uppercase;
        color: #143a60;
    }
    @media (max-width: 900px) {
        .topbar-wrap {
            flex-direction: column;
            align-items: stretch;
        }
        .profile-chip {
            justify-content: flex-start;
            border-radius: 12px;
        }
        .profile-text {
            text-align: left;
        }
    }
    </style>
    """,
    unsafe_allow_html=True,
)

profile_initials = _avatar_initials(auth_ctx.get("display_name", ""))
profile_line = (
    f"Signed in as {auth_ctx['display_name']} ({auth_ctx['role']})"
    f" | tenant: {auth_ctx['tenant']}"
)

left_col, right_col = st.columns([2.8, 2.2])
with left_col:
    st.markdown('<div class="topbar-wrap"><div class="topbar-title">AUTOSAR AI MDE Console</div></div>', unsafe_allow_html=True)
with right_col:
    st.markdown(
        f"""
        <div class="topbar-wrap">
            <div class="profile-chip" aria-label="Signed-in profile information">
                <span class="avatar-circle" aria-hidden="true">{profile_initials}</span>
                <span class="profile-text">{profile_line}<span class="tenant-badge">{auth_ctx['tenant']}</span></span>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

components.html(
        """
        <script>
        (function () {
            const parentDoc = window.parent.document;
            const MENU_SELECTOR = 'div[data-testid="stMainMenuList"][role="menu"][aria-label="Main menu"]';
            const ITEM_ID = 'stMainMenuItem-signOutCustom';
            const DIVIDER_ID = 'stMainMenuDivider-signOutCustom';

            function signOutUrl() {
                const url = new URL(window.parent.location.href);
                url.searchParams.set('action', 'signout');
                return url.toString();
            }

            function ensureSignOutItem() {
                const menu = parentDoc.querySelector(MENU_SELECTOR);
                if (!menu || menu.querySelector('#' + ITEM_ID)) {
                    return;
                }

                const templateItem = menu.querySelector('button[role="menuitem"][data-testid^="stMainMenuItem-"]');
                if (!templateItem) {
                    return;
                }

                const templateDivider = menu.querySelector('div[data-testid="stMainMenuDivider"]');
                if (templateDivider && !menu.querySelector('#' + DIVIDER_ID)) {
                    const divider = templateDivider.cloneNode(true);
                    divider.id = DIVIDER_ID;
                    menu.appendChild(divider);
                }

                const item = parentDoc.createElement('a');
                item.id = ITEM_ID;
                item.setAttribute('data-testid', 'stMainMenuItem-signOut');
                item.setAttribute('role', 'menuitem');
                item.setAttribute('aria-label', 'Sign out');
                item.setAttribute('href', signOutUrl());
                item.tabIndex = -1;
                item.className = templateItem.className;
                item.innerHTML = templateItem.innerHTML;

                const accelerator = item.querySelector('kbd');
                if (accelerator) {
                    accelerator.remove();
                }

                const label = item.querySelector('[data-testid="stMainMenuItemLabel"]');
                if (label) {
                    label.textContent = 'Sign out';
                } else {
                    item.textContent = 'Sign out';
                }

                menu.appendChild(item);
            }

            ensureSignOutItem();

            const observer = new MutationObserver(function () {
                ensureSignOutItem();
            });
            observer.observe(parentDoc.body, { childList: true, subtree: true });
        })();
        </script>
        """,
        height=0,
)

st.markdown(hero_html, unsafe_allow_html=True)

if "model_name" not in st.session_state:
    st.session_state.model_name = "llama3.1:latest"
if "model_provider" not in st.session_state:
    st.session_state.model_provider = "ollama"
if "gemini_api_key" not in st.session_state:
    st.session_state.gemini_api_key = ""
if (
    "engine" not in st.session_state
    or not hasattr(st.session_state.engine, "add_reference_documents")
):
    st.session_state.engine = _create_engine(
        model_name=st.session_state.model_name,
        model_provider=st.session_state.model_provider,
        api_key=st.session_state.gemini_api_key,
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
if "arxml_output_adaptive" not in st.session_state:
    st.session_state.arxml_output_adaptive = ""
if "arxml_output_classic" not in st.session_state:
    st.session_state.arxml_output_classic = ""
if "vector_docs" not in st.session_state:
    st.session_state.vector_docs = []
if "keyword_docs" not in st.session_state:
    st.session_state.keyword_docs = []
if "uploaded_docs" not in st.session_state:
    st.session_state.uploaded_docs = []
if "upload_validation_results" not in st.session_state:
    st.session_state.upload_validation_results = []
if "upload_validation_summary" not in st.session_state:
    st.session_state.upload_validation_summary = {
        "total": 0,
        "accepted": 0,
        "rejected": 0,
        "unknown": 0,
    }


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
if "use_mapping_precheck" not in st.session_state:
    st.session_state.use_mapping_precheck = True
if "ai_suggestions" not in st.session_state:
    st.session_state.ai_suggestions = []
if "selected_suggestion_ids" not in st.session_state:
    st.session_state.selected_suggestion_ids = []
if "last_safety_report" not in st.session_state:
    st.session_state.last_safety_report = None
if "improvement_summary" not in st.session_state:
    st.session_state.improvement_summary = None
if "session_flow_summary" not in st.session_state:
    st.session_state.session_flow_summary = ""

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
    st.markdown("Upload AUTOSAR ARXML reference files and run model generation.")
    provider_choice = st.selectbox(
        "Model provider",
        options=["ollama", "gemini"],
        index=0 if st.session_state.model_provider == "ollama" else 1,
        format_func=lambda value: value.capitalize(),
    )
    st.session_state.model_provider = provider_choice

    if provider_choice == "gemini":
        st.session_state.gemini_api_key = st.text_input(
            "Gemini API key",
            value=st.session_state.gemini_api_key,
            type="password",
            help="Stored only in this Streamlit session unless you set GEMINI_API_KEY.",
        )

        recommended = [
            "gemini-2.5-flash",
            "gemini-2.5-pro",
            "gemini-2.0-flash",
            "gemini-2.0-flash-lite",
        ]
        available_models = _list_gemini_models(st.session_state.gemini_api_key)

        # Keep recommended models first when available, then append remaining discovered models.
        ordered_models = [name for name in recommended if name in available_models]
        ordered_models.extend([name for name in available_models if name not in ordered_models])
        if not ordered_models:
            ordered_models = recommended

        gemini_choices = ordered_models + ["Custom..."]
        initial_model = (st.session_state.model_name or ordered_models[0]).strip()
        if available_models and initial_model not in ordered_models:
            initial_model = ordered_models[0]
        default_choice = initial_model if initial_model in ordered_models else "Custom..."
        selected_gemini_model = st.selectbox(
            "Gemini model",
            options=gemini_choices,
            index=gemini_choices.index(default_choice),
            help="Auto-filtered to models supporting generateContent when API key is provided.",
        )
        if selected_gemini_model == "Custom...":
            model_name = st.text_input(
                "Custom Gemini model",
                value=initial_model,
            )
        else:
            model_name = selected_gemini_model

        if st.button("Refresh Gemini models"):
            st.rerun()
    else:
        ollama_models = _list_ollama_models()
        current_model = (st.session_state.model_name or "llama3.1:latest").strip()
        ollama_choices = ollama_models + ["Custom..."] if ollama_models else ["Custom..."]
        default_choice = current_model if current_model in ollama_models else "Custom..."
        selected_ollama_model = st.selectbox(
            "Ollama model",
            options=ollama_choices,
            index=ollama_choices.index(default_choice),
            help="Auto-detected from local ollama list.",
        )
        if selected_ollama_model == "Custom...":
            model_name = st.text_input(
                "Custom Ollama model",
                value=current_model,
                help="Enter a model tag exactly as shown by ollama list.",
            )
        else:
            model_name = selected_ollama_model
        if st.button("Refresh Ollama models"):
            st.rerun()

    if st.button("Reload model settings"):
        fallback_model = (
            "gemini-2.5-flash"
            if st.session_state.model_provider == "gemini"
            else "llama3.1:latest"
        )
        st.session_state.model_name = model_name.strip() or fallback_model
        st.session_state.engine = _create_engine(
            model_name=st.session_state.model_name,
            model_provider=st.session_state.model_provider,
            api_key=st.session_state.gemini_api_key,
            tenant_id=auth_ctx["tenant"],
        )
        _audit_log(
            auth_ctx["tenant"],
            auth_ctx["username"],
            auth_ctx["role"],
            "reload_model",
            {
                "model": st.session_state.model_name,
                "provider": st.session_state.model_provider,
            },
        )
        engine = st.session_state.engine
        st.session_state.vector_warmup_started = False
        st.session_state.vector_warmup_done = False
        st.session_state.vector_warmup_error = ""
        st.session_state.vector_warmup_thread = None
        st.success(
            f"Reloaded {st.session_state.model_provider} model: {st.session_state.model_name}"
        )
    st.session_state.auto_warmup = st.checkbox(
        "Warm retrieval model in background",
        value=st.session_state.auto_warmup,
    )
    st.session_state.use_mapping_precheck = st.checkbox(
        "Enable ML mapping pre-check",
        value=st.session_state.use_mapping_precheck,
        help="When enabled, compile/safety includes baseline ML checks for signal-to-service mappings.",
    )
    if st.button("Retrain ML mapping model"):
        with st.spinner("Retraining mapping baseline model..."):
            try:
                retrain_result = engine.retrain_mapping_baseline()
                metrics = retrain_result.get("metrics")
                st.success("ML mapping model retrained and reloaded.")
                if metrics:
                    st.caption("Latest baseline metrics")
                    st.json(metrics)
                _audit_log(
                    auth_ctx["tenant"],
                    auth_ctx["username"],
                    auth_ctx["role"],
                    "retrain_mapping_model",
                    {
                        "model_path": retrain_result.get("model_path", ""),
                        "metrics_path": retrain_result.get("metrics_path", ""),
                    },
                )
            except Exception as exc:
                st.error(f"Retrain failed: {exc}")
                _audit_log(
                    auth_ctx["tenant"],
                    auth_ctx["username"],
                    auth_ctx["role"],
                    "retrain_mapping_model_failed",
                    {"error": str(exc)},
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
    if st.button("Load AEB demo prompt"):
        st.session_state.user_request = (
            "Generate an AUTOSAR design for emergency braking (AEB) with sensor plausibility checks, "
            "safe-state transitions, and end-to-end safety mechanisms."
        )
    if st.button("Load CAN→SOME/IP demo prompt"):
        st.session_state.user_request = engine.get_example_prompt("can_to_someip")
    if st.button("Compare retrieval modes"):
        st.session_state.vector_docs = engine.retrieve_documents(st.session_state.user_request, use_vector=True)
        st.session_state.keyword_docs = engine.retrieve_documents(st.session_state.user_request, use_vector=False)

    st.markdown("---")
    st.subheader("Quick Commands")
    if st.button("Quick A: Validate uploads"):
        summary = st.session_state.upload_validation_summary
        st.info(
            "Validation snapshot -> "
            f"accepted={summary.get('accepted', 0)}, "
            f"rejected={summary.get('rejected', 0)}, "
            f"unknown={summary.get('unknown', 0)}, "
            f"total={summary.get('total', 0)}"
        )

    if st.button("Quick B: Regenerate normalized ARXML"):
        if not st.session_state.yaml_data:
            st.warning("Generate YAML first.")
        else:
            try:
                outputs = engine.compile_to_arxml_pair(
                    st.session_state.yaml_data,
                    use_mapping_precheck=st.session_state.use_mapping_precheck,
                )
                st.session_state.arxml_output_adaptive = outputs.get("adaptive", "")
                st.session_state.arxml_output_classic = outputs.get("classic", "")
                st.session_state.arxml_output = st.session_state.arxml_output_adaptive
                st.success("Adaptive and Classic ARXML regenerated.")
            except Exception as exc:
                st.error(f"ARXML regeneration failed: {exc}")

    if st.button("Quick C: Suggest + Apply + Safety"):
        if not st.session_state.yaml_data:
            st.warning("Generate YAML first.")
        else:
            prev_report = engine.safety_check_report(
                st.session_state.yaml_data,
                use_mapping_check=st.session_state.use_mapping_precheck,
            )
            suggestions = engine.generate_ai_suggestions(
                st.session_state.yaml_data,
                use_mapping_check=st.session_state.use_mapping_precheck,
                max_items=5,
            )
            st.session_state.ai_suggestions = suggestions
            selected_ids = [item.get("id", "") for item in suggestions[:2] if item.get("id")]
            st.session_state.yaml_data = engine.apply_selected_suggestions(
                st.session_state.yaml_data,
                selected_ids,
            )
            curr_report = engine.safety_check_report(
                st.session_state.yaml_data,
                use_mapping_check=st.session_state.use_mapping_precheck,
            )
            st.session_state.last_safety_report = curr_report
            st.session_state.improvement_summary = engine.summarize_safety_improvement(prev_report, curr_report)
            st.success("Applied top suggestions and re-ran safety validation.")

    if st.button("Run Session Flow"):
        if not st.session_state.user_request.strip():
            st.warning("Provide a natural language request first.")
        else:
            flow_steps = []
            try:
                st.session_state.yaml_data = engine.generate_simplified_structure(
                    st.session_state.user_request,
                    use_vector_retrieval=True,
                )
                flow_steps.append("1. Generated YAML from current request.")
            except Exception as exc:
                st.warning(f"Session flow YAML generation failed, using fallback: {exc}")
                st.session_state.yaml_data = engine.generate_simplified_structure_fallback(st.session_state.user_request)
                flow_steps.append("1. Generated fallback YAML after model failure.")

            summary = st.session_state.upload_validation_summary
            flow_steps.append(
                "2. Upload validation status: "
                f"accepted={summary.get('accepted', 0)}, rejected={summary.get('rejected', 0)}, unknown={summary.get('unknown', 0)}."
            )

            try:
                outputs = engine.compile_to_arxml_pair(
                    st.session_state.yaml_data,
                    use_mapping_precheck=st.session_state.use_mapping_precheck,
                )
                st.session_state.arxml_output_adaptive = outputs.get("adaptive", "")
                st.session_state.arxml_output_classic = outputs.get("classic", "")
                st.session_state.arxml_output = st.session_state.arxml_output_adaptive
                flow_steps.append("3. Compiled YAML to Adaptive and Classic ARXML.")
            except Exception as exc:
                flow_steps.append(f"3. Compile step failed: {exc}")

            suggestions = engine.generate_ai_suggestions(
                st.session_state.yaml_data,
                use_mapping_check=st.session_state.use_mapping_precheck,
                max_items=5,
            )
            st.session_state.ai_suggestions = suggestions
            flow_steps.append(f"4. Generated {len(suggestions)} AI suggestion(s).")

            apply_ids = [item.get("id", "") for item in suggestions[:2] if item.get("id")]
            if apply_ids:
                before = engine.safety_check_report(
                    st.session_state.yaml_data,
                    use_mapping_check=st.session_state.use_mapping_precheck,
                )
                st.session_state.yaml_data = engine.apply_selected_suggestions(
                    st.session_state.yaml_data,
                    apply_ids,
                )
                flow_steps.append(f"5. Applied selected suggestion IDs: {', '.join(apply_ids)}.")
                after = engine.safety_check_report(
                    st.session_state.yaml_data,
                    use_mapping_check=st.session_state.use_mapping_precheck,
                )
                st.session_state.last_safety_report = after
                st.session_state.improvement_summary = engine.summarize_safety_improvement(before, after)
                flow_steps.append(
                    "6. Safety report refreshed: "
                    f"score={after.get('score', 0)}, summary={after.get('summary', '')}."
                )
            else:
                flow_steps.append("5. No suggestions were applied.")

            st.session_state.session_flow_summary = "\n".join(flow_steps)
            st.success("Session flow completed.")

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
    validation_rows = []
    rejected_files = []
    unknown_files = []
    for file in uploaded_files:
        content = file.read().decode("utf-8", errors="replace")
        validation = _classify_arxml_platform(content)
        row = {
            "name": file.name,
            "classification": validation.get("classification", "unknown"),
            "classic_score": int(validation.get("classic_score", 0) or 0),
            "adaptive_score": int(validation.get("adaptive_score", 0) or 0),
            "reason": str(validation.get("reason", "")),
        }
        validation_rows.append(row)

        classification = row["classification"]
        if classification == "adaptive":
            rejected_files.append((file.name, "Please upload correct ARXML or check ARXML."))
            continue
        if classification == "invalid":
            rejected_files.append((file.name, f"Invalid ARXML: {row['reason']}"))
            continue
        if classification == "unknown":
            unknown_files.append(file.name)

        parsed_text = _parse_autosar_arxml_text(content, source_name=file.name)
        uploaded_texts.append({"name": file.name, "text": parsed_text})

    st.session_state.upload_validation_results = validation_rows
    st.session_state.upload_validation_summary = {
        "total": len(uploaded_files),
        "accepted": len(uploaded_texts),
        "rejected": len(rejected_files),
        "unknown": len(unknown_files),
    }
    st.session_state.uploaded_docs = uploaded_texts
    if uploaded_texts:
        st.sidebar.success(f"Accepted {len(uploaded_texts)} ARXML file(s) for reference.")
    if rejected_files:
        for filename, reason in rejected_files:
            st.sidebar.error(f"Rejected {filename}: {reason}")
    if unknown_files:
        st.sidebar.warning(
            "Unknown ARXML format for: " + ", ".join(unknown_files) + ". Continuing with caution."
        )

if st.sidebar.button("Reset uploaded references"):
    st.session_state.uploaded_docs = []
    st.session_state.upload_validation_results = []
    st.session_state.upload_validation_summary = {
        "total": 0,
        "accepted": 0,
        "rejected": 0,
        "unknown": 0,
    }
    st.session_state.reference_ingest_status = ""
    st.session_state.vector_docs = []
    st.session_state.keyword_docs = []
    st.session_state.engine = _create_engine(
        model_name=st.session_state.model_name,
        model_provider=st.session_state.model_provider,
        api_key=st.session_state.gemini_api_key,
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

with st.expander("ARXML upload validation"):
    rows = st.session_state.upload_validation_results
    summary = st.session_state.upload_validation_summary
    st.markdown(
        "**Summary**  "
        f"Accepted: {summary.get('accepted', 0)} | "
        f"Rejected: {summary.get('rejected', 0)} | "
        f"Unknown: {summary.get('unknown', 0)} | "
        f"Total: {summary.get('total', 0)}"
    )
    if rows:
        for row in rows:
            st.markdown(
                f"- **{row['name']}**: {row['classification']} | "
                f"classic_score={row['classic_score']} | "
                f"adaptive_score={row['adaptive_score']} | "
                f"reason={row['reason']}"
            )
    else:
        st.info("No ARXML validation results yet.")

summary = st.session_state.upload_validation_summary
st.sidebar.caption(
    "ARXML validation: "
    f"accepted={summary.get('accepted', 0)}, "
    f"rejected={summary.get('rejected', 0)}, "
    f"unknown={summary.get('unknown', 0)}, "
    f"total={summary.get('total', 0)}"
)

col1, col2 = st.columns([3, 2])
with col1:
    st.subheader("1. Natural language request")
    st.caption(
        f"Active runtime model: {getattr(engine, 'model_provider', 'ollama')} / {getattr(engine, 'model_name', st.session_state.model_name)}"
    )
    if st.session_state.use_mapping_precheck:
        st.caption("ML pre-check: ON")
    else:
        st.caption("ML pre-check: OFF")
    user_request = st.text_area("Describe the AUTOSAR component or gateway behavior", value=st.session_state.user_request, height=180)
    st.session_state.user_request = user_request

    st.subheader("2. Generate simplified AUTOSAR YAML")
    use_vector = st.checkbox("Use Chroma vector retrieval", value=True)
    show_raw_prompt = st.checkbox("Show raw Ollama prompt", value=False)
    fallback_label = (
        "Allow fallback if Gemini is unavailable"
        if st.session_state.model_provider == "gemini"
        else "Allow fallback if Ollama is unavailable"
    )
    allow_fallback = st.checkbox(fallback_label, value=True)
    if st.button("Generate YAML"):
        if st.session_state.model_provider == "ollama":
            installed_models = _list_ollama_models()
            selected_model = (st.session_state.model_name or "").strip()
            if installed_models and selected_model not in installed_models:
                st.error(
                    "Selected Ollama model is not installed locally. "
                    "Pick a model from the dropdown or run `ollama pull <model>` first."
                )
                _audit_log(
                    auth_ctx["tenant"],
                    auth_ctx["username"],
                    auth_ctx["role"],
                    "generate_yaml_failed",
                    {
                        "error": "ollama_model_not_installed",
                        "model": selected_model,
                        "mode": st.session_state.model_provider,
                    },
                )
                st.stop()

        active_provider = getattr(engine, "model_provider", "ollama")
        active_model = getattr(engine, "model_name", "")
        active_api_key = getattr(engine, "api_key", "")
        provider_mismatch = active_provider != st.session_state.model_provider
        model_mismatch = active_model != st.session_state.model_name
        api_key_mismatch = (
            st.session_state.model_provider == "gemini"
            and (active_api_key or "") != (st.session_state.gemini_api_key or "")
        )
        if provider_mismatch or model_mismatch or api_key_mismatch:
            st.session_state.engine = _create_engine(
                model_name=st.session_state.model_name,
                model_provider=st.session_state.model_provider,
                api_key=st.session_state.gemini_api_key,
                tenant_id=auth_ctx["tenant"],
            )
            engine = st.session_state.engine

        with st.spinner("Running model inference..."):
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
                _audit_log(
                    auth_ctx["tenant"],
                    auth_ctx["username"],
                    auth_ctx["role"],
                    "generate_yaml",
                    {
                        "use_vector": use_vector,
                        "prompt_len": len(user_request or ""),
                        "mode": st.session_state.model_provider,
                        "model": st.session_state.model_name,
                    },
                )
            except Exception as exc:
                if allow_fallback:
                    st.warning(f"Model generation failed ({exc}). Using deterministic fallback YAML.")
                    st.session_state.yaml_data = engine.generate_simplified_structure_fallback(user_request)
                    st.session_state.last_prompt = ""
                    _audit_log(
                        auth_ctx["tenant"],
                        auth_ctx["username"],
                        auth_ctx["role"],
                        "generate_yaml_fallback",
                        {
                            "error": str(exc),
                            "prompt_len": len(user_request or ""),
                            "mode": st.session_state.model_provider,
                            "model": st.session_state.model_name,
                        },
                    )
                else:
                    st.error(f"YAML generation failed: {exc}")
                    _audit_log(
                        auth_ctx["tenant"],
                        auth_ctx["username"],
                        auth_ctx["role"],
                        "generate_yaml_failed",
                        {
                            "error": str(exc),
                            "prompt_len": len(user_request or ""),
                            "mode": st.session_state.model_provider,
                            "model": st.session_state.model_name,
                        },
                    )

    if st.session_state.reference_ingest_status:
        st.caption(st.session_state.reference_ingest_status)

    if st.session_state.yaml_data:
        st.subheader("Generated YAML")
        st.code(st.session_state.yaml_data, language="yaml")
        st.download_button("Download YAML", st.session_state.yaml_data, file_name="autosar_architecture.yaml", mime="text/yaml")
        st.subheader("AI Suggestions")
        tab_suggestions, tab_improvement, tab_session = st.tabs(
            ["Suggestions", "Improvement", "Session Flow"]
        )

        with tab_suggestions:
            if st.button("Generate AI suggestions"):
                st.session_state.ai_suggestions = engine.generate_ai_suggestions(
                    st.session_state.yaml_data,
                    use_mapping_check=st.session_state.use_mapping_precheck,
                    max_items=5,
                )
                st.session_state.selected_suggestion_ids = []

            suggestions = st.session_state.ai_suggestions
            if suggestions:
                st.caption("Review AI-generated suggestions, select the ones to apply, then update YAML.")
                suggestion_options = [
                    f"{item.get('id', '')}: {item.get('title', '')} [{item.get('category', '')}]"
                    for item in suggestions
                ]
                id_lookup = {
                    f"{item.get('id', '')}: {item.get('title', '')} [{item.get('category', '')}]": item.get("id", "")
                    for item in suggestions
                }
                selected_labels = st.multiselect(
                    "Select suggestions to apply",
                    options=suggestion_options,
                    default=[label for label in suggestion_options if id_lookup.get(label) in st.session_state.selected_suggestion_ids],
                )
                st.session_state.selected_suggestion_ids = [id_lookup[label] for label in selected_labels if id_lookup.get(label)]

                table_rows = []
                detail_lookup = {}
                for item in suggestions:
                    suggestion_id = str(item.get("id", ""))
                    title = str(item.get("title", "")).strip()
                    rationale = str(item.get("rationale", "")).strip()
                    suggestion_text = rationale or title
                    confidence = float(item.get("confidence", 0) or 0)
                    confidence = max(0.0, min(1.0, confidence))
                    category = str(item.get("category", "")).upper()

                    bar_count = int(round(confidence * 10))
                    confidence_bar = "[" + ("#" * bar_count) + ("-" * (10 - bar_count)) + "]"

                    table_rows.append(
                        {
                            "ID": suggestion_id,
                            "Category": category,
                            "Suggestion": suggestion_text,
                            "Confidence": confidence,
                            "ConfidenceBar": confidence_bar,
                            "Source": "AI",
                        }
                    )
                    detail_lookup[suggestion_id] = {
                        "title": title,
                        "rationale": rationale,
                        "patch_instruction": str(item.get("patch_instruction", "")).strip(),
                    }

                table_rows.sort(key=lambda row: float(row.get("Confidence", 0.0)), reverse=True)

                avg_confidence = 0.0
                if table_rows:
                    avg_confidence = sum(float(item.get("Confidence", 0) or 0) for item in table_rows) / len(table_rows)
                selected_count = len(st.session_state.selected_suggestion_ids or [])

                st.markdown(
                    """
                    <style>
                    .shuttle-panel {
                        margin-top: 8px;
                        border-radius: 16px;
                        border: 1px solid #28598f;
                        padding: 14px;
                        background:
                            radial-gradient(90% 120% at 0% 0%, rgba(82, 143, 214, 0.24) 0%, rgba(82, 143, 214, 0.05) 58%, rgba(82, 143, 214, 0) 100%),
                            linear-gradient(145deg, #0d213b 0%, #122c4d 45%, #0f2942 100%);
                        box-shadow: 0 10px 28px rgba(8, 22, 39, 0.45);
                    }
                    .shuttle-title {
                        color: #e7f3ff;
                        font-weight: 780;
                        letter-spacing: 0.25px;
                        margin-bottom: 10px;
                        font-size: 0.98rem;
                    }
                    .shuttle-meta {
                        display: flex;
                        gap: 8px;
                        flex-wrap: wrap;
                        margin-bottom: 10px;
                    }
                    .shuttle-chip {
                        color: #d9ebff;
                        border: 1px solid #4a7fb9;
                        background: rgba(106, 168, 235, 0.17);
                        border-radius: 999px;
                        font-size: 0.75rem;
                        padding: 2px 9px;
                        font-weight: 640;
                    }
                    .shuttle-row {
                        margin-top: 10px;
                        padding: 8px 10px;
                        border-radius: 10px;
                        border: 1px solid rgba(133, 183, 240, 0.28);
                        background: rgba(9, 24, 42, 0.52);
                    }
                    .shuttle-row-id {
                        color: #d8e8fb;
                        font-size: 0.79rem;
                        font-weight: 700;
                        margin-bottom: 3px;
                    }
                    .shuttle-row-text {
                        color: #f0f6ff;
                        font-size: 0.9rem;
                        line-height: 1.32;
                    }
                    .shuttle-track {
                        width: 100%;
                        height: 7px;
                        border-radius: 999px;
                        background: #274569;
                        overflow: hidden;
                        margin-top: 7px;
                    }
                    .shuttle-fill {
                        height: 100%;
                        background: linear-gradient(90deg, #67a5f8 0%, #56d2be 100%);
                    }
                    .shuttle-score {
                        color: #b9d5f8;
                        font-size: 0.76rem;
                        margin-top: 4px;
                    }
                    </style>
                    """,
                    unsafe_allow_html=True,
                )

                panel = [
                    '<div class="shuttle-panel">',
                    '<div class="shuttle-title">Suggestion Shuttle Lane</div>',
                    '<div class="shuttle-meta">',
                    f'<span class="shuttle-chip">Total: {len(table_rows)}</span>',
                    f'<span class="shuttle-chip">Selected: {selected_count}</span>',
                    f'<span class="shuttle-chip">Avg confidence: {avg_confidence:.2f}</span>',
                    '</div>',
                ]

                for row in table_rows:
                    confidence = max(0.0, min(1.0, float(row.get("Confidence", 0) or 0)))
                    panel.append(
                        '<div class="shuttle-row">'
                        f'<div class="shuttle-row-id">{html.escape(str(row.get("ID", "")))}</div>'
                        f'<div class="shuttle-row-text">{html.escape(detail_lookup.get(str(row.get("ID", "")), {}).get("title", ""))}</div>'
                        '<div class="shuttle-track">'
                        f'<div class="shuttle-fill" style="width:{int(round(confidence * 100))}%;"></div>'
                        '</div>'
                        f'<div class="shuttle-score">confidence {confidence:.2f}</div>'
                        '</div>'
                    )

                panel.append('</div>')
                st.markdown("".join(panel), unsafe_allow_html=True)

                if st.button("Apply selected suggestions"):
                    if not st.session_state.selected_suggestion_ids:
                        st.warning("Select at least one suggestion.")
                    else:
                        prev_report = engine.safety_check_report(
                            st.session_state.yaml_data,
                            use_mapping_check=st.session_state.use_mapping_precheck,
                        )
                        st.session_state.yaml_data = engine.apply_selected_suggestions(
                            st.session_state.yaml_data,
                            st.session_state.selected_suggestion_ids,
                        )
                        new_report = engine.safety_check_report(
                            st.session_state.yaml_data,
                            use_mapping_check=st.session_state.use_mapping_precheck,
                        )
                        st.session_state.last_safety_report = new_report
                        st.session_state.improvement_summary = engine.summarize_safety_improvement(prev_report, new_report)
                        st.success("Applied selected suggestions and regenerated YAML.")
            else:
                st.info("Generate AI suggestions to start guided improvements.")

        with tab_improvement:
            if st.button("Run improvement check"):
                if st.session_state.improvement_summary is None:
                    st.info("Apply suggestions first to compare previous and new safety reports.")
                else:
                    summary = st.session_state.improvement_summary
                    st.markdown(
                        f"**Previous score:** {summary.get('previous_score', 0)}  \n"
                        f"**New score:** {summary.get('new_score', 0)}  \n"
                        f"**Delta:** {summary.get('delta', 0)}"
                    )
                    improved = summary.get("improved", [])
                    if improved:
                        st.markdown("**What improved**")
                        for item in improved:
                            st.write(f"- {item}")
                    remaining = summary.get("remaining", [])
                    if remaining:
                        st.markdown("**Remaining critical/warning findings**")
                        for item in remaining:
                            st.write(f"- [{item.get('severity', '').upper()}] {item.get('message', '')}")
                    else:
                        st.success("No remaining critical/warning findings.")

        with tab_session:
            if st.session_state.session_flow_summary:
                st.text(st.session_state.session_flow_summary)
            else:
                st.info("Use sidebar action 'Run Session Flow' to populate this summary.")

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
                outputs = engine.compile_to_arxml_pair(
                    st.session_state.yaml_data,
                    use_mapping_precheck=st.session_state.use_mapping_precheck,
                )
                st.session_state.arxml_output_adaptive = outputs.get("adaptive", "")
                st.session_state.arxml_output_classic = outputs.get("classic", "")
                st.session_state.arxml_output = st.session_state.arxml_output_adaptive
                _audit_log(
                    auth_ctx["tenant"],
                    auth_ctx["username"],
                    auth_ctx["role"],
                    "compile_arxml",
                    {
                        "yaml_len": len(st.session_state.yaml_data or ""),
                        "use_mapping_precheck": st.session_state.use_mapping_precheck,
                        "outputs": ["adaptive", "classic"],
                    },
                )
            except Exception as exc:
                st.session_state.arxml_output = ""
                st.session_state.arxml_output_adaptive = ""
                st.session_state.arxml_output_classic = ""
                st.error(f"Compile failed: {exc}")
                _audit_log(
                    auth_ctx["tenant"],
                    auth_ctx["username"],
                    auth_ctx["role"],
                    "compile_arxml_failed",
                    {
                        "yaml_len": len(st.session_state.yaml_data or ""),
                        "error": str(exc),
                        "use_mapping_precheck": st.session_state.use_mapping_precheck,
                    },
                )

if st.session_state.arxml_output_adaptive or st.session_state.arxml_output_classic:
    if st.session_state.arxml_output_adaptive:
        st.markdown("**Adaptive ARXML**")
        st.code(st.session_state.arxml_output_adaptive, language="xml")
        st.download_button(
            "Download Adaptive ARXML",
            st.session_state.arxml_output_adaptive,
            file_name="autosar_output_adaptive.arxml",
            mime="application/xml",
        )

    if st.session_state.arxml_output_classic:
        st.markdown("**Classic ARXML**")
        st.code(st.session_state.arxml_output_classic, language="xml")
        st.download_button(
            "Download Classic ARXML",
            st.session_state.arxml_output_classic,
            file_name="autosar_output_classic.arxml",
            mime="application/xml",
        )

st.subheader("4. Safety validation")
st.caption("Run safety assessment and review findings with score, severity counts, and actionable fixes.")
if st.button("Run safety check"):
    if not st.session_state.yaml_data:
        st.warning("Generate YAML first.")
    else:
        current_yaml = st.session_state.yaml_data
        report = engine.safety_check_report(
            current_yaml,
            use_mapping_check=st.session_state.use_mapping_precheck,
        )

        # Auto-apply tenant baseline when core sections are structurally missing.
        structural_messages = {
            "No ECU definitions found; add at least one ECU for deployment and safety allocation.",
            "No service descriptions generated; add services so Adaptive communication can be validated.",
            "No signal mappings generated; add signals so Classic-to-Adaptive flow is testable.",
        }
        findings = report.get("findings", []) if isinstance(report.get("findings"), list) else []
        missing_structure = any(
            item.get("severity") == "critical" and item.get("message") in structural_messages
            for item in findings
            if isinstance(item, dict)
        )

        baseline_profile = (auth_ctx.get("tenant") or "default").strip().lower() or "default"
        if missing_structure:
            try:
                patched_yaml = engine.apply_demo_baseline_yaml(current_yaml, profile=baseline_profile)
                patched_report = engine.safety_check_report(
                    patched_yaml,
                    use_mapping_check=st.session_state.use_mapping_precheck,
                )
                old_score = int(report.get("score", 0) or 0)
                new_score = int(patched_report.get("score", 0) or 0)
                if new_score >= old_score:
                    st.session_state.yaml_data = patched_yaml
                    report = patched_report
                    st.info(
                        f"Applied demo baseline profile '{baseline_profile}' before validation to complete missing sections."
                    )
            except Exception:
                pass

        st.session_state.last_safety_report = report
        issues = [
            f"[{item.get('severity', 'info').upper()}] {item.get('message', '')}"
            for item in (report.get("findings", []) if isinstance(report.get("findings", []), list) else [])
            if item.get("severity") in {"critical", "warning"}
        ]
        _audit_log(
            auth_ctx["tenant"],
            auth_ctx["username"],
            auth_ctx["role"],
            "run_safety_check",
            {
                "issue_count": len(issues),
                "score": int(report.get("score", 0) or 0),
                "summary": str(report.get("summary", "")),
                "use_mapping_precheck": st.session_state.use_mapping_precheck,
            },
        )
        if issues:
            st.error("Safety issues found")
        else:
            st.success("No high-level safety issues detected.")

if st.session_state.last_safety_report:
    report = st.session_state.last_safety_report
    counts = report.get("counts", {}) if isinstance(report.get("counts", {}), dict) else {}
    critical_count = int(counts.get("critical", 0) or 0)
    warning_count = int(counts.get("warning", 0) or 0)
    info_count = int(counts.get("info", 0) or 0)
    summary_text = str(report.get("summary", ""))
    score_value = int(report.get("score", 0) or 0)

    if critical_count > 0:
        st.error(f"Safety status: FAILED | {summary_text}")
    elif warning_count > 0:
        st.warning(f"Safety status: PASS WITH WARNINGS | {summary_text}")
    else:
        st.success(f"Safety status: PASSED | {summary_text}")

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Safety score", f"{score_value}/100")
    m2.metric("Critical", str(critical_count))
    m3.metric("Warning", str(warning_count))
    m4.metric("Info", str(info_count))

    findings = report.get("findings", []) if isinstance(report.get("findings", []), list) else []
    findings = [item for item in findings if isinstance(item, dict)]

    show_info_findings = st.checkbox("Show informational findings", value=False, key="show_info_findings")
    visible_findings = [
        item
        for item in findings
        if show_info_findings or str(item.get("severity", "")).lower() in {"critical", "warning"}
    ]

    if visible_findings:
        st.markdown("**Findings**")
        table_rows = [
            {
                "Severity": str(item.get("severity", "")).upper(),
                "Message": str(item.get("message", "")),
            }
            for item in visible_findings
        ]
        st.dataframe(table_rows, use_container_width=True, hide_index=True)
    else:
        st.info("No findings to display for the selected filter.")

    action_items = [
        str(item.get("message", ""))
        for item in findings
        if str(item.get("severity", "")).lower() in {"critical", "warning"}
    ]
    if action_items:
        st.markdown("**Recommended next actions**")
        for message in action_items[:5]:
            st.write(f"- {message}")

st.markdown("---")
st.markdown("#### Notes\n- Upload ARXML files on the sidebar to use them as reference material.\n- Generated ARXML is a simplified demonstration output.\n- Choose model provider in the sidebar (Ollama or Gemini).")













































































































