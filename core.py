import re
import shutil
import subprocess
import sys
import os
import hashlib
import threading
import json
import pickle
import urllib.error
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import List, Dict, Optional

import chromadb
from chromadb.config import Settings
from chromadb.utils import embedding_functions
import yaml
from jinja2 import Environment, FileSystemLoader, select_autoescape
from pypdf import PdfReader

BASE_DIR = Path(__file__).resolve().parent
DEMO_BASELINE_PATH = BASE_DIR / "data" / "demo_validation_baseline.json"
MAPPING_BASELINE_MODEL_PATH = BASE_DIR / "models" / "gnn_mapping_baseline.pkl"
_EMBEDDING_FUNCTION = None
_EMBEDDING_FUNCTION_LOCK = threading.Lock()

DEFAULT_CHUNK_SIZE = 1200
DEFAULT_CHUNK_OVERLAP = 180
MAX_PROMPT_REFERENCE_CHARS = 3200
MAX_PROMPT_CHUNK_CHARS = 700


def _strip_ansi_and_controls(text: str) -> str:
    cleaned = text or ""
    # Remove ANSI escape sequences and spinner control noise from CLI output.
    cleaned = re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]", "", cleaned)
    cleaned = re.sub(r"\x1b\][^\x07]*(?:\x07|\x1b\\)", "", cleaned)
    cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", cleaned)
    return cleaned.strip()


def _get_embedding_function():
    global _EMBEDDING_FUNCTION
    if _EMBEDDING_FUNCTION is None:
        with _EMBEDDING_FUNCTION_LOCK:
            if _EMBEDDING_FUNCTION is None:
                _EMBEDDING_FUNCTION = embedding_functions.SentenceTransformerEmbeddingFunction(
                    model_name="all-MiniLM-L6-v2"
                )
    return _EMBEDDING_FUNCTION


def _parse_autosar_arxml_root(root: ET.Element, source_name: str = "ARXML") -> str:
    ns = {"a": "http://autosar.org/schema/r4.0"}
    text_chunks = [f"# AUTOSAR file: {source_name}"]

    def localname(tag: str) -> str:
        return tag.rsplit('}', 1)[-1] if '}' in tag else tag

    for pkg in root.findall(".//a:AR-PACKAGE", ns):
        short_name = pkg.findtext("a:SHORT-NAME", default="", namespaces=ns)
        if short_name:
            text_chunks.append(f"## Package: {short_name}")

        for element in pkg.findall("a:ELEMENTS/*", ns):
            tag = localname(element.tag)
            name = element.findtext("a:SHORT-NAME", default="", namespaces=ns)
            if name:
                text_chunks.append(f"### {tag}: {name}")
            desc = element.findtext(".//a:DESC/a:L-2", default="", namespaces=ns)
            if desc:
                text_chunks.append(f"Description: {desc.strip()}")

            if tag in {"SENDER-RECEIVER-INTERFACE", "CLIENT-SERVER-INTERFACE", "PARAMETER-INTERFACE"}:
                data_elements = []
                for data in element.findall(".//a:VARIABLE-DATA-PROTOTYPE", ns):
                    data_name = data.findtext("a:SHORT-NAME", default="", namespaces=ns)
                    data_type = data.findtext(".//a:TYPE-TREF", default="", namespaces=ns)
                    if data_name and data_type:
                        data_elements.append(f"- {data_name}: {data_type}")
                if data_elements:
                    text_chunks.append("Data elements:")
                    text_chunks.extend(data_elements)

            if tag in {"APPLICATION-SW-COMPONENT-TYPE", "COMPOSITION-SW-COMPONENT-TYPE"}:
                ports = []
                for port in element.findall(".//a:PORTS/*", ns):
                    port_name = port.findtext("a:SHORT-NAME", default="", namespaces=ns)
                    iface = port.findtext(".//a:PROVIDED-INTERFACE-TREF|.//a:REQUIRED-INTERFACE-TREF", default="", namespaces=ns)
                    if port_name and iface:
                        ports.append(f"- {port_name}: {iface}")
                if ports:
                    text_chunks.append("Ports:")
                    text_chunks.extend(ports)

            if tag == "RUNNABLE-ENTITY":
                runnable_name = name or element.findtext("a:SHORT-NAME", default="", namespaces=ns)
                if runnable_name:
                    text_chunks.append(f"Runnable: {runnable_name}")
                min_int = element.findtext("a:MINIMUM-START-INTERVAL", default="", namespaces=ns)
                if min_int:
                    text_chunks.append(f"- Minimum start interval: {min_int}")

            if tag.endswith("-CONNECTOR"):
                connector_name = name or tag
                text_chunks.append(f"Connector: {connector_name}")
                provider = element.findtext(".//a:PROVIDER-IREF/a:CONTEXT-COMPONENT-REF", default="", namespaces=ns)
                requester = element.findtext(".//a:REQUESTER-IREF/a:CONTEXT-COMPONENT-REF", default="", namespaces=ns)
                if provider and requester:
                    text_chunks.append(f"- Provider component: {provider}")
                    text_chunks.append(f"- Requester component: {requester}")

    return "\n\n".join(text_chunks)


def _parse_autosar_arxml(path: Path) -> str:
    try:
        tree = ET.parse(path)
        return _parse_autosar_arxml_root(tree.getroot(), source_name=path.name)
    except ET.ParseError:
        return f"Failed to parse AUTOSAR XML file {path.name}."


