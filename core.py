import re
import shutil
import subprocess
import sys
import os
import hashlib
import threading
import json
import pickle
import time
import urllib.error
import urllib.request
import urllib.parse
import copy
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
MAPPING_BASELINE_METRICS_PATH = BASE_DIR / "models" / "gnn_mapping_baseline_metrics.json"
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


def classify_arxml_platform(xml_text: str) -> Dict[str, object]:
    payload = (xml_text or "").strip()
    if not payload:
        return {
            "classification": "invalid",
            "classic_score": 0,
            "adaptive_score": 0,
            "reason": "empty content",
        }

    try:
        ET.fromstring(payload)
    except ET.ParseError as exc:
        return {
            "classification": "invalid",
            "classic_score": 0,
            "adaptive_score": 0,
            "reason": f"invalid xml: {exc}",
        }

    text_lower = payload.lower()
    classic_markers = [
        "<ecuc-",
        "canif",
        "cantp",
        "linif",
        "dcm",
        "dem",
        "ecum",
        "watchdog",
        "rte",
        "autosar.org/schema/r4",
    ]
    adaptive_markers = [
        "ara::",
        "adaptive",
        "machine-design",
        "execution-manifest",
        "service-instance",
        "someipservice",
        "persistency",
        "phm",
        "state-management",
    ]

    classic_hits = [marker for marker in classic_markers if marker in text_lower]
    adaptive_hits = [marker for marker in adaptive_markers if marker in text_lower]
    classic_score = len(classic_hits)
    adaptive_score = len(adaptive_hits)

    if adaptive_score >= 2 and adaptive_score > classic_score:
        return {
            "classification": "adaptive",
            "classic_score": classic_score,
            "adaptive_score": adaptive_score,
            "reason": "adaptive markers detected: " + ", ".join(adaptive_hits[:4]),
        }

    if classic_score >= 1 and classic_score >= adaptive_score:
        return {
            "classification": "classic",
            "classic_score": classic_score,
            "adaptive_score": adaptive_score,
            "reason": "classic markers detected: " + ", ".join(classic_hits[:4]),
        }

    return {
        "classification": "unknown",
        "classic_score": classic_score,
        "adaptive_score": adaptive_score,
        "reason": "insufficient known classic/adaptive markers",
    }


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


def _query_tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9_]+", (text or "").lower()))


