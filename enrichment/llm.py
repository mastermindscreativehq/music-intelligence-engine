"""Optional Ollama-backed enrichment steps with versioned prompts (Phase 5).

The local model is an OPTIONAL reasoning layer, never a dependency:
deterministic rules run first; the LLM is consulted only for genuinely
ambiguous judgments (docs/ai-architecture.md decision table). When Ollama
is unreachable or returns unusable output, every entry point falls back to
the honest deterministic answer — the system stays fully deterministic and
testable without a running server.

Guardrails implemented here (docs/ai-architecture.md):

1. The client performs no network acting beyond talking to the configured
   Ollama host; it never crawls, fetches pages, or sends anything.
2. Every response is parsed and validated by deterministic code against
   the template's declared OUTPUT_SCHEMA before it is used. Malformed
   output is rejected — never repaired by guessing.
3. Prompts live ONLY as immutable versioned files under
   ``prompts/templates/<domain>/<name>.v<N>.md``; each declares its
   purpose, input variables, output schema, and example. Python source
   contains no prompt text and no secrets.
4. Every successful result carries the model identifier and prompt
   version so downstream records stay reproducible and auditable.

Configuration uses environment variable NAMES only (MIE_OLLAMA_HOST /
MIE_OLLAMA_MODEL). Values are never logged.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from enrichment.roles import ROLE_VOCABULARY, classify_role

PROMPTS_DIR = Path(__file__).resolve().parents[1] / "prompts" / "templates"

DEFAULT_HOST = "http://localhost:11434"
DEFAULT_MODEL = "qwen2.5-coder:7b"
ENV_HOST = "MIE_OLLAMA_HOST"
ENV_MODEL = "MIE_OLLAMA_MODEL"

_HEADER_KEYS = ("PURPOSE", "VARIABLES", "OUTPUT_SCHEMA", "EXAMPLE")
_PLACEHOLDER = re.compile(r"\{\{\s*([a-z_][a-z0-9_]*)\s*\}\}")


class TemplateError(ValueError):
    """Raised when a prompt template file violates the contract."""


# ---------------------------------------------------------------------------
# Versioned prompt templates
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PromptTemplate:
    """One immutable, versioned prompt file."""

    domain: str
    name: str
    version: int
    purpose: str
    variables: tuple[str, ...]
    output_schema: dict
    example: dict | None
    body: str
    source_path: str
    content_hash: str

    @property
    def prompt_version(self) -> str:
        return f"v{self.version}"

    def render(self, **variables) -> str:
        missing = [name for name in self.variables
                   if name not in variables]
        if missing:
            raise TemplateError(
                f"{self.domain}/{self.name} missing variables: {missing}")
        rendered = self.body
        for name in self.variables:
            value = variables[name]
            if not isinstance(value, str):
                raise TemplateError(f"variable {name!r} must be a string")
            rendered = rendered.replace("{{" + name + "}}", value)
        return rendered


def _parse_template(domain: str, name: str, version: int,
                    path: Path, text: str) -> PromptTemplate:
    header: dict[str, str] = {}
    body_parts: list[str] = []
    in_body = False
    for line in text.splitlines():
        if not in_body and line.strip() == "---":
            in_body = True
            continue
        if in_body:
            body_parts.append(line)
            continue
        key, sep, value = line.partition(":")
        if sep:
            key = key.strip().upper()
            if key in _HEADER_KEYS:
                header[key] = value.strip()
    missing = [key for key in _HEADER_KEYS if key not in header]
    if missing or not in_body:
        raise TemplateError(
            f"{path.name}: missing sections {missing or ['body']}; "
            "templates must declare PURPOSE, VARIABLES, OUTPUT_SCHEMA, "
            "EXAMPLE before a '---' separator followed by the prompt body")

    variables = tuple(v.strip() for v in header["VARIABLES"].split(",")
                      if v.strip())
    try:
        output_schema = json.loads(header["OUTPUT_SCHEMA"])
    except ValueError as exc:
        raise TemplateError(f"{path.name}: OUTPUT_SCHEMA is not JSON: "
                            f"{exc}") from None
    if not isinstance(output_schema, dict):
        raise TemplateError(f"{path.name}: OUTPUT_SCHEMA must be a JSON "
                            "object")
    try:
        example = json.loads(header["EXAMPLE"])
    except ValueError:
        example = None

    declared = set(variables)
    used = set(_PLACEHOLDER.findall("\n".join(body_parts)))
    if used - declared:
        raise TemplateError(f"{path.name}: body uses undeclared variables "
                            f"{sorted(used - declared)}")
    if declared - used:
        raise TemplateError(f"{path.name}: declared variables unused in "
                            f"body: {sorted(declared - used)}")

    return PromptTemplate(
        domain=domain,
        name=name,
        version=version,
        purpose=header["PURPOSE"],
        variables=variables,
        output_schema=output_schema,
        example=example,
        body="\n".join(body_parts).strip() + "\n",
        source_path=str(path),
        content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
    )


def load_template(domain: str, name: str,
                  version: int | None = None) -> PromptTemplate:
    """Load ``prompts/templates/<domain>/<name>.v<N>.md``.

    Without an explicit *version* the highest available version is used;
    existing files are never modified — new versions are new files.
    """
    folder = PROMPTS_DIR / domain
    pattern = f"{name}.v*.md"
    candidates = []
    if folder.is_dir():
        for path in sorted(folder.glob(pattern)):
            match = re.fullmatch(rf"{re.escape(name)}\.v(\d+)\.md",
                                 path.name)
            if match:
                candidates.append((int(match.group(1)), path))
    if not candidates:
        raise TemplateError(f"no prompt template found for "
                            f"{domain}/{name}")
    chosen_version, path = (
        (version, folder / f"{name}.v{version}.md")
        if version is not None else max(candidates, key=lambda pair: pair[0]))
    if not path.is_file():
        raise TemplateError(f"prompt template not found: {path}")
    text = path.read_text(encoding="utf-8")
    return _parse_template(domain, name, chosen_version, path, text)


# ---------------------------------------------------------------------------
# Minimal JSON-schema validation (deterministic gate before any use)
# ---------------------------------------------------------------------------

def validate_against_schema(data: object, schema: dict,
                            where: str = "output") -> dict:
    """Validate parsed LLM output against a small JSON-schema subset."""
    expected = schema.get("type", "object")
    if expected == "object":
        if not isinstance(data, dict):
            raise ValueError(f"{where} must be a JSON object")
        required = schema.get("required") or []
        for key in required:
            if key not in data:
                raise ValueError(f"{where} missing required key {key!r}")
        properties = schema.get("properties") or {}
        for key, sub in properties.items():
            if key in data:
                validate_against_schema(data[key], sub, f"{where}.{key}")
        return data
    if expected == "array":
        if not isinstance(data, list):
            raise ValueError(f"{where} must be a JSON array")
        items = schema.get("items")
        if isinstance(items, dict):
            for index, item in enumerate(data):
                validate_against_schema(item, items, f"{where}[{index}]")
        maximum = schema.get("maxItems")
        if maximum is not None and len(data) > maximum:
            raise ValueError(f"{where} exceeds {maximum} items")
        return data  # type: ignore[return-value]
    if expected == "string":
        if not isinstance(data, str):
            raise ValueError(f"{where} must be a string")
    elif expected == "boolean":
        if not isinstance(data, bool):
            raise ValueError(f"{where} must be a boolean")
    elif expected in ("number", "integer"):
        if isinstance(data, bool) or not isinstance(data, (int, float)):
            raise ValueError(f"{where} must be a number")
        if expected == "integer" and not isinstance(data, int):
            raise ValueError(f"{where} must be an integer")
    enum = schema.get("enum")
    if enum is not None and data not in enum:
        raise ValueError(f"{where} must be one of {sorted(enum)}")
    return data  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Ollama client
# ---------------------------------------------------------------------------

@dataclass
class OllamaConfig:
    host: str = DEFAULT_HOST
    model: str = DEFAULT_MODEL
    timeout_seconds: float = 20.0
    availability_timeout_seconds: float = 3.0
    max_attempts: int = 2

    @classmethod
    def from_env(cls) -> "OllamaConfig":
        """Env var NAMES only; values are read at runtime, never logged."""
        return cls(
            host=os.environ.get(ENV_HOST) or DEFAULT_HOST,
            model=os.environ.get(ENV_MODEL) or DEFAULT_MODEL,
        )


@dataclass(frozen=True)
class LLMResult:
    ok: bool
    data: object = None
    error_kind: str | None = None
    attempts: int = 0
    model: str = ""
    prompt_version: str = ""
    template_hash: str = ""


class OllamaClient:
    """Thin stdlib client around a LOCAL Ollama server. Optional always."""

    def __init__(self, config: OllamaConfig | None = None,
                 transport=None) -> None:
        self.config = config or OllamaConfig.from_env()
        self._transport = transport      # injectable for tests/offline runs

    # -- transport ----------------------------------------------------------

    def _request(self, method: str, path: str,
                 payload: dict | None = None,
                 timeout: float | None = None) -> dict:
        url = self.config.host.rstrip("/") + path
        data = None
        headers = {}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, method=method,
                                         headers=headers)
        with urllib.request.urlopen(
                request,
                timeout=timeout or self.config.timeout_seconds) as response:
            body = response.read()
        if not body:
            return {}
        return json.loads(body.decode("utf-8"))

    def available(self) -> bool:
        """Cheap reachability probe; NEVER raises."""
        try:
            request_fn = self._transport or self._request
            request_fn("GET", "/api/tags", timeout=min(
                self.config.availability_timeout_seconds,
                self.config.timeout_seconds))
            return True
        except Exception:
            return False

    # -- generation -------------------------------------------------------------

    def generate(self, template: PromptTemplate,
                 **variables) -> LLMResult:
        """Render the template, query the model, validate strictly.

        Transport failures are retried up to config.max_attempts; schema
        violations are deterministic rejections and are NOT retried.
        """
        prompt = template.render(**variables)
        attempts = 0
        last_error: str | None = None
        while attempts < max(1, self.config.max_attempts):
            attempts += 1
            try:
                request_fn = self._transport or self._request
                response = request_fn(
                    "POST", "/api/generate",
                    {"model": self.config.model, "prompt": prompt,
                     "stream": False})
            except Exception as exc:
                last_error = type(exc).__name__
                continue
            text = response.get("response") if isinstance(response, dict) \
                else None
            if not isinstance(text, str) or not text.strip():
                return LLMResult(ok=False, error_kind="EmptyOutput",
                                 attempts=attempts,
                                 model=self.config.model,
                                 prompt_version=template.prompt_version,
                                 template_hash=template.content_hash)
            parsed = _extract_json(text)
            if parsed is None:
                return LLMResult(ok=False, error_kind="UnparsableOutput",
                                 attempts=attempts,
                                 model=self.config.model,
                                 prompt_version=template.prompt_version,
                                 template_hash=template.content_hash)
            try:
                data = validate_against_schema(parsed,
                                               template.output_schema)
            except ValueError as exc:
                return LLMResult(ok=False, error_kind=f"InvalidOutput: "
                                                      f"{exc}",
                                 attempts=attempts,
                                 model=self.config.model,
                                 prompt_version=template.prompt_version,
                                 template_hash=template.content_hash)
            return LLMResult(ok=True, data=data, attempts=attempts,
                             model=self.config.model,
                             prompt_version=template.prompt_version,
                             template_hash=template.content_hash)
        return LLMResult(ok=False, error_kind=last_error or "TransportError",
                         attempts=attempts, model=self.config.model,
                         prompt_version=template.prompt_version,
                         template_hash=template.content_hash)


_FENCE = re.compile(r"^```[a-zA-Z0-9]*\n|\n```\s*$")


def _extract_json(text: str) -> object | None:
    """Pull the first balanced JSON value out of a raw model response."""
    cleaned = _FENCE.sub("", text.strip())
    decoder = json.JSONDecoder()
    for start in (index for index, char in enumerate(cleaned)
                  if char in "{["):
        try:
            value, _ = decoder.raw_decode(cleaned[start:])
            return value
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# Phase 5 AI enrichment step: ambiguous contact-role inference
# ---------------------------------------------------------------------------

def suggest_contact_role(context_text: str,
                         client: OllamaClient | None = None
                         ) -> tuple[str, dict | None]:
    """Deterministic-first role classification with optional AI hinting.

    Returns ``(role, metadata)``:

    - Rules decide whenever they can → ``(role, None)``; no AI involved.
    - No client / unreachable server / invalid output → the honest default
      ``("unknown", None)``. Never raises on infrastructure failure.
    - A validated AI hint returns ``(role, meta)`` where meta keeps the
      INFERENCE label, model identifier, and prompt version.
    """
    rule_role = classify_role(context_text)
    if rule_role != "unknown":
        return rule_role, None
    if client is None or not isinstance(context_text, str) \
            or not context_text.strip():
        return "unknown", None
    if not client.available():
        return "unknown", None
    template = load_template("enrichment", "contact_role")
    result = client.generate(template, context_text=context_text)
    if not result.ok or not isinstance(result.data, dict):
        return "unknown", None
    role = result.data.get("role")
    # Double-check against the engine's own vocabulary: even a schema-valid
    # hint must exist in the stable vocabulary the rest of the engine uses.
    if role not in ROLE_VOCABULARY:
        return "unknown", None
    metadata = {
        "kind": "inference",
        "method": "llm",
        "model": result.model,
        "prompt_version": result.prompt_version,
        "template_hash": result.template_hash,
    }
    return role, metadata