def _parse_autosar_arxml_text(xml_text: str, source_name: str = "Uploaded ARXML") -> str:
    try:
        root = ET.fromstring(xml_text)
        return _parse_autosar_arxml_root(root, source_name=source_name)
    except ET.ParseError:
        return f"Failed to parse AUTOSAR XML content from {source_name}."


def _extract_text_from_pdf(pdf_path: Path, timeout_seconds: float = 5.0) -> str:
    """Extract text from a PDF file while avoiding startup hangs on problematic files."""
    script = f"""
import sys
from pathlib import Path
from pypdf import PdfReader
pdf_path = Path(sys.argv[1])
try:
    reader = PdfReader(pdf_path)
    text_chunks = [f"# AUTOSAR Document: {{pdf_path.name}}"]
    for page_num, page in enumerate(reader.pages, start=1):
        text = page.extract_text()
        if text:
            text_chunks.append(f"## Page {{page_num}}")
            text_chunks.append(text)
    sys.stdout.write("\\n\\n".join(text_chunks))
except Exception as e:
    sys.stderr.write(str(e))
    sys.exit(1)
"""
    try:
        result = subprocess.run(
            [sys.executable, "-c", script, str(pdf_path)],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
        error_message = result.stderr.strip() or "no text extracted"
        return f"Failed to extract text from PDF {pdf_path.name}: {error_message}"
    except subprocess.TimeoutExpired:
        return f"Failed to extract text from PDF {pdf_path.name}: timed out after {timeout_seconds} seconds"


def _load_reference_documents(load_pdfs: bool = False) -> List[Dict[str, str]]:
    docs = []
    reference_dir = BASE_DIR / "data"
    reference_dir.mkdir(exist_ok=True)
    for path in sorted(reference_dir.glob("*.md")):
        docs.append({"name": path.name, "text": path.read_text(encoding="utf-8")})

    autosar_dir = BASE_DIR / "AUTOSAR_WorkflowExample" / "EcuSystemDescription"
    if autosar_dir.exists():
        for arxml_path in sorted(autosar_dir.rglob("*.arxml")):
            docs.append({"name": arxml_path.name, "text": _parse_autosar_arxml(arxml_path)})

    if load_pdfs:
        pdf_dir = BASE_DIR / "autosar_docs"
        if pdf_dir.exists():
            for pdf_subdir in ["adaptive", "classic"]:
                pdf_subdir_path = pdf_dir / pdf_subdir
                if pdf_subdir_path.exists():
                    for pdf_path in sorted(pdf_subdir_path.glob("*.pdf")):
                        pdf_text = _extract_text_from_pdf(pdf_path)
                        docs.append({"name": pdf_path.name, "text": pdf_text})

    return docs


def _simple_retrieve(query: str, docs: List[Dict[str, str]], top_k: int = 2) -> List[str]:
    query_tokens = set(re.findall(r"\w+", query.lower()))
    scored = []
    for doc in docs:
        doc_tokens = set(re.findall(r"\w+", doc["text"].lower()))
        score = len(query_tokens.intersection(doc_tokens))
        scored.append((score, doc["text"]))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [text for _, text in scored[:top_k] if _ > 0]


def _chunk_text(text: str, chunk_size: int = DEFAULT_CHUNK_SIZE, overlap: int = DEFAULT_CHUNK_OVERLAP) -> List[str]:
    cleaned = (text or "").strip()
    if not cleaned:
        return []
    if len(cleaned) <= chunk_size:
        return [cleaned]

    pieces = [piece.strip() for piece in re.split(r"\n\s*\n", cleaned) if piece.strip()]
    chunks: List[str] = []
    buffer = ""
    for piece in pieces:
        if not buffer:
            buffer = piece
            continue
        candidate = f"{buffer}\n\n{piece}"
        if len(candidate) <= chunk_size:
            buffer = candidate
            continue
        chunks.append(buffer)
        tail = buffer[-overlap:] if overlap > 0 else ""
        buffer = f"{tail}\n\n{piece}" if tail else piece

    if buffer:
        chunks.append(buffer)

    # Fallback for very long single paragraphs.
    normalized: List[str] = []
    stride = max(1, chunk_size - overlap)
    for chunk in chunks:
        if len(chunk) <= chunk_size:
            normalized.append(chunk)
            continue
        for start in range(0, len(chunk), stride):
            section = chunk[start:start + chunk_size].strip()
            if section:
                normalized.append(section)

    return normalized


class AutosarHackathonEngine:
    def __init__(
        self,
        model_name: str = "llama3.1",
        top_k: int = 3,
        load_pdfs: bool = False,
        tenant_id: str = "public",
        model_provider: str = "ollama",
        api_key: Optional[str] = None,
    ):
        self.model_name = model_name
        self.model_provider = (model_provider or "ollama").strip().lower()
        self.api_key = (api_key or os.environ.get("GEMINI_API_KEY", "")).strip()
        self.docs = _load_reference_documents(load_pdfs=load_pdfs)
        tenant_id = (tenant_id or "public").strip().lower()
        self.tenant_id = re.sub(r"[^a-z0-9_-]+", "_", tenant_id) or "public"
        self.template_env = Environment(
            loader=FileSystemLoader(BASE_DIR / "templates"),
            autoescape=select_autoescape(enabled_extensions=("xml",)),
        )
        self.top_k = top_k
        self.vector_store = None
        self._vector_store_lock = threading.Lock()
        self.vector_warmup_error: Optional[str] = None
        self.last_retrieved: List[str] = []
        self.last_prompt: str = ""
        self.retrieval_cache: Dict[str, List[str]] = {}
        self.mapping_baseline_model = None
        self.mapping_baseline_error: Optional[str] = None
        self.last_mapping_assessment: List[Dict[str, object]] = []
        self.demo_baseline = self._load_demo_validation_baseline()

    def _load_demo_validation_baseline(self) -> Dict[str, object]:
        fallback_profile = {
            "default_asil": "B",
            "default_processor": "Generic_MCU",
            "services": [
                {
                    "name": "TelemetrySomeIpService",
                    "protocol": "SOME/IP",
                    "description": "Demo baseline SOME/IP service for Adaptive communication",
                }
            ],
            "signals": [
                {
                    "name": "CabinTemp_CAN",
                    "source": "CAN",
                    "destination": "SOME/IP",
                    "transform": "linear_scale",
                }
            ],
            "safety": [
                {"check": "watchdog_supervision", "note": "Demo baseline safety monitor enabled"}
            ],
        }
        fallback = {
            "baseline_profiles": {
                "default": fallback_profile,
            }
        }

        if not DEMO_BASELINE_PATH.exists():
            return fallback

        try:
            loaded = json.loads(DEMO_BASELINE_PATH.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                return fallback
            merged = dict(fallback)
            merged.update(loaded)

            # Backward compatibility: if profile map is absent, treat root-level
            # fields as the default profile.
            if "baseline_profiles" not in merged:
                merged["baseline_profiles"] = {
                    "default": {
                        "default_asil": merged.get("default_asil", fallback_profile["default_asil"]),
                        "default_processor": merged.get("default_processor", fallback_profile["default_processor"]),
                        "services": merged.get("services", fallback_profile["services"]),
                        "signals": merged.get("signals", fallback_profile["signals"]),
                        "safety": merged.get("safety", fallback_profile["safety"]),
                    }
                }
            return merged
        except Exception:
            return fallback

    def get_demo_baseline_profiles(self) -> List[str]:
        profiles = self.demo_baseline.get("baseline_profiles", {}) if isinstance(self.demo_baseline, dict) else {}
        if not isinstance(profiles, dict) or not profiles:
            return ["default"]
        return sorted(profiles.keys())

    def _resolve_demo_profile(self, profile: str) -> Dict[str, object]:
        profiles = self.demo_baseline.get("baseline_profiles", {}) if isinstance(self.demo_baseline, dict) else {}
        if not isinstance(profiles, dict) or not profiles:
            return {}

        requested = (profile or "default").strip().lower()
        if requested in profiles and isinstance(profiles[requested], dict):
            return profiles[requested]
        if "default" in profiles and isinstance(profiles["default"], dict):
            return profiles["default"]

        for value in profiles.values():
            if isinstance(value, dict):
                return value
        return {}

    @staticmethod
    def _doc_key(doc: Dict[str, str]) -> str:
        return f"{doc['name']}::{doc['text']}"

    @staticmethod
    def _doc_id(doc: Dict[str, str]) -> str:
        return hashlib.sha1(AutosarHackathonEngine._doc_key(doc).encode("utf-8")).hexdigest()

    @staticmethod
    def _chunk_id(source_name: str, chunk_index: int, chunk_text: str) -> str:
        key = f"{source_name}::{chunk_index}::{chunk_text}"
        return hashlib.sha1(key.encode("utf-8")).hexdigest()

    def _prepare_chunk_records(self, docs: List[Dict[str, str]]) -> List[Dict[str, object]]:
        records: List[Dict[str, object]] = []
        for doc in docs:
            name = str(doc.get("name", "unknown"))
            text = str(doc.get("text", ""))
            chunks = _chunk_text(text)
            total = len(chunks)
            for idx, chunk in enumerate(chunks):
                records.append(
                    {
                        "id": self._chunk_id(name, idx, chunk),
                        "metadata": {"source": name, "chunk_index": idx, "chunk_total": total},
                        "document": chunk,
                    }
                )
        return records

    def _build_vector_store(self, docs: List[Dict[str, str]]):
        persist_dir = BASE_DIR / "chroma_db" / self.tenant_id
        persist_dir.mkdir(exist_ok=True)
        client = chromadb.Client(
            settings=Settings(persist_directory=str(persist_dir), is_persistent=True)
        )
        embedding_function = _get_embedding_function()
        collection = client.get_or_create_collection(
            name=f"autosar_docs_{self.tenant_id}",
            embedding_function=embedding_function,
        )

        if collection.count() == 0 and docs:
            chunk_records = self._prepare_chunk_records(docs)
            collection.upsert(
                ids=[record["id"] for record in chunk_records],
                metadatas=[record["metadata"] for record in chunk_records],
                documents=[record["document"] for record in chunk_records],
            )
        return collection

    def _ensure_vector_store(self):
        if self.vector_store is None:
            with self._vector_store_lock:
                if self.vector_store is None:
                    self.vector_store = self._build_vector_store(self.docs)
        return self.vector_store

    def warm_up_vector_store(self) -> bool:
        try:
            self._ensure_vector_store()
            self.vector_warmup_error = None
            return True
        except Exception as exc:
            self.vector_warmup_error = str(exc)
            return False

    def is_vector_store_ready(self) -> bool:
        return self.vector_store is not None

    def add_reference_documents(self, docs: List[Dict[str, str]]) -> Dict[str, int]:
        if not docs:
            return {"added": 0, "already_present": 0}

        existing_keys = {self._doc_key(doc) for doc in self.docs}
        new_docs = [doc for doc in docs if self._doc_key(doc) not in existing_keys]
        already_present = len(docs) - len(new_docs)
        if not new_docs:
            return {"added": 0, "already_present": already_present}

        self.docs.extend(new_docs)
        self.retrieval_cache.clear()
        if self.vector_store is not None:
            chunk_records = self._prepare_chunk_records(new_docs)
            self.vector_store.upsert(
                ids=[record["id"] for record in chunk_records],
                metadatas=[record["metadata"] for record in chunk_records],
                documents=[record["document"] for record in chunk_records],
            )
        return {"added": len(new_docs), "already_present": already_present}

    def _retrieve_documents(self, query: str, use_vector: bool = True) -> List[str]:
        cache_key = f"{int(use_vector)}::{query.strip().lower()}"
        cached = self.retrieval_cache.get(cache_key)
        if cached is not None:
            self.last_retrieved = cached
            return cached

        if use_vector:
            vector_store = self._ensure_vector_store()
            result = vector_store.query(query_texts=[query], n_results=max(self.top_k * 3, self.top_k))
            documents = result.get("documents", [[]])[0]
            self.last_retrieved = [doc for doc in documents if doc]
            self.retrieval_cache[cache_key] = self.last_retrieved
            return self.last_retrieved
        self.last_retrieved = _simple_retrieve(query, self.docs, self.top_k)
        self.retrieval_cache[cache_key] = self.last_retrieved
        return self.last_retrieved

    def retrieve_documents(self, query: str, use_vector: bool = True) -> List[str]:
        return self._retrieve_documents(query, use_vector)

    def get_example_prompt(self, use_case: str = "can_to_someip") -> str:
        examples = {
            "can_to_someip": (
                "Design a Software-Defined Vehicle architecture that converts a legacy "
                "bit-packed CAN signal domain into Adaptive Platform SOME/IP services. "
                "The solution should map Classic Platform signals through an ECU bridge, "
                "balance processing across multiple cores, and assign ASIL levels for "
                "critical routing and service safety."
            ),
            "load_optimization": (
                "Create an AUTOSAR architecture focused on runtime load optimization for "
                "multi-core ECUs. Include service partitioning, scheduled task balancing, "
                "and safety checks for ASIL C/D communication paths."
            ),
        }
        return examples.get(use_case, examples["can_to_someip"])

    def _build_prompt(self, user_request: str, retrieved_docs: List[str]) -> str:
        compact_references: List[str] = []
        used_chars = 0
        for index, doc in enumerate(retrieved_docs or [], start=1):
            excerpt = (doc or "").strip()
            if len(excerpt) > MAX_PROMPT_CHUNK_CHARS:
                excerpt = excerpt[:MAX_PROMPT_CHUNK_CHARS].rstrip() + " ..."
            candidate = f"Reference chunk {index}:\n{excerpt}"
            if used_chars + len(candidate) > MAX_PROMPT_REFERENCE_CHARS:
                break
            compact_references.append(candidate)
            used_chars += len(candidate)

        prompt = [
            "You are an AUTOSAR automation assistant.",
            "Use the reference material below to generate a simplified AUTOSAR architecture in YAML.",
            "Return only the YAML content. Do not add any explanation or markdown fences.",
            "Focus on service mapping, signal routing, ECU load distribution, and ASIL-aware safeguards.",
            "If the request mentions Classic CAN and Adaptive SOME/IP, create a bridge section describing the service transformation.",
            "Use the following YAML structure: system, ecus, services, signals, safety.",
            "Reference material:",
        ]
        prompt.extend(compact_references or ["(no reference material available)"])
        prompt.append("User request:")
        prompt.append(user_request.strip())
        prompt.append(
            "Produce YAML with these sections: system, ecus, services, signals, safety. Keep names short and valid."
        )
        return "\n\n".join(prompt)

    def _call_ollama(self, prompt: str) -> str:
        api_payload = json.dumps(
            {
                "model": self.model_name,
                "prompt": prompt,
                "stream": False,
            }
        ).encode("utf-8")

        try:
            request = urllib.request.Request(
                "http://127.0.0.1:11434/api/generate",
                data=api_payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=180) as response:
                payload = json.loads(response.read().decode("utf-8", errors="replace"))
            text_response = (payload.get("response") or "").strip()
            if not text_response:
                raise RuntimeError("Ollama returned an empty response.")
            return text_response
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            # Fall back to CLI if the local Ollama API is not reachable.
            pass

        ollama_executable = shutil.which("ollama")
        if not ollama_executable and sys.platform.startswith("win"):
            candidate = Path.home() / "AppData" / "Local" / "Programs" / "Ollama" / "ollama.exe"
            if candidate.exists():
                ollama_executable = str(candidate)

        if not ollama_executable:
            raise RuntimeError(
                "Ollama executable was not found. Install Ollama and ensure ollama is available on PATH."
            )

        try:
            result = subprocess.run(
                [ollama_executable, "run", self.model_name, prompt],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=180,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                "Ollama executable was not found. Install Ollama and ensure ollama is available on PATH."
            ) from exc
        except OSError as exc:
            if getattr(exc, "winerror", None) == 206:
                raise RuntimeError(
                    "Ollama inference command exceeded Windows command length limits."
                ) from exc
            raise
        if result.returncode != 0:
            raw_error = result.stderr.strip() or result.stdout.strip()
            clean_error = _strip_ansi_and_controls(raw_error)
            error_lower = clean_error.lower()

            if "pull model manifest: file does not exist" in error_lower:
                raise RuntimeError(
                    "Ollama model was not found. Set a valid model name (for example, llama3.1) "
                    "and run `ollama pull <model>` first."
                )

            raise RuntimeError(
                f"Ollama inference failed: {clean_error or 'unknown CLI error'}"
            )
        return result.stdout.strip()

    def _call_gemini(self, prompt: str) -> str:
        api_key = (self.api_key or "").strip()
        if not api_key:
            raise RuntimeError(
                "Gemini API key is missing. Provide it in the UI or set GEMINI_API_KEY."
            )

        model = (self.model_name or "gemini-2.0-flash").strip()
        if model.startswith("models/"):
            model = model.split("/", 1)[1]
        encoded_model = urllib.parse.quote(model, safe="")
        endpoint = (
            f"https://generativelanguage.googleapis.com/v1beta/models/{encoded_model}:generateContent"
            f"?key={urllib.parse.quote(api_key, safe='')}"
        )
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.2,
                "topP": 0.9,
                "maxOutputTokens": 2048,
            },
        }
        request = urllib.request.Request(
            endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                body = response.read().decode("utf-8", errors="replace")
            data = json.loads(body)
        except urllib.error.HTTPError as exc:
            details = exc.read().decode("utf-8", errors="replace") if hasattr(exc, "read") else str(exc)
            details_lower = details.lower()
            if exc.code == 404 and "listmodels" in details_lower:
                suggestions = self._list_gemini_models(api_key)
                if suggestions:
                    hint = ", ".join(suggestions[:6])
                    raise RuntimeError(
                        "Gemini model is not available for this API key/version. "
                        f"Try one of: {hint}"
                    ) from exc
                raise RuntimeError(
                    "Gemini model is not available for this API key/version. "
                    "Use a currently supported model such as gemini-2.0-flash."
                ) from exc
            raise RuntimeError(f"Gemini request failed: {details}") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Gemini request failed: {exc}") from exc

        candidates = data.get("candidates") or []
        if not candidates:
            raise RuntimeError("Gemini returned no candidates.")

        parts = candidates[0].get("content", {}).get("parts", [])
        text_response = "\n".join(str(part.get("text", "")) for part in parts if part.get("text"))
        text_response = text_response.strip()
        if not text_response:
            raise RuntimeError("Gemini returned an empty response.")
        return text_response

    def _list_gemini_models(self, api_key: str) -> List[str]:
        endpoint = (
            "https://generativelanguage.googleapis.com/v1beta/models"
            f"?key={urllib.parse.quote(api_key, safe='')}"
        )
        request = urllib.request.Request(endpoint, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = response.read().decode("utf-8", errors="replace")
            data = json.loads(body)
        except Exception:
            return []

        names: List[str] = []
        for item in data.get("models", []):
            if not isinstance(item, dict):
                continue
            methods = item.get("supportedGenerationMethods", []) or []
            if "generateContent" not in methods:
                continue
            name = str(item.get("name", "")).strip()
            if name.startswith("models/"):
                name = name.split("/", 1)[1]
            if name:
                names.append(name)

        # Preserve order while removing duplicates.
        seen = set()
        deduped = []
        for name in names:
            if name in seen:
                continue
            seen.add(name)
            deduped.append(name)
        return deduped

    def _call_model(self, prompt: str) -> str:
        if self.model_provider == "gemini":
            return self._call_gemini(prompt)
        return self._call_ollama(prompt)

    def generate_simplified_structure_fallback(self, user_request: str) -> str:
        """
        Deterministic local fallback so the UI remains usable without Ollama.
        """
        name_match = re.search(r"component\s+named\s+([A-Za-z0-9_]+)", user_request, re.IGNORECASE)
        component_name = name_match.group(1) if name_match else "GatewayComponent"
        return f"""system:
  name: SDV_Gateway
  mode: demo_fallback
ecus:
  - name: ECU_CLASSIC
    asil: B
    processor: MCU_A
  - name: ECU_ADAPTIVE
    asil: C
    processor: SoC_B
services:
  - name: SOMEIP_{component_name}
    protocol: SOME/IP
signals:
  - source: CAN
    destination: SOME/IP
    transform: normalize_and_route
safety:
  - check: fallback_generation_used
  - note: Ollama unavailable, generated deterministic scaffold
"""

    def generate_simplified_structure(self, user_request: str, use_vector_retrieval: bool = True) -> str:
        retrieved_docs = self._retrieve_documents(user_request, use_vector_retrieval)
        prompt = self._build_prompt(user_request, retrieved_docs)
        self.last_prompt = prompt
        return self._extract_yaml_payload(self._call_model(prompt))

    def get_last_prompt(self) -> str:
        return self.last_prompt

    def _extract_yaml_payload(self, text: str) -> str:
        payload = (text or "").replace("\r\n", "\n").strip()
        fenced_match = re.search(r"```(?:yaml|yml)?\s*(.*?)```", payload, flags=re.IGNORECASE | re.DOTALL)
        if fenced_match:
            payload = fenced_match.group(1).strip()

        root_key_match = re.search(r"(?m)^(system|ecus|services|signals|safety)\s*:", payload)
        if root_key_match:
            payload = payload[root_key_match.start():].strip()

        return payload

    def _parse_yaml_spec(self, yaml_text: str) -> Dict[str, object]:
        payload = self._extract_yaml_payload(yaml_text)
        try:
            parsed = yaml.safe_load(payload)
        except yaml.YAMLError as exc:
            mark = getattr(exc, "problem_mark", None)
            if mark is not None:
                line = mark.line + 1
                col = mark.column + 1
                raise ValueError(f"Invalid YAML near line {line}, column {col}.") from exc
            raise ValueError("Invalid YAML syntax.") from exc

        if not isinstance(parsed, dict):
            raise ValueError("Parsed YAML must be a dictionary.")

        return parsed

    def apply_demo_baseline_yaml(self, yaml_text: str, profile: str = "default") -> str:
        spec = self._parse_yaml_spec(yaml_text)
        baseline = self._resolve_demo_profile(profile)

        spec.setdefault("system", {})
        spec.setdefault("ecus", [])
        spec.setdefault("services", [])
        spec.setdefault("signals", [])
        spec.setdefault("safety", [])

        ecus = spec["ecus"] if isinstance(spec.get("ecus"), list) else []
        default_asil = str(baseline.get("default_asil", "B"))
        default_processor = str(baseline.get("default_processor", "Generic_MCU"))
        for ecu in ecus:
            if isinstance(ecu, dict):
                if not str(ecu.get("asil", "")).strip():
                    ecu["asil"] = default_asil
                if not self._has_hardware_binding(ecu):
                    ecu["processor"] = default_processor

        services = spec["services"] if isinstance(spec.get("services"), list) else []
        signals = spec["signals"] if isinstance(spec.get("signals"), list) else []

        has_someip = any("some/ip" in str(item).lower() for item in services)
        has_can = any("can" in str(item).lower() for item in signals)

        if not has_someip:
            baseline_services = baseline.get("services", [])
            if isinstance(baseline_services, list):
                services.extend([item for item in baseline_services if isinstance(item, dict)])

        if not has_can:
            baseline_signals = baseline.get("signals", [])
            if isinstance(baseline_signals, list):
                signals.extend([item for item in baseline_signals if isinstance(item, dict)])

        if not spec.get("safety"):
            baseline_safety = baseline.get("safety", [])
            if isinstance(baseline_safety, list):
                spec["safety"] = [item for item in baseline_safety if isinstance(item, dict)]

        spec["ecus"] = ecus
        spec["services"] = services
        spec["signals"] = signals

        return yaml.safe_dump(spec, sort_keys=False, allow_unicode=True)

    def compile_to_arxml(self, yaml_text: str, use_mapping_precheck: bool = True) -> str:
        data = self._parse_yaml_spec(yaml_text)

        data.setdefault("system", {})
        data.setdefault("ecus", [])
        data.setdefault("services", [])
        data.setdefault("signals", [])
        data.setdefault("safety", [])

        if use_mapping_precheck:
            mapping_assessment = self._assess_mapping_features(data)
            high_risk = [
                item
                for item in mapping_assessment
                if item.get("prediction") == 0 and (item.get("confidence") or 0.0) >= 0.65
            ]
            if high_risk:
                sample = ", ".join(
                    f"{item.get('signal_name', 'signal')}->{item.get('service_name', 'service')}"
                    for item in high_risk[:3]
                )
                raise ValueError(
                    "ML mapping pre-check blocked compile for "
                    f"{len(high_risk)} high-risk mapping(s): {sample}. "
                    "Adjust mappings or run safety review before compiling."
                )

        template = self.template_env.get_template("arxml_template.xml.j2")
        return template.render(spec=data)

    def _append_finding(self, report: Dict[str, object], severity: str, message: str):
        findings = report.get("findings", [])
        findings.append({"severity": severity, "message": message})
        report["findings"] = findings

    def _collect_names(self, items: List[dict], keys: List[str]) -> List[str]:
        names = []
        for item in items:
            if not isinstance(item, dict):
                continue
            for key in keys:
                value = str(item.get(key, "")).strip()
                if value:
                    names.append(value)
                    break
        return names

    def _has_hardware_binding(self, ecu: dict) -> bool:
        if not isinstance(ecu, dict):
            return False
        alias_keys = ["processor", "hardware", "platform", "cpu", "soc", "node"]
        return any(str(ecu.get(key, "")).strip() for key in alias_keys)

    @staticmethod
    def _to_float(value, default: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _load_mapping_baseline_model(self):
        if self.mapping_baseline_model is not None:
            return self.mapping_baseline_model
        if self.mapping_baseline_error is not None:
            return None
        if not MAPPING_BASELINE_MODEL_PATH.exists():
            self.mapping_baseline_error = f"Model not found at {MAPPING_BASELINE_MODEL_PATH}"
            return None

        try:
            with MAPPING_BASELINE_MODEL_PATH.open("rb") as f:
                self.mapping_baseline_model = pickle.load(f)
            self.mapping_baseline_error = None
        except Exception as exc:
            self.mapping_baseline_model = None
            self.mapping_baseline_error = str(exc)
        return self.mapping_baseline_model

    def _build_mapping_feature_rows(self, spec: Dict[str, object]) -> List[Dict[str, object]]:
        services = spec.get("services", []) if isinstance(spec.get("services", []), list) else []
        signals = spec.get("signals", []) if isinstance(spec.get("signals", []), list) else []
        ecus = spec.get("ecus", []) if isinstance(spec.get("ecus", []), list) else []

        source_ecu_default = "ECU_SourceDomain"
        target_ecu_default = "ECU_CentralCompute"
        asil_default = "B"

        ecu_names = []
        for ecu in ecus:
            if not isinstance(ecu, dict):
                continue
            name = str(ecu.get("name", "")).strip()
            if name:
                ecu_names.append(name)
            asil = str(ecu.get("asil", "")).strip()
            if asil:
                asil_default = asil

        if ecu_names:
            source_ecu_default = ecu_names[0]
            target_ecu_default = ecu_names[1] if len(ecu_names) > 1 else ecu_names[0]

        rows: List[Dict[str, object]] = []
        for signal in signals:
            if not isinstance(signal, dict):
                continue

            signal_name = (
                str(signal.get("name", "")).strip()
                or str(signal.get("signal", "")).strip()
                or str(signal.get("short_name", "")).strip()
                or str(signal.get("source", "")).strip()
                or "UnknownSignal"
            )
            target_service_name = (
                str(signal.get("service_name", "")).strip()
                or str(signal.get("service", "")).strip()
                or str(signal.get("target_service", "")).strip()
            )

            selected_services = []
            if target_service_name:
                for service in services:
                    if not isinstance(service, dict):
                        continue
                    service_name = (
                        str(service.get("name", "")).strip()
                        or str(service.get("service", "")).strip()
                        or str(service.get("short_name", "")).strip()
                    )
                    if service_name and service_name == target_service_name:
                        selected_services.append(service)

            if not selected_services and services and isinstance(services[0], dict):
                selected_services = [services[0]]
            if not selected_services:
                selected_services = [{}]

            for service in selected_services:
                service_name = (
                    str(service.get("name", "")).strip()
                    or str(service.get("service", "")).strip()
                    or str(service.get("short_name", "")).strip()
                    or target_service_name
                    or "UnknownService"
                )
                source_protocol = (
                    str(signal.get("source_protocol", "")).strip()
                    or str(signal.get("source", "")).strip()
                    or "CAN"
                )
                target_protocol = (
                    str(service.get("protocol", "")).strip()
                    or str(signal.get("target_protocol", "")).strip()
                    or str(signal.get("destination", "")).strip()
                    or "SOME/IP"
                )
                data_type = (
                    str(signal.get("data_type", "")).strip()
                    or str(service.get("data_type", "")).strip()
                    or "float32"
                )
                cycle_time_ms = self._to_float(signal.get("cycle_time_ms"), 10.0)
                asil = (
                    str(signal.get("asil", "")).strip()
                    or str(service.get("asil", "")).strip()
                    or asil_default
                )
                source_ecu = (
                    str(signal.get("source_ecu", "")).strip()
                    or str(signal.get("ecu", "")).strip()
                    or source_ecu_default
                )
                target_ecu = (
                    str(signal.get("target_ecu", "")).strip()
                    or str(service.get("target_ecu", "")).strip()
                    or target_ecu_default
                )
                expected_latency_ms = self._to_float(
                    signal.get("expected_latency_ms") or service.get("expected_latency_ms"),
                    30.0,
                )

                rows.append(
                    {
                        "signal_name": signal_name,
                        "service_name": service_name,
                        "source_protocol": source_protocol,
                        "target_protocol": target_protocol,
                        "data_type": data_type,
                        "cycle_time_ms": cycle_time_ms,
                        "asil": asil,
                        "source_ecu": source_ecu,
                        "target_ecu": target_ecu,
                        "expected_latency_ms": expected_latency_ms,
                    }
                )
        return rows

    def _assess_mapping_features(self, spec: Dict[str, object]) -> List[Dict[str, object]]:
        model = self._load_mapping_baseline_model()
        if model is None:
            self.last_mapping_assessment = []
            return []

        feature_rows = self._build_mapping_feature_rows(spec)
        if not feature_rows:
            self.last_mapping_assessment = []
            return []

        try:
            predictions = model.predict(feature_rows)
            probabilities = model.predict_proba(feature_rows) if hasattr(model, "predict_proba") else None
        except Exception as exc:
            self.mapping_baseline_error = str(exc)
            self.last_mapping_assessment = []
            return []

        assessments: List[Dict[str, object]] = []
        for index, row in enumerate(feature_rows):
            prediction = int(predictions[index])
            confidence = None
            if probabilities is not None:
                confidence = float(probabilities[index][prediction])
            assessments.append(
                {
                    "signal_name": row.get("signal_name", ""),
                    "service_name": row.get("service_name", ""),
                    "prediction": prediction,
                    "label": "valid_mapping" if prediction == 1 else "invalid_or_unsafe_mapping",
                    "confidence": confidence,
                }
            )

        self.last_mapping_assessment = assessments
        return assessments

    def safety_check_report(self, yaml_text: str, use_mapping_check: bool = True) -> Dict[str, object]:
        report: Dict[str, object] = {
            "score": 100,
            "summary": "Safety checks passed",
            "findings": [],
            "counts": {"critical": 0, "warning": 0, "info": 0},
        }

        def add_finding(severity: str, message: str):
            self._append_finding(report, severity, message)

        try:
            spec = yaml.safe_load(yaml_text) or {}
        except yaml.YAMLError as exc:
            add_finding("critical", f"YAML parse error: {exc}")
            report["summary"] = "Validation failed"
            report["score"] = 0
            report["counts"] = {"critical": 1, "warning": 0, "info": 0}
            return report

        if not isinstance(spec, dict):
            add_finding("critical", "Generated YAML is not a top-level mapping.")
            report["summary"] = "Validation failed"
            report["score"] = 0
            report["counts"] = {"critical": 1, "warning": 0, "info": 0}
            return report

        system = spec.get("system", {})
        ecus = spec.get("ecus", []) if isinstance(spec.get("ecus", []), list) else []
        services = spec.get("services", []) if isinstance(spec.get("services", []), list) else []
        signals = spec.get("signals", []) if isinstance(spec.get("signals", []), list) else []
        safety_items = spec.get("safety", [])

        if isinstance(system, dict) and str(system.get("name", "")).strip():
            add_finding("info", "System name is present: OK for traceability.")
        else:
            add_finding("critical", "System name is missing; add a unique system.name so the architecture can be traced.")

        if not ecus:
            add_finding("critical", "No ECU definitions found; add at least one ECU for deployment and safety allocation.")
        else:
            add_finding("info", f"Found {len(ecus)} ECU definition(s).")

        valid_asil_values = {"qm", "a", "b", "c", "d", "asil-a", "asil-b", "asil-c", "asil-d"}
        missing_asil = 0
        missing_hw = 0
        invalid_asil = 0
        for index, ecu in enumerate(ecus, start=1):
            if not isinstance(ecu, dict):
                add_finding("warning", f"ECU #{index} is not a mapping object.")
                continue
            asil = str(ecu.get("asil", "")).strip().lower()
            if not asil:
                missing_asil += 1
            elif asil not in valid_asil_values:
                invalid_asil += 1
            if not self._has_hardware_binding(ecu):
                missing_hw += 1

        if missing_asil:
            add_finding("warning", f"{missing_asil} ECU(s) are missing ASIL; add qm/A/B/C/D so risk level is explicit.")
        else:
            add_finding("info", "All ECUs include ASIL assignments.")

        if invalid_asil:
            add_finding("warning", f"{invalid_asil} ECU(s) use non-standard ASIL values; use qm or A/B/C/D format.")

        if missing_hw:
            add_finding("warning", f"{missing_hw} ECU(s) have no hardware binding; add processor/platform so runtime target is clear.")
        else:
            add_finding("info", "All ECUs include hardware binding details.")

        if not services:
            add_finding("critical", "No service descriptions generated; add services so Adaptive communication can be validated.")
        else:
            add_finding("info", f"Found {len(services)} service definition(s).")

        if not signals:
            add_finding("critical", "No signal mappings generated; add signals so Classic-to-Adaptive flow is testable.")
        else:
            add_finding("info", f"Found {len(signals)} signal mapping(s).")

        someip_detected = any("some/ip" in str(item).lower() for item in services)
        can_detected = any("can" in str(item).lower() for item in signals)
        if someip_detected:
            add_finding("info", "SOME/IP protocol detected in services: OK for Adaptive interface demo.")
        else:
            add_finding("warning", "No SOME/IP protocol detected in services; add protocol: SOME/IP or apply demo baseline.")
        if can_detected:
            add_finding("info", "CAN signal mapping detected: OK for Classic source path.")
        else:
            add_finding("warning", "No CAN signal mapping detected; add source: CAN signal entry or apply demo baseline.")

        service_names = self._collect_names(services, ["name", "service", "short_name"])
        signal_names = self._collect_names(signals, ["name", "signal", "short_name"])
        duplicate_services = len(service_names) - len(set(service_names))
        duplicate_signals = len(signal_names) - len(set(signal_names))
        if duplicate_services > 0:
            add_finding("warning", f"Detected {duplicate_services} duplicate service name(s); rename duplicates to avoid routing ambiguity.")
        if duplicate_signals > 0:
            add_finding("warning", f"Detected {duplicate_signals} duplicate signal name(s); rename duplicates for deterministic mapping.")

        if not safety_items:
            add_finding("warning", "No safety checklist entries found; add safety checks (watchdog/monitor/fallback) for review readiness.")
        else:
            add_finding("info", f"Found {len(safety_items)} safety checklist item(s).")

        if use_mapping_check:
            mapping_assessment = self._assess_mapping_features(spec)
            if mapping_assessment:
                invalid_predictions = [item for item in mapping_assessment if item.get("prediction") == 0]
                low_confidence = [
                    item
                    for item in mapping_assessment
                    if item.get("confidence") is not None and float(item.get("confidence", 0.0)) < 0.6
                ]

                if invalid_predictions:
                    add_finding(
                        "warning",
                        f"ML mapping check flagged {len(invalid_predictions)} potential invalid mapping(s); review signal-to-service alignment.",
                    )
                else:
                    add_finding("info", "ML mapping check did not detect invalid mappings.")

                if low_confidence:
                    add_finding(
                        "warning",
                        f"ML mapping check found {len(low_confidence)} low-confidence mapping(s); manual review recommended.",
                    )

        yaml_lower = yaml_text.lower()
        if re.search(r"\bmissing\b|\bundefined\b|\bunsupported\b", yaml_lower):
            add_finding("warning", "Design text contains unresolved terms (missing/undefined/unsupported).")
        if re.search(r"\bdeadlock\b|\bdata loss\b|\bsafety violation\b", yaml_lower):
            add_finding("warning", "Potential safety hazard keywords were detected; review required.")

        findings = report.get("findings", [])
        critical_count = sum(1 for item in findings if item.get("severity") == "critical")
        warning_count = sum(1 for item in findings if item.get("severity") == "warning")
        info_count = sum(1 for item in findings if item.get("severity") == "info")
        report["counts"] = {
            "critical": critical_count,
            "warning": warning_count,
            "info": info_count,
        }

        score = 100 - (critical_count * 30) - (warning_count * 8)
        report["score"] = max(0, score)
        if critical_count > 0:
            report["summary"] = "Validation failed: critical findings present"
        elif warning_count > 0:
            report["summary"] = "Validation passed with warnings"
        else:
            report["summary"] = "Validation passed"

        return report

    def safety_check(self, yaml_text: str, use_mapping_check: bool = True) -> List[str]:
        report = self.safety_check_report(yaml_text, use_mapping_check=use_mapping_check)
        findings = report.get("findings", [])
        return [
            f"[{item.get('severity', 'info').upper()}] {item.get('message', '')}"
            for item in findings
            if item.get("severity") in {"critical", "warning"}
        ]
