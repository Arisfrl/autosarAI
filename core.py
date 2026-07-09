import re
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import List, Dict, Optional

import chromadb
from chromadb.config import Settings
from chromadb.utils import embedding_functions
import yaml
from jinja2 import Environment, FileSystemLoader, select_autoescape

BASE_DIR = Path(__file__).resolve().parent


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


def _load_reference_documents() -> List[Dict[str, str]]:
    docs = []
    reference_dir = BASE_DIR / "data"
    reference_dir.mkdir(exist_ok=True)
    for path in sorted(reference_dir.glob("*.md")):
        docs.append({"name": path.name, "text": path.read_text(encoding="utf-8")})

    autosar_dir = BASE_DIR / "AUTOSAR_WorkflowExample" / "EcuSystemDescription"
    if autosar_dir.exists():
        for arxml_path in sorted(autosar_dir.rglob("*.arxml")):
            docs.append({"name": arxml_path.name, "text": _parse_autosar_arxml(arxml_path)})

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
    def __init__(self, model_name: str = "llama3.1", top_k: int = 3):
        self.model_name = model_name
        self.docs = _load_reference_documents()
        self.template_env = Environment(
            loader=FileSystemLoader(BASE_DIR / "templates"),
            autoescape=select_autoescape(enabled_extensions=("xml",)),
        )
        self.top_k = top_k
        self.vector_store = self._build_vector_store(self.docs)
        self.last_retrieved: List[str] = []
        self.last_prompt: str = ""

    def _build_vector_store(self, docs: List[Dict[str, str]]):
        persist_dir = BASE_DIR / "chroma_db"
        persist_dir.mkdir(exist_ok=True)
        client = chromadb.Client(
            settings=Settings(persist_directory=str(persist_dir), is_persistent=True)
        )
        embedding_function = embedding_functions.SentenceTransformerEmbeddingFunction(
            model_name="all-MiniLM-L6-v2"
        )
        collection = client.get_or_create_collection(
            name="autosar_docs",
            embedding_function=embedding_function,
        )

        if collection.count() == 0:
            collection.add(
                ids=[doc["name"] for doc in docs],
                metadatas=[{"source": doc["name"]} for doc in docs],
                documents=[doc["text"] for doc in docs],
            )
        return collection

    def _retrieve_documents(self, query: str, use_vector: bool = True) -> List[str]:
        if use_vector and self.vector_store is not None:
            result = self.vector_store.query(query_texts=[query], n_results=self.top_k)
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
        result = subprocess.run(
            ["ollama", "run", self.model_name, prompt],
            capture_output=True,
            text=True,
            timeout=180,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"Ollama inference failed: {result.stderr.strip() or result.stdout.strip()}"
            )
        return result.stdout.strip()

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