def _lexical_overlap_score(query: str, text: str) -> float:
    q_tokens = _query_tokens(query)
    if not q_tokens:
        return 0.0
    t_tokens = _query_tokens(text)
    if not t_tokens:
        return 0.0
    overlap = len(q_tokens.intersection(t_tokens))
    return overlap / max(1.0, float(len(q_tokens)))


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
            result = vector_store.query(
                query_texts=[query],
                n_results=max(self.top_k * 5, self.top_k),
                include=["documents", "distances", "metadatas"],
            )
            documents = result.get("documents", [[]])[0]
            distances = result.get("distances", [[]])[0]

            ranked: List[tuple[float, str]] = []
            for idx, doc in enumerate(documents):
                if not doc:
                    continue
                lexical = _lexical_overlap_score(query, doc)
                distance = float(distances[idx]) if idx < len(distances) and distances[idx] is not None else 1.0
                semantic = 1.0 / (1.0 + max(0.0, distance))
                score = (0.65 * semantic) + (0.35 * lexical)
                ranked.append((score, doc))

            ranked.sort(key=lambda item: item[0], reverse=True)
            selected = [doc for score, doc in ranked if score > 0.05][: self.top_k]
            self.last_retrieved = selected
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

        grounding_names = set()
        for doc in retrieved_docs or []:
            # Prefer explicit AUTOSAR element names from parsed references.
            for line in (doc or "").splitlines():
                match = re.match(r"^###\s+[A-Z0-9_-]+:\s+([A-Za-z0-9_\-]+)", line.strip())
                if match:
                    grounding_names.add(match.group(1))
                    continue
                match = re.match(r"^-\s+([A-Za-z0-9_\-]+):\s+", line.strip())
                if match:
                    grounding_names.add(match.group(1))

        grounding_preview = ", ".join(sorted(grounding_names)[:20]) if grounding_names else "(none)"

        prompt = [
            "You are an AUTOSAR automation assistant.",
            "Use the reference material below to generate a simplified AUTOSAR architecture in YAML.",
            "Return only the YAML content. Do not add any explanation or markdown fences.",
            "Focus on service mapping, signal routing, ECU load distribution, and ASIL-aware safeguards.",
            "If the request mentions Classic CAN and Adaptive SOME/IP, create a bridge section describing the service transformation.",
            "Use the following YAML structure: system, ecus, services, signals, safety.",
            "Do not leave sections empty. Always provide meaningful entries.",
            "Minimum structure requirements: ecus >= 2, services >= 1, signals >= 1, safety >= 1.",
            "For names, use concise PascalCase or SCREAMING_SNAKE_CASE identifiers without spaces.",
            "Grounding rules: reuse names/signals/services from the references when available.",
            "Do not invent random identifiers if a relevant reference identifier exists.",
            f"Reference identifiers (prefer these): {grounding_preview}",
            "Reference material:",
        ]
        prompt.extend(compact_references or ["(no reference material available)"])
        prompt.append("User request:")
        prompt.append(user_request.strip())
        prompt.append(
            "Produce YAML with these sections: system, ecus, services, signals, safety. Keep names short and valid."
        )
        return "\n\n".join(prompt)

    def _is_sparse_spec(self, yaml_text: str) -> bool:
        try:
            spec = self._parse_yaml_spec(yaml_text)
        except Exception:
            return True

        ecus = spec.get("ecus") if isinstance(spec.get("ecus"), list) else []
        services = spec.get("services") if isinstance(spec.get("services"), list) else []
        signals = spec.get("signals") if isinstance(spec.get("signals"), list) else []
        safety = spec.get("safety") if isinstance(spec.get("safety"), list) else []

        if len(ecus) < 2:
            return True
        if len(services) < 1:
            return True
        if len(signals) < 1:
            return True
        if len(safety) < 1:
            return True
        return False

    def _build_expand_prompt(self, user_request: str, current_yaml: str) -> str:
        return "\n\n".join(
            [
                "You produced AUTOSAR YAML that is too sparse.",
                "Expand and improve it while keeping it consistent with the user request.",
                "Return only YAML. No markdown fences or commentary.",
                "Hard requirements:",
                "- Keep sections: system, ecus, services, signals, safety.",
                "- Ensure ecus >= 2, services >= 1, signals >= 1, safety >= 1.",
                "- Fill all important fields with practical non-empty values.",
                "- Keep safety checks concrete (ASIL, watchdog, plausibility, timeout, etc.).",
                "User request:",
                user_request.strip(),
                "Current YAML to improve:",
                current_yaml.strip(),
            ]
        )

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

        base_model = (self.model_name or "gemini-2.0-flash").strip()
        if base_model.startswith("models/"):
            base_model = base_model.split("/", 1)[1]

        models_to_try = [base_model]
        if base_model == "gemini-2.5-flash":
            models_to_try.extend(["gemini-2.0-flash", "gemini-2.0-flash-lite"])
        elif base_model == "gemini-2.5-pro":
            models_to_try.extend(["gemini-2.5-flash", "gemini-2.0-flash"])

        seen_models = set()
        ordered_models = []
        for candidate in models_to_try:
            if candidate in seen_models:
                continue
            seen_models.add(candidate)
            ordered_models.append(candidate)

        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.0,
                "topP": 0.9,
                "maxOutputTokens": 2048,
            },
        }

        transient_errors: List[str] = []

        for model in ordered_models:
            encoded_model = urllib.parse.quote(model, safe="")
            endpoint = (
                f"https://generativelanguage.googleapis.com/v1beta/models/{encoded_model}:generateContent"
                f"?key={urllib.parse.quote(api_key, safe='')}"
            )

            for attempt in range(3):
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

                    candidates = data.get("candidates") or []
                    if not candidates:
                        raise RuntimeError("Gemini returned no candidates.")

                    parts = candidates[0].get("content", {}).get("parts", [])
                    text_response = "\n".join(
                        str(part.get("text", "")) for part in parts if part.get("text")
                    ).strip()
                    if not text_response:
                        raise RuntimeError("Gemini returned an empty response.")
                    return text_response

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

                    if exc.code in (429, 503):
                        transient_errors.append(f"{model} attempt {attempt + 1}: {details}")
                        if attempt < 2:
                            time.sleep(0.8 * (2 ** attempt))
                            continue
                        break

                    raise RuntimeError(f"Gemini request failed: {details}") from exc

                except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                    transient_errors.append(f"{model} attempt {attempt + 1}: {exc}")
                    if attempt < 2:
                        time.sleep(0.8 * (2 ** attempt))
                        continue
                    break

        if transient_errors:
            tried = ", ".join(ordered_models)
            raise RuntimeError(
                "Gemini is temporarily unavailable due to high demand. "
                f"Tried models: {tried}. Last error: {transient_errors[-1]}"
            )

        raise RuntimeError("Gemini request failed for unknown reasons.")

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
        yaml_payload = self._extract_yaml_payload(self._call_model(prompt))

        # Gemini can be overly conservative and return sparse structures.
        # Retry once with a strict expansion prompt to align output richness.
        if self.model_provider == "gemini" and self._is_sparse_spec(yaml_payload):
            expand_prompt = self._build_expand_prompt(user_request, yaml_payload)
            self.last_prompt = f"{prompt}\n\n# Expansion pass\n\n{expand_prompt}"
            yaml_payload = self._extract_yaml_payload(self._call_model(expand_prompt))

        return yaml_payload

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

    def _parse_yaml_spec_lenient(self, yaml_text: str) -> Dict[str, object]:
        payload = self._extract_yaml_payload(yaml_text).replace("\t", "  ").replace("\xa0", " ")
        lines = payload.splitlines()

        spec: Dict[str, object] = {
            "system": {},
            "ecus": [],
            "services": [],
            "signals": [],
            "safety": [],
        }

        section: Optional[str] = None
        current_item: Optional[object] = None

        for raw_line in lines:
            line = raw_line.rstrip()
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue

            section_match = re.match(r"^(system|ecus|services|signals|safety)\s*:\s*$", stripped, flags=re.IGNORECASE)
            if section_match:
                section = section_match.group(1).lower()
                current_item = None
                continue

            if section is None:
                continue

            if section == "system":
                if stripped.startswith("-"):
                    continue
                if ":" in stripped:
                    key, value = stripped.split(":", 1)
                    key = key.strip()
                    value = value.strip().strip('"\'')
                    if key:
                        spec["system"][key] = value
                continue

            # List-like sections.
            section_items = spec[section]
            if not isinstance(section_items, list):
                continue

            if stripped.startswith("-"):
                entry = stripped[1:].strip()
                if not entry:
                    current_item = {}
                    section_items.append(current_item)
                    continue

                if ":" in entry:
                    key, value = entry.split(":", 1)
                    item_dict = {key.strip(): value.strip().strip('"\'')}
                    section_items.append(item_dict)
                    current_item = item_dict
                else:
                    scalar_value = entry.strip('"\'')
                    section_items.append(scalar_value)
                    current_item = scalar_value
                continue

            if ":" in stripped and isinstance(current_item, dict):
                key, value = stripped.split(":", 1)
                key = key.strip()
                value = value.strip().strip('"\'')
                if key:
                    current_item[key] = value

        return spec

    def _parse_yaml_spec(self, yaml_text: str) -> Dict[str, object]:
        payload = self._extract_yaml_payload(yaml_text)
        try:
            parsed = yaml.safe_load(payload)
        except yaml.YAMLError as exc:
            recovered = self._parse_yaml_spec_lenient(payload)
            if isinstance(recovered, dict):
                has_content = bool(recovered.get("system")) or any(
                    bool(recovered.get(section)) for section in ["ecus", "services", "signals", "safety"]
                )
                if has_content:
                    return recovered
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

        if not ecus:
            profile_token = (profile or "default").strip().lower()
            prefix = "Default" if profile_token == "default" else re.sub(r"[^a-zA-Z0-9]+", "", profile_token).title()
            ecus = [
                {
                    "name": f"{prefix}CanIngressECU",
                    "asil": default_asil,
                    "processor": default_processor,
                },
                {
                    "name": f"{prefix}SomeIpGatewayECU",
                    "asil": default_asil,
                    "processor": default_processor,
                },
            ]

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
        outputs = self.compile_to_arxml_pair(
            yaml_text,
            use_mapping_precheck=use_mapping_precheck,
        )
        return outputs["adaptive"]

    def _build_adaptive_spec(self, data: Dict[str, object]) -> Dict[str, object]:
        spec = copy.deepcopy(data)
        system = spec.get("system") if isinstance(spec.get("system"), dict) else {}
        if isinstance(system, dict):
            name = str(system.get("name", "autosar-system")).strip() or "autosar-system"
            if not name.upper().endswith("_ADAPTIVE"):
                system["name"] = f"{name}_ADAPTIVE"
            spec["system"] = system
        return spec

    def _build_classic_spec(self, data: Dict[str, object]) -> Dict[str, object]:
        spec = copy.deepcopy(data)

        system = spec.get("system") if isinstance(spec.get("system"), dict) else {}
        if isinstance(system, dict):
            name = str(system.get("name", "autosar-system")).strip() or "autosar-system"
            if not name.upper().endswith("_CLASSIC"):
                system["name"] = f"{name}_CLASSIC"
            spec["system"] = system

        services = spec.get("services") if isinstance(spec.get("services"), list) else []
        for item in services:
            if not isinstance(item, dict):
                continue
            item["protocol"] = "CAN"
            route = str(item.get("route", "")).strip()
            item["route"] = route or "/classic/can/bus"

        signals = spec.get("signals") if isinstance(spec.get("signals"), list) else []
        for item in signals:
            if not isinstance(item, dict):
                continue
            item["format"] = "CAN"
            source = str(item.get("source", "")).strip()
            destination = str(item.get("destination", "")).strip()
            source_lower = source.lower()
            destination_lower = destination.lower()

            if not source:
                item["source"] = "ClassicCANBus"
            if not destination:
                item["destination"] = "ClassicCANBus"
            if any(token in source_lower for token in ["some/ip", "someip", "ethernet", "adaptive"]):
                item["source"] = "ClassicCANBus"
            if any(token in destination_lower for token in ["some/ip", "someip", "ethernet", "adaptive"]):
                item["destination"] = "ClassicCANBus"

        spec["services"] = services
        spec["signals"] = signals
        return spec

    def _render_arxml(self, spec: Dict[str, object]) -> str:
        template = self.template_env.get_template("arxml_template.xml.j2")
        raw_xml = template.render(spec=spec)
        return self._format_arxml_output(raw_xml)

    def compile_to_arxml_pair(self, yaml_text: str, use_mapping_precheck: bool = True) -> Dict[str, str]:
        data = self._parse_yaml_spec(yaml_text)
        data = self._normalize_spec_for_compile(data)

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

        adaptive_spec = self._build_adaptive_spec(data)
        classic_spec = self._build_classic_spec(data)
        return {
            "adaptive": self._render_arxml(adaptive_spec),
            "classic": self._render_arxml(classic_spec),
        }

    @staticmethod
    def _format_arxml_output(xml_text: str) -> str:
        """Produce stable, readable XML output with deterministic indentation."""
        try:
            root = ET.fromstring(xml_text)
            tree = ET.ElementTree(root)
            ET.indent(tree, space="  ")
            return ET.tostring(root, encoding="unicode") if xml_text.strip().startswith("<AUTOSAR") else (
                '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode")
            )
        except Exception:
            # Keep original output if formatting fails for any reason.
            return xml_text.strip()

    def generate_ai_suggestions(
        self,
        yaml_text: str,
        use_mapping_check: bool = True,
        max_items: int = 5,
    ) -> List[Dict[str, object]]:
        try:
            spec = self._parse_yaml_spec(yaml_text)
        except Exception:
            return [
                {
                    "id": "YAML_PARSE_FIX",
                    "title": "Repair YAML structure",
                    "category": "safety_completeness",
                    "rationale": "YAML could not be parsed reliably.",
                    "patch_instruction": "Regenerate YAML and ensure top-level keys: system, ecus, services, signals, safety.",
                    "confidence": 0.95,
                }
            ]

        report = self.safety_check_report(yaml_text, use_mapping_check=use_mapping_check)
        findings = report.get("findings", []) if isinstance(report, dict) else []

        ecus = spec.get("ecus", []) if isinstance(spec.get("ecus", []), list) else []
        services = spec.get("services", []) if isinstance(spec.get("services", []), list) else []
        signals = spec.get("signals", []) if isinstance(spec.get("signals", []), list) else []
        safety = spec.get("safety", []) if isinstance(spec.get("safety", []), list) else []

        suggestions: List[Dict[str, object]] = []

        def add_suggestion(
            sid: str,
            title: str,
            category: str,
            rationale: str,
            patch_instruction: str,
            confidence: float,
        ):
            suggestions.append(
                {
                    "id": sid,
                    "title": title,
                    "category": category,
                    "rationale": rationale,
                    "patch_instruction": patch_instruction,
                    "confidence": round(float(confidence), 2),
                }
            )

        if len(ecus) < 2:
            add_suggestion(
                "DEPLOY_ECU_COUNT_LOW",
                "Increase ECU deployment coverage",
                "deployment",
                "Current architecture has low ECU coverage for realistic partitioning.",
                "Add at least two ECUs and split source/control responsibilities.",
                0.88,
            )

        if not any(str(item.get("protocol", "")).strip().lower() == "some/ip" for item in services if isinstance(item, dict)):
            add_suggestion(
                "SVC_ADD_SOMEIP",
                "Add SOME/IP service binding",
                "services",
                "Adaptive communication is incomplete without SOME/IP service definitions.",
                "Add a SOME/IP service with name, route, and protocol fields.",
                0.91,
            )

        if not any("can" in str(item.get("format", "")).lower() for item in signals if isinstance(item, dict)):
            add_suggestion(
                "SIG_ADD_CAN_MAPPING",
                "Add CAN signal mapping",
                "signals",
                "Classic-to-Adaptive path needs at least one CAN-origin signal mapping.",
                "Add signal entries with source, destination, and format: CAN.",
                0.89,
            )

        missing_asil = sum(
            1
            for ecu in ecus
            if isinstance(ecu, dict) and not str(ecu.get("asil", "")).strip()
        )
        if missing_asil > 0:
            add_suggestion(
                "ECU_ASIL_FILL",
                "Complete ECU ASIL assignments",
                "safety_completeness",
                f"{missing_asil} ECU(s) are missing ASIL values.",
                "Set asil on each ECU (QM or A/B/C/D as appropriate).",
                0.9,
            )

        missing_hw = sum(
            1
            for ecu in ecus
            if isinstance(ecu, dict) and not self._has_hardware_binding(ecu)
        )
        if missing_hw > 0:
            add_suggestion(
                "ECU_HW_BINDING_FILL",
                "Add ECU hardware bindings",
                "deployment",
                f"{missing_hw} ECU(s) do not specify processor/platform bindings.",
                "Set processor/platform for each ECU to improve deployment clarity.",
                0.86,
            )

        if not safety:
            add_suggestion(
                "SAFETY_CHECKLIST_ADD",
                "Add safety checklist items",
                "safety_completeness",
                "Safety checklist is empty; validation confidence is reduced.",
                "Add checks such as asil_b_monitoring, watchdog_supervision, and signal_plausibility_check.",
                0.93,
            )

        if not suggestions:
            add_suggestion(
                "QUALITY_TUNE_ROUTING",
                "Refine service-to-signal routing",
                "provider_bindings",
                "No critical gaps found; quality can still improve with clearer routing semantics.",
                "Align signal names with service intent and ensure route/path fields are explicit.",
                0.72,
            )

        # Promote issues explicitly detected in current findings.
        joined_findings = " ".join(str(item.get("message", "")) for item in findings).lower()
        if "low-confidence mapping" in joined_findings:
            add_suggestion(
                "MAP_LOW_CONFIDENCE_REVIEW",
                "Review low-confidence mappings",
                "provider_bindings",
                "ML mapping check reported low-confidence mappings.",
                "Review signal-to-service pairs and enforce explicit target service_name fields.",
                0.84,
            )

        return suggestions[: max(1, int(max_items))]

    def apply_selected_suggestions(self, yaml_text: str, selected_ids: List[str]) -> str:
        spec = self._parse_yaml_spec(yaml_text)
        selected = {str(item).strip() for item in (selected_ids or []) if str(item).strip()}
        if not selected:
            return yaml.safe_dump(spec, sort_keys=False, allow_unicode=True)

        updated = copy.deepcopy(spec)
        updated.setdefault("system", {})
        updated.setdefault("ecus", [])
        updated.setdefault("services", [])
        updated.setdefault("signals", [])
        updated.setdefault("safety", [])

        ecus = updated["ecus"] if isinstance(updated.get("ecus"), list) else []
        services = updated["services"] if isinstance(updated.get("services"), list) else []
        signals = updated["signals"] if isinstance(updated.get("signals"), list) else []
        safety = updated["safety"] if isinstance(updated.get("safety"), list) else []

        if "DEPLOY_ECU_COUNT_LOW" in selected:
            while len(ecus) < 2:
                idx = len(ecus) + 1
                ecus.append({"name": f"ECU_{idx}", "asil": "B", "processor": "Automotive_MCU"})

        if "ECU_ASIL_FILL" in selected:
            for ecu in ecus:
                if isinstance(ecu, dict) and not str(ecu.get("asil", "")).strip():
                    ecu["asil"] = "B"

        if "ECU_HW_BINDING_FILL" in selected:
            for ecu in ecus:
                if isinstance(ecu, dict) and not self._has_hardware_binding(ecu):
                    ecu["processor"] = "Automotive_MCU"

        if "SVC_ADD_SOMEIP" in selected:
            if not any(isinstance(item, dict) and str(item.get("protocol", "")).strip().lower() == "some/ip" for item in services):
                services.append({"name": "VehicleSomeIpService", "route": "/vehicle/service", "protocol": "SOME/IP"})

        if "SIG_ADD_CAN_MAPPING" in selected:
            if not any(isinstance(item, dict) and "can" in str(item.get("format", "")).lower() for item in signals):
                source_name = "ECU_1"
                destination_name = "ECU_2"
                if ecus and isinstance(ecus[0], dict):
                    source_name = str(ecus[0].get("name", source_name))
                if len(ecus) > 1 and isinstance(ecus[1], dict):
                    destination_name = str(ecus[1].get("name", destination_name))
                signals.append(
                    {
                        "name": "VehicleSpeedSignal",
                        "source": source_name,
                        "destination": destination_name,
                        "format": "CAN",
                    }
                )

        if "SAFETY_CHECKLIST_ADD" in selected:
            existing = {str(item).strip().lower() for item in safety if isinstance(item, str)}
            for check in ["asil_b_monitoring", "watchdog_supervision", "signal_plausibility_check"]:
                if check.lower() not in existing:
                    safety.append(check)

        updated["ecus"] = ecus
        updated["services"] = services
        updated["signals"] = signals
        updated["safety"] = safety
        return yaml.safe_dump(updated, sort_keys=False, allow_unicode=True)

    @staticmethod
    def summarize_safety_improvement(previous: Dict[str, object], current: Dict[str, object]) -> Dict[str, object]:
        prev_score = int(previous.get("score", 0) or 0)
        curr_score = int(current.get("score", 0) or 0)
        delta = curr_score - prev_score

        prev_counts = previous.get("counts", {}) if isinstance(previous.get("counts", {}), dict) else {}
        curr_counts = current.get("counts", {}) if isinstance(current.get("counts", {}), dict) else {}

        improved_items: List[str] = []
        if int(curr_counts.get("critical", 0) or 0) < int(prev_counts.get("critical", 0) or 0):
            improved_items.append("critical findings reduced")
        if int(curr_counts.get("warning", 0) or 0) < int(prev_counts.get("warning", 0) or 0):
            improved_items.append("warning findings reduced")
        if delta > 0:
            improved_items.append("overall safety score increased")

        remaining = [
            item
            for item in (current.get("findings", []) if isinstance(current.get("findings", []), list) else [])
            if item.get("severity") in {"critical", "warning"}
        ]

        return {
            "previous_score": prev_score,
            "new_score": curr_score,
            "delta": delta,
            "improved": improved_items,
            "remaining": remaining,
        }

    @staticmethod
    def _pick_first_non_empty(payload: Dict[str, object], keys: List[str], default: str = "") -> str:
        for key in keys:
            value = str(payload.get(key, "")).strip()
            if value:
                return value
        return default

    @staticmethod
    def _to_list(value) -> List[object]:
        if isinstance(value, list):
            return value
        if value is None:
            return []
        return [value]

    def _infer_domain_hint(self, spec: Dict[str, object]) -> str:
        corpus: List[str] = []

        system_raw = spec.get("system", {})
        if isinstance(system_raw, dict):
            for key in ["name", "description", "short_name", "summary"]:
                value = str(system_raw.get(key, "")).strip()
                if value:
                    corpus.append(value.lower())

        for section in ["ecus", "services", "signals", "safety"]:
            for item in self._to_list(spec.get(section, [])):
                if isinstance(item, dict):
                    for value in item.values():
                        text = str(value).strip()
                        if text:
                            corpus.append(text.lower())
                elif isinstance(item, str) and item.strip():
                    corpus.append(item.strip().lower())

        joined = " ".join(corpus)
        if any(token in joined for token in ["aeb", "brake", "ebrake", "emergency braking"]):
            return "BrakeControl"
        if any(token in joined for token in ["wheel", "vehicle speed", "speed"]):
            return "VehicleSpeed"
        if any(token in joined for token in ["temp", "thermal", "heat"]):
            return "Thermal"
        if any(token in joined for token in ["gateway", "some/ip", "can", "ethernet"]):
            return "Gateway"
        if any(token in joined for token in ["battery", "soc", "bms", "charge"]):
            return "BatteryManagement"
        return "VehicleControl"

    def _normalize_spec_for_compile(self, spec: Dict[str, object]) -> Dict[str, object]:
        normalized: Dict[str, object] = {}
        domain_hint = self._infer_domain_hint(spec)

        system_raw = spec.get("system", {})
        if not isinstance(system_raw, dict):
            system_raw = {}
        normalized["system"] = {
            "name": self._pick_first_non_empty(
                system_raw,
                ["name", "short_name", "system", "system_name"],
                default=f"{domain_hint}System",
            ),
            "description": self._pick_first_non_empty(
                system_raw,
                ["description", "desc", "summary"],
                default=f"AUTOSAR architecture for {domain_hint} use case",
            ),
        }

        ecus: List[Dict[str, str]] = []
        for index, item in enumerate(self._to_list(spec.get("ecus", [])), start=1):
            if not isinstance(item, dict):
                continue
            name = self._pick_first_non_empty(item, ["name", "short_name", "id"], default=f"ECU_{index}")
            processor = self._pick_first_non_empty(
                item,
                ["processor", "hardware", "platform", "cpu", "soc", "node"],
                default="Automotive_MCU",
            )
            asil = self._pick_first_non_empty(
                item,
                ["asil", "safety_class", "safety"],
                default="QM",
            )
            ecus.append({"name": name, "processor": processor, "asil": asil})

        while len(ecus) < 2:
            if len(ecus) == 0:
                fallback_name = f"{domain_hint}InputECU"
            else:
                fallback_name = f"{domain_hint}ControlECU"
            ecus.append({"name": fallback_name, "processor": "Automotive_MCU", "asil": "QM"})

        services: List[Dict[str, str]] = []
        for index, item in enumerate(self._to_list(spec.get("services", [])), start=1):
            if not isinstance(item, dict):
                continue
            name = self._pick_first_non_empty(
                item,
                ["name", "short_name", "service", "service_name"],
                default=f"Service_{index}",
            )
            route = self._pick_first_non_empty(
                item,
                ["route", "path", "topic", "channel"],
                default=f"/{domain_hint.lower()}/service",
            )
            protocol = self._pick_first_non_empty(
                item,
                ["protocol", "transport", "bus"],
                default="SOME/IP",
            )
            services.append({"name": name, "route": route, "protocol": protocol})

        if not services:
            services.append(
                {
                    "name": f"{domain_hint}SomeIpService",
                    "route": f"/{domain_hint.lower()}/service",
                    "protocol": "SOME/IP",
                }
            )

        signals: List[Dict[str, str]] = []
        for index, item in enumerate(self._to_list(spec.get("signals", [])), start=1):
            if not isinstance(item, dict):
                continue
            name = self._pick_first_non_empty(
                item,
                ["name", "short_name", "signal", "signal_name", "source"],
                default=f"Signal_{index}",
            )
            source = self._pick_first_non_empty(item, ["source", "src", "producer", "from"], default="")
            destination = self._pick_first_non_empty(
                item,
                ["destination", "dest", "target", "to"],
                default="",
            )
            fmt = self._pick_first_non_empty(item, ["format", "bus", "protocol", "type"], default="CAN")
            signals.append(
                {
                    "name": name,
                    "source": source,
                    "destination": destination,
                    "format": fmt,
                }
            )

        if not signals:
            signals.append(
                {
                    "name": f"{domain_hint}Signal",
                    "source": ecus[0]["name"],
                    "destination": ecus[1]["name"],
                    "format": "CAN",
                }
            )

        safety_checks: List[str] = []
        for item in self._to_list(spec.get("safety", [])):
            if isinstance(item, str):
                value = item.strip()
                if value:
                    safety_checks.append(value)
                continue
            if isinstance(item, dict):
                value = self._pick_first_non_empty(item, ["check", "name", "rule", "type"], default="")
                if value:
                    safety_checks.append(value)

        if not safety_checks:
            safety_checks = ["asil_b_monitoring", "signal_plausibility_check"]

        normalized["ecus"] = ecus
        normalized["services"] = services
        normalized["signals"] = signals
        normalized["safety"] = safety_checks
        return normalized

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

    def retrain_mapping_baseline(
        self,
        train_path: Optional[Path] = None,
        val_path: Optional[Path] = None,
        test_path: Optional[Path] = None,
    ) -> Dict[str, object]:
        script_path = BASE_DIR / "scripts" / "train_mapping_baseline.py"
        if not script_path.exists():
            raise FileNotFoundError(f"Training script not found at {script_path}")

        cmd = [sys.executable, str(script_path)]
        if train_path is not None:
            cmd.extend(["--train", str(train_path)])
        if val_path is not None:
            cmd.extend(["--val", str(val_path)])
        if test_path is not None:
            cmd.extend(["--test", str(test_path)])

        result = subprocess.run(
            cmd,
            cwd=str(BASE_DIR),
            capture_output=True,
            text=True,
        )

        stdout_clean = _strip_ansi_and_controls(result.stdout)
        stderr_clean = _strip_ansi_and_controls(result.stderr)
        if result.returncode != 0:
            detail = stderr_clean or stdout_clean or "unknown training failure"
            raise RuntimeError(f"Mapping model retrain failed: {detail}")

        # Force reload so new predictions are used immediately in this session.
        self.mapping_baseline_model = None
        self.mapping_baseline_error = None
        self.last_mapping_assessment = []
        self._load_mapping_baseline_model()

        metrics: Dict[str, object] = {}
        if MAPPING_BASELINE_METRICS_PATH.exists():
            try:
                metrics = json.loads(MAPPING_BASELINE_METRICS_PATH.read_text(encoding="utf-8"))
            except Exception:
                metrics = {}

        return {
            "model_path": str(MAPPING_BASELINE_MODEL_PATH),
            "metrics_path": str(MAPPING_BASELINE_METRICS_PATH),
            "metrics": metrics,
            "stdout": stdout_clean,
            "stderr": stderr_clean,
        }

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
            spec = self._parse_yaml_spec(yaml_text)
        except Exception as exc:
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
