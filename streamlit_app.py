






import threading
import subprocess
import importlib
import urllib.request
import urllib.parse
import base64
import hmac
import os
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
    st.session_state.model_name = "gemini-2.5-flash"
if "model_provider" not in st.session_state:
    st.session_state.model_provider = "gemini"
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
if "use_mapping_precheck" not in st.session_state:
    st.session_state.use_mapping_precheck = True

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
            else "llama3.1"
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

col1, col2 = st.columns([2, 1])
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
                st.session_state.arxml_output = engine.compile_to_arxml(
                    st.session_state.yaml_data,
                    use_mapping_precheck=st.session_state.use_mapping_precheck,
                )
                _audit_log(
                    auth_ctx["tenant"],
                    auth_ctx["username"],
                    auth_ctx["role"],
                    "compile_arxml",
                    {
                        "yaml_len": len(st.session_state.yaml_data or ""),
                        "use_mapping_precheck": st.session_state.use_mapping_precheck,
                    },
                )
            except Exception as exc:
                st.session_state.arxml_output = ""
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

if st.session_state.arxml_output:
    st.code(st.session_state.arxml_output, language="xml")
    st.download_button("Download ARXML", st.session_state.arxml_output, file_name="autosar_output.arxml", mime="application/xml")

st.subheader("4. Safety validation")
if st.button("Run safety check"):
    if not st.session_state.yaml_data:
        st.warning("Generate YAML first.")
    else:
        issues = engine.safety_check(
            st.session_state.yaml_data,
            use_mapping_check=st.session_state.use_mapping_precheck,
        )
        _audit_log(
            auth_ctx["tenant"],
            auth_ctx["username"],
            auth_ctx["role"],
            "run_safety_check",
            {"issue_count": len(issues), "use_mapping_precheck": st.session_state.use_mapping_precheck},
        )
        if issues:
            st.error("Safety issues found")
            for issue in issues:
                st.write(f"- {issue}")
        else:
            st.success("No high-level safety issues detected.")

st.markdown("---")
st.markdown("#### Notes\n- Upload ARXML files on the sidebar to use them as reference material.\n- Generated ARXML is a simplified demonstration output.\n- Choose model provider in the sidebar (Ollama or Gemini).")













































































































