import re
import shutil
import subprocess
import sys
import hashlib
import threading
import json
import urllib.error
import urllib.request
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
_EMBEDDING_FUNCTION = None
_EMBEDDING_FUNCTION_LOCK = threading.Lock()


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


class AutosarHackathonEngine:
    def __init__(self, model_name: str = "llama3.1", top_k: int = 3, load_pdfs: bool = False, tenant_id: str = "public"):
        self.model_name = model_name
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

    @staticmethod
    def _doc_key(doc: Dict[str, str]) -> str:
        return f"{doc['name']}::{doc['text']}"

    @staticmethod
    def _doc_id(doc: Dict[str, str]) -> str:
        return hashlib.sha1(AutosarHackathonEngine._doc_key(doc).encode("utf-8")).hexdigest()

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
            collection.upsert(
                ids=[self._doc_id(doc) for doc in docs],
                metadatas=[{"source": doc["name"]} for doc in docs],
                documents=[doc["text"] for doc in docs],
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
        if self.vector_store is not None:
            self.vector_store.upsert(
                ids=[self._doc_id(doc) for doc in new_docs],
                metadatas=[{"source": doc["name"]} for doc in new_docs],
                documents=[doc["text"] for doc in new_docs],
            )
        return {"added": len(new_docs), "already_present": already_present}

    def _retrieve_documents(self, query: str, use_vector: bool = True) -> List[str]:
        if use_vector:
            vector_store = self._ensure_vector_store()
            result = vector_store.query(query_texts=[query], n_results=self.top_k)
            documents = result.get("documents", [[]])[0]
            self.last_retrieved = [doc for doc in documents if doc]
            return self.last_retrieved
        self.last_retrieved = _simple_retrieve(query, self.docs, self.top_k)
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
        prompt = [
            "You are an AUTOSAR automation assistant.",
            "Use the reference material below to generate a simplified AUTOSAR architecture in YAML.",
            "Return only the YAML content. Do not add any explanation or markdown fences.",
            "Focus on service mapping, signal routing, ECU load distribution, and ASIL-aware safeguards.",
            "If the request mentions Classic CAN and Adaptive SOME/IP, create a bridge section describing the service transformation.",
            "Use the following YAML structure: system, ecus, services, signals, safety.",
            "Reference material:",
        ]
        prompt.extend(retrieved_docs or ["(no reference material available)"])
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
            raise RuntimeError(
                f"Ollama inference failed: {result.stderr.strip() or result.stdout.strip()}"
            )
        return result.stdout.strip()

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
        return self._call_ollama(prompt)

    def get_last_prompt(self) -> str:
        return self.last_prompt

    def compile_to_arxml(self, yaml_text: str) -> str:
        data = yaml.safe_load(yaml_text)
        if not isinstance(data, dict):
            raise ValueError("Parsed YAML must be a dictionary.")

        data.setdefault("system", {})
        data.setdefault("ecus", [])
        data.setdefault("services", [])
        data.setdefault("signals", [])
        data.setdefault("safety", [])

        template = self.template_env.get_template("arxml_template.xml.j2")
        return template.render(spec=data)

    def safety_check(self, yaml_text: str) -> List[str]:
        issues = []
        try:
            spec = yaml.safe_load(yaml_text) or {}
        except yaml.YAMLError as exc:
            return [f"YAML parse error: {exc}"]

        if not isinstance(spec, dict):
            return ["Generated YAML is not a top-level mapping."]

        system = spec.get("system", {})
        if not system.get("name"):
            issues.append("System name is missing in the YAML.")

        ecus = spec.get("ecus", [])
        if not isinstance(ecus, list) or len(ecus) == 0:
            issues.append("No ECU definitions were found.")
        else:
            for index, ecu in enumerate(ecus, start=1):
                if not ecu.get("asil"):
                    issues.append(f"ECU #{index} is missing an ASIL assignment.")
                if not ecu.get("processor"):
                    issues.append(f"ECU #{index} has no processor or hardware binding.")

        services = spec.get("services", [])
        signals = spec.get("signals", [])
        if not services:
            issues.append("No service descriptions were generated.")
        if not signals:
            issues.append("No signal mappings were generated.")

        if not any("some/ip" in str(item).lower() for item in services):
            issues.append("No SOME/IP service protocol was identified in the generated services.")
        if not any("can" in str(item).lower() for item in signals):
            issues.append("No CAN signal was identified in the generated signal mapping.")

        if re.search(r"\bmissing\b|\bundefined\b|\bunsupported\b", yaml_text.lower()):
            issues.append("YAML contains terms that may indicate incomplete or undefined design details.")
        if re.search(r"\bdeadlock\b|\bloop\b|\bdata loss\b|\bviolation\b", yaml_text.lower()):
            issues.append("Potential safety hazard terms were detected; validate the design before deployment.")

        return issues
