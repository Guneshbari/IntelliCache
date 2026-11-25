"""
Deterministic compatibility extraction and tri-state validation engine for IntelliCache (Stage 3.5.2).
Extracts, normalizes, and evaluates technical query requirements without external LLM calls.
Enforces attribute-aware compatibility with explicit tri-state outcomes (COMPATIBLE, INCOMPATIBLE, UNCERTAIN),
provenance-bound evidence checks, and fail-closed handling for unverified or legacy records.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Sequence, Set, Tuple

from .contracts import (
    CURRENT_SCHEMA_VERSION,
    CompatibilityDecision,
    CompatibilityEvidence,
    CompatibilityReasonCode,
    CompatibilityStatus,
    ConstraintItem,
    DependencyItem,
    DimensionValue,
    EVIDENCE_TIER_RESPONSE_VERIFIED,
    EmbeddingContract,
    GenerationContract,
    MetadataStatus,
    SemanticCompatibilityMetadata,
    TRUSTED_REQUEST_EXTRACTOR_ID,
    TaskKind,
)

if TYPE_CHECKING:
    from ..core.contracts import GenerationRequest


# Keys that must never be trusted if supplied outside validated CompatibilityEvidence.
UNTRUSTED_EVIDENCE_OVERRIDE_KEYS = frozenset({
    "version_independent",
    "language_independent",
    "context_independent",
    "verified_output_format",
    "verified_json_valid",
    "freshness_verified",
    "compatible_language_versions",
    "compatible_framework_versions",
    "compatible_dependency_versions",
})

# Tokens excluded from unrecognized framework/dependency fallback inference.
_NON_LIBRARY_TOKENS = frozenset({
    "python", "python3", "javascript", "js", "typescript", "ts", "rust", "golang", "go",
    "java", "c", "c++", "cpp", "c#", "csharp", "sql", "postgresql", "postgres", "mysql",
    "sqlite", "bash", "shell", "zsh", "powershell", "ruby", "php", "swift", "kotlin",
    "elixir", "scala", "dart", "haskell", "clojure", "erlang", "perl", "lua", "zig",
    "ocaml", "julia", "solidity", "groovy", "html", "css",
    "react", "reactjs", "react.js", "vue", "vuejs", "vue.js", "angular", "angularjs",
    "next.js", "nextjs", "fastapi", "flask", "django", "express", "expressjs", "express.js",
    "spring", "docker", "dockerfile", "kubernetes", "k8s", "nestjs", "nest.js", "svelte",
    "sveltekit", "rails", "laravel", "asp.net", "actix", "axum", "gin",
    "node", "nodejs", "node.js", "bun", "deno", "cpython", "pypy", "jvm", "wasm", "webassembly",
    # Stdlib / protocols / formats / generic English nouns
    "json", "yaml", "xml", "csv", "toml", "ini", "markdown", "text", "plain", "strict",
    "http", "https", "http/2", "http/3", "tls", "ssl", "tcp", "udp", "ip", "ipv4", "ipv6",
    "oauth", "jwt", "rest", "graphql", "grpc", "utf", "utf-8", "ascii", "sha256", "md5",
    "os", "sys", "re", "math", "pathlib", "datetime", "time", "typing", "collections",
    "itertools", "functools", "asyncio", "contextlib", "unittest", "threading",
    "multiprocessing", "logging", "subprocess", "argparse", "hashlib", "hmac", "sqlite3",
    "venv", "pip", "npm", "cargo", "git", "linux", "ubuntu", "debian", "macos", "windows",
    "code", "script", "file", "files", "data", "list", "dict", "array", "string", "int",
    "float", "bool", "function", "class", "method", "module", "package", "library", "crate",
    "gem", "sdk", "framework", "runtime", "server", "client", "model", "router", "helper",
    "api", "app", "endpoint", "service", "database", "cache", "query", "version", "release",
    "testing", "test", "mock", "mocking", "conceptual", "theoretical", "security",
    "regulatory", "compliance", "web", "backend", "frontend", "ui", "mvc", "orm", "async",
    "sync", "unit", "modern", "lightweight", "custom", "new", "old", "latest", "any", "same",
    "another", "different", "this", "that", "other", "agent", "memory", "general", "detail",
    "practice", "production", "databases", "networking", "cryptography", "theory",
    "algorithms", "environment", "variables", "requirements", "requirements.txt", "stdio",
    "standard", "stdlib", "builtin", "built-in", "xml", "xml.etree", "xml.etree.elementtree",
    "parser", "example", "implementation", "library's",
})

_NON_LANGUAGE_CONCEPT_NOUNS = _NON_LIBRARY_TOKENS | frozenset({
    "order", "place", "parallel", "batch", "streaming", "real-time", "realtime", "local",
    "remote", "distributed", "concurrent", "browser", "container", "containers", "cloud",
    "cluster", "clusters", "english", "reverse", "sorted", "binary", "linear", "constant",
    "logarithmic", "polymorphism", "inheritance", "encapsulation", "abstraction",
    "interpreted", "compiled", "typed", "statically", "dynamically", "functional",
    "imperative", "declarative", "procedural", "scripting", "markup", "natural",
    "high-level", "low-level", "multi-paradigm", "object-oriented", "systems",
    "memory-safe", "preferred", "chosen", "given", "specific", "single", "wrong",
    "second", "first", "programming", "strongly", "weakly", "target", "source",
    "modern", "native", "assembly", "query",
})

_NON_LANGUAGE_PROPER_NOUNS = frozenset({
    "Nginx", "Redis", "Docker", "Kubernetes", "Postgres", "PostgreSQL", "MySQL", "SQLite",
    "Linux", "Ubuntu", "Debian", "Windows", "macOS", "Unix", "Git", "GitHub", "GitLab",
    "AWS", "GCP", "Azure", "Virtual", "Shadow", "Leaky", "Token", "Round", "Consistent",
})

_DSL_LANGUAGES = frozenset({"sql", "html", "css"})
_STRUCTURED_OUTPUT_FORMATS = frozenset({"json", "yaml", "xml", "csv"})


def extract_effective_user_query_with_strategy(request: GenerationRequest) -> Tuple[str, str]:
    """
    Identifies the effective user instruction from request.messages without blindly
    indexing messages[0]. Selects the most recent non-empty message with role == 'user'.
    Returns (query_text, extraction_strategy).
    """
    messages = getattr(request, "messages", None) or []
    if not messages:
        return "", "no_messages"

    user_contents: List[str] = []
    for msg in messages:
        role = getattr(msg, "role", None)
        content = getattr(msg, "content", None)
        if role == "user" and isinstance(content, str) and content.strip():
            user_contents.append(content.strip())

    if not user_contents:
        return "", "no_user_message"

    if len(messages) == 1 and len(user_contents) == 1:
        return user_contents[0], "single_user_message"

    return user_contents[-1], "latest_user_message"


def extract_effective_user_query(request: GenerationRequest) -> str:
    """Returns the effective user query string for compatibility analysis and embeddings."""
    query_text, _ = extract_effective_user_query_with_strategy(request)
    return query_text


class CompatibilityExtractor:
    """
    Deterministic requirement extractor (schema v3).
    Extracts structured domain dimensions from query text and request parameters.
    Ensures identical inputs always yield identical metadata without emitting
    response-level verification claims from prompt text alone.
    """

    EXTRACTOR_VERSION = TRUSTED_REQUEST_EXTRACTOR_ID

    FRAMEWORK_UNAMBIGUOUS_LANGUAGES: Dict[str, str] = {
        "fastapi": "python",
        "flask": "python",
        "django": "python",
        "spring": "java",
        "rails": "ruby",
        "laravel": "php",
        "asp.net": "c#",
        "actix": "rust",
        "axum": "rust",
        "gin": "go",
    }

    MULTI_LANGUAGE_FRAMEWORKS = frozenset({
        "react",
        "vue",
        "angular",
        "next.js",
        "express",
        "nestjs",
        "svelte",
    })

    DEPENDENCY_UNAMBIGUOUS_LANGUAGES: Dict[str, str] = {
        "pydantic": "python",
        "sqlalchemy": "python",
        "pytorch": "python",
        "tensorflow": "python",
        "scikit-learn": "python",
        "pandas": "python",
        "numpy": "python",
        "pytest": "python",
        "pytest-asyncio": "python",
        "httpx": "python",
        "aiohttp": "python",
        "requests": "python",
        "alembic": "python",
        "celery": "python",
        "marshmallow": "python",
        "asyncpg": "python",
        "psycopg2": "python",
        "psycopg": "python",
        "apscheduler": "python",
        "hvac": "python",
        "tokio": "rust",
        "serde": "rust",
        "sqlx": "rust",
        "reqwest": "rust",
        "gorm": "go",
        "hibernate": "java",
        "junit": "java",
    }

    def extract(self, request: GenerationRequest, configured_model: str) -> SemanticCompatibilityMetadata:
        """Extracts complete versioned v3 metadata from a GenerationRequest."""
        query_text = extract_effective_user_query(request)
        return self.extract_from_text(
            text=query_text,
            provider=request.provider,
            model=request.model,
            temperature=request.temperature,
            max_tokens=request.max_tokens,
            configured_model=configured_model,
        )

    def extract_from_text(
        self,
        text: str,
        provider: str = "groq",
        model: str = "llama-3.3-70b-versatile",
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        configured_model: str = "nomic-embed-text",
    ) -> SemanticCompatibilityMetadata:
        """Deterministic extraction from raw query text plus generation contract parameters."""
        clean_text = (text or "").strip()

        task_kind_dim = self._detect_task_kind(clean_text)
        task_kind_val = task_kind_dim.value if task_kind_dim.status == MetadataStatus.KNOWN else TaskKind.CODE_GENERATION

        framework_dim = self._detect_framework(clean_text)
        framework_val = framework_dim.value if framework_dim.status == MetadataStatus.KNOWN else None

        deps_dim = self._detect_dependencies(clean_text)
        dep_names: Set[str] = set()
        if deps_dim.status == MetadataStatus.KNOWN and isinstance(deps_dim.value, list):
            dep_names = {d.name for d in deps_dim.value if isinstance(d, DependencyItem)}

        lang_dim = self._detect_language(clean_text, task_kind_val, framework_val, dep_names)
        lang_val = lang_dim.value if lang_dim.status == MetadataStatus.KNOWN else None

        runtime_dim = self._detect_runtime(clean_text)
        runtime_val = runtime_dim.value if runtime_dim.status == MetadataStatus.KNOWN else None

        lang_ver_dim, fw_ver_dim, runtime_ver_dim = self._detect_versions(
            clean_text, lang_val, framework_val, runtime_val
        )

        exec_dim = self._detect_execution_model(clean_text)
        fmt_dim = self._detect_output_format(clean_text)
        out_constraints_dim = self._detect_output_constraints(clean_text)
        constraints_dim = self._detect_constraints(clean_text)
        freshness_dim = self._detect_freshness(clean_text)
        context_dep_dim = self._detect_context_dependency(clean_text)

        dimensions = {
            "language": lang_dim,
            "language_version": lang_ver_dim,
            "framework": framework_dim,
            "framework_version": fw_ver_dim,
            "runtime": runtime_dim,
            "runtime_version": runtime_ver_dim,
            "dependencies": deps_dim,
            "execution_model": exec_dim,
            "output_format": fmt_dim,
            "output_constraints": out_constraints_dim,
            "constraints": constraints_dim,
            "freshness": freshness_dim,
            "context_dependency": context_dep_dim,
        }

        return SemanticCompatibilityMetadata(
            schema_version=CURRENT_SCHEMA_VERSION,
            extractor_version=self.EXTRACTOR_VERSION,
            task_kind=task_kind_dim,
            dimensions=dimensions,
            generation_contract=GenerationContract(
                provider=provider,
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
            ),
            embedding_contract=EmbeddingContract(model=configured_model),
            compatibility_evidence=CompatibilityEvidence.default_request_derived(self.EXTRACTOR_VERSION),
        )

    def _detect_task_kind(self, text: str) -> DimensionValue:
        if not text:
            return DimensionValue(status=MetadataStatus.UNKNOWN, value=None)
        lower = text.lower()

        error_indicators = (
            "traceback", "exception", "stacktrace", "syntax error", "type error",
            "typeerror", "referenceerror", "nullpointer", "segfault", "fix error",
            "why do i get ", "error:",
        )
        if any(k in lower for k in error_indicators):
            return DimensionValue(status=MetadataStatus.KNOWN, value=TaskKind.DEBUGGING_EXPLANATION)

        code_markers = (
            "syntax", "code", "function", "method", "class", "script", "command",
            "write ", "implement ", "create ", "how to ", "how do i ", "how can i ",
            "query", "loop", "def ", "SELECT ", "import ", "const ",
        )
        conceptual_starts = (
            "what is ", "what are ", "explain ", "difference between ", "why does ",
            "how does ", "pros and cons ", "time complexity ", "define ", "meaning of ",
        )
        if any(lower.startswith(p) for p in conceptual_starts):
            if not any(k in lower for k in code_markers):
                return DimensionValue(status=MetadataStatus.KNOWN, value=TaskKind.CONCEPTUAL)

        return DimensionValue(status=MetadataStatus.KNOWN, value=TaskKind.CODE_GENERATION)

    def _detect_framework(self, text: str) -> DimensionValue:
        lower = text.lower()
        matched: List[str] = []

        # React (with English word guards)
        if (
            re.search(r"\breact(?:\.js|js)\b", lower)
            or re.search(
                r"\breact\s+(?:v?1[6-9]|v?20|component|components|hook|hooks|router|app|native|jsx|tsx|render|vdom|virtual\s+dom|props|state|context|query|element|elements|dev|developer)\b"
                r"|\b(?:in|using|with)\s+react\b",
                lower,
            )
        ):
            if not re.search(r"\b(?:people|body|skin|chemicals?|how\s+will\s+they|users?)\s+react\b", lower):
                matched.append("react")

        if re.search(r"\bvue(?:\.js|js)?\b", lower):
            matched.append("vue")
        if re.search(r"\bangular(?:\.js|js)?\b", lower):
            matched.append("angular")
        if re.search(r"\bnext(?:\.?js)\b|\bnext\s+(?:v?1[3-9]|app\s+router|pages\s+router|router|server\s+action|server\s+component|auth)\b", lower):
            matched.append("next.js")
        if re.search(r"\bfastapi\b", lower):
            matched.append("fastapi")
        if re.search(r"\bflask\b", lower):
            matched.append("flask")
        if re.search(r"\bdjango\b", lower):
            matched.append("django")
        if re.search(r"\bexpress(?:\.js|js)\b|\bexpress\s+(?:app|server|router|middleware|route|routes|endpoint|endpoints)\b|\b(?:in|using|with)\s+express\b", lower):
            if not re.search(r"\b(?:express\s+(?:intent|opinion|feeling|feelings|gratitude|train|bus|delivery))\b", lower):
                matched.append("express")
        if re.search(r"\bspring\s+(?:boot|framework|security|data|mvc|cloud|batch|context|bean|beans)\b|\b@spring\b", lower):
            matched.append("spring")
        if re.search(r"\bnest(?:\.?js)\b", lower):
            matched.append("nestjs")
        if re.search(r"\bsvelte(?:kit)?\b", lower):
            matched.append("svelte")
        if re.search(r"\bruby\s+on\s+rails\b|\brails\s+(?:app|controller|model|route|routes|migration|migrations|active\s*record)\b", lower):
            matched.append("rails")
        if re.search(r"\blaravel\b", lower):
            matched.append("laravel")
        if re.search(r"\basp\.net\b", lower):
            matched.append("asp.net")
        if re.search(r"\bactix(?:[\s\-]web)?\b", lower):
            matched.append("actix")
        if re.search(r"\baxum\b", lower):
            matched.append("axum")
        if re.search(r"\bgin[\s\-]?(?:framework|gonic|router)\b", lower) or (
            re.search(r"\b(?:in|using|with)\s+gin\b", lower) and re.search(r"\b(?:go|golang)\b", lower)
        ):
            matched.append("gin")
        if re.search(r"\bdocker(?:file)?\b", lower):
            matched.append("docker")
        if re.search(r"\b(?:kubernetes|k8s)\b", lower):
            matched.append("kubernetes")

        if len(matched) > 1:
            return DimensionValue(status=MetadataStatus.UNKNOWN, value=None)
        if len(matched) == 1:
            return DimensionValue(status=MetadataStatus.KNOWN, value=matched[0])

        # Unrecognized framework cue -> UNKNOWN instead of NOT_APPLICABLE
        unknown_fw_patterns = (
            r"\b(?:in|using|with)\s+(?:the\s+)?([a-z][a-z0-9._-]{1,30})\s+framework\b",
            r"\bthe\s+([a-z][a-z0-9._-]{1,30})\s+framework\b",
            r"\bframework\s+(?:called|named)\s+([a-z][a-z0-9._-]{1,30})\b",
        )
        for pat in unknown_fw_patterns:
            m = re.search(pat, lower)
            if m:
                candidate_tok = m.group(1).strip("._-")
                if candidate_tok and candidate_tok not in _NON_LIBRARY_TOKENS:
                    return DimensionValue(status=MetadataStatus.UNKNOWN, value=None)

        # CamelCase JS/Kit/Framework unrecognized token with explicit context
        m_camel = re.search(r"\b(?:in|using|with)\s+([A-Z][A-Za-z0-9]{1,25}(?:Framework|Kit))\b", text)
        if m_camel and m_camel.group(1).lower() not in _NON_LIBRARY_TOKENS:
            return DimensionValue(status=MetadataStatus.UNKNOWN, value=None)

        return DimensionValue(status=MetadataStatus.NOT_APPLICABLE, value=None)

    def _extract_version_candidates(self, pattern: str, lower_text: str) -> List[str]:
        versions: List[str] = []
        for match in re.finditer(pattern, lower_text):
            val = match.group(1).strip().lower()
            if val and val not in versions:
                versions.append(val)
        return versions

    def _detect_dependencies(self, text: str) -> DimensionValue:
        lower = text.lower()
        found: Dict[str, Optional[str]] = {}
        ambiguous_dep = False

        # Pattern specifications: (canonical_name, presence_regex, version_regex)
        known_lib_specs: Sequence[Tuple[str, str, Optional[str]]] = (
            ("pydantic", r"\bpydantic\b", r"\bpydantic\s*(?:v|version\s*)?([0-9]+(?:\.[0-9]+)*)\b"),
            ("sqlalchemy", r"\bsqlalchemy\b", r"\bsqlalchemy\s*(?:v|version\s*)?([0-9]+(?:\.[0-9]+)*)\b"),
            ("pytorch", r"\b(?:pytorch|torch)\b", r"\b(?:pytorch|torch)\s*(?:v|version\s*)?([0-9]+(?:\.[0-9]+)*)\b"),
            ("tensorflow", r"\btensorflow\b", r"\btensorflow\s*(?:v|version\s*)?([0-9]+(?:\.[0-9]+)*)\b"),
            ("scikit-learn", r"\b(?:scikit[\s\-]learn|sklearn)\b", r"\b(?:scikit[\s\-]learn|sklearn)\s*(?:v|version\s*)?([0-9]+(?:\.[0-9]+)*)\b"),
            ("pandas", r"\bpandas\b", r"\bpandas\s*(?:v|version\s*)?([0-9]+(?:\.[0-9]+)*)\b"),
            ("numpy", r"\bnumpy\b", r"\bnumpy\s*(?:v|version\s*)?([0-9]+(?:\.[0-9]+)*)\b"),
            ("pytest-asyncio", r"\bpytest[\s\-]asyncio\b", r"\bpytest[\s\-]asyncio\s*(?:v|version\s*)?([0-9]+(?:\.[0-9]+)*)\b"),
            ("pytest", r"\bpytest\b(?!\s*[\-]\s*asyncio)", r"\bpytest\s*(?:v|version\s*)?([0-9]+(?:\.[0-9]+)*)\b"),
            ("httpx", r"\bhttpx\b", r"\bhttpx\s*(?:v|version\s*)?([0-9]+(?:\.[0-9]+)*)\b"),
            ("aiohttp", r"\baiohttp\b", r"\baiohttp\s*(?:v|version\s*)?([0-9]+(?:\.[0-9]+)*)\b"),
            (
                "requests",
                r"\brequests\.(?:get|post|put|delete|head|patch|request|session)\b"
                r"|\b(?:using|with|import|module\s+named)\s+requests\b"
                r"|\brequests\s+(?:library|package|module|v?[0-9]+(?:\.[0-9]+)*)\b",
                r"\brequests\s*(?:v|version\s*)?([0-9]+(?:\.[0-9]+)*)\b",
            ),
            ("alembic", r"\balembic\b", r"\balembic\s*(?:v|version\s*)?([0-9]+(?:\.[0-9]+)*)\b"),
            ("celery", r"\bcelery\b", r"\bcelery\s*(?:v|version\s*)?([0-9]+(?:\.[0-9]+)*)\b"),
            ("marshmallow", r"\bmarshmallow\b", r"\bmarshmallow\s*(?:v|version\s*)?([0-9]+(?:\.[0-9]+)*)\b"),
            ("asyncpg", r"\basyncpg\b", r"\basyncpg\s*(?:v|version\s*)?([0-9]+(?:\.[0-9]+)*)\b"),
            ("psycopg2", r"\bpsycopg2\b", r"\bpsycopg2\s*(?:v|version\s*)?([0-9]+(?:\.[0-9]+)*)\b"),
            ("psycopg", r"\bpsycopg\b(?![23])", r"\bpsycopg\s*(?:v|version\s*)?([0-9]+(?:\.[0-9]+)*)\b"),
            ("apscheduler", r"\bapscheduler\b", r"\bapscheduler\s*(?:v|version\s*)?([0-9]+(?:\.[0-9]+)*)\b"),
            ("prisma", r"\bprisma\b", r"\bprisma\s*(?:v|version\s*)?([0-9]+(?:\.[0-9]+)*)\b"),
            ("zod", r"\bzod\b", r"\bzod\s*(?:v|version\s*)?([0-9]+(?:\.[0-9]+)*)\b"),
            ("axios", r"\baxios\b", r"\baxios\s*(?:v|version\s*)?([0-9]+(?:\.[0-9]+)*)\b"),
            ("redux", r"\bredux\b", r"\bredux\s*(?:v|version\s*)?([0-9]+(?:\.[0-9]+)*)\b"),
            ("tailwindcss", r"\btailwind(?:css)?\b", r"\btailwind(?:css)?\s*(?:v|version\s*)?([0-9]+(?:\.[0-9]+)*)\b"),
            ("vitest", r"\bvitest\b", r"\bvitest\s*(?:v|version\s*)?([0-9]+(?:\.[0-9]+)*)\b"),
            ("jest", r"\bjest\b", r"\bjest\s*(?:v|version\s*)?([0-9]+(?:\.[0-9]+)*)\b"),
            ("playwright", r"\bplaywright\b", r"\bplaywright\s*(?:v|version\s*)?([0-9]+(?:\.[0-9]+)*)\b"),
            ("drizzle", r"\bdrizzle\b", r"\bdrizzle\s*(?:v|version\s*)?([0-9]+(?:\.[0-9]+)*)\b"),
            ("mongoose", r"\bmongoose\b", r"\bmongoose\s*(?:v|version\s*)?([0-9]+(?:\.[0-9]+)*)\b"),
            ("tokio", r"\btokio\b", r"\btokio\s*(?:v|version\s*)?([0-9]+(?:\.[0-9]+)*)\b"),
            ("serde", r"\bserde\b", r"\bserde\s*(?:v|version\s*)?([0-9]+(?:\.[0-9]+)*)\b"),
            ("sqlx", r"\bsqlx\b", r"\bsqlx\s*(?:v|version\s*)?([0-9]+(?:\.[0-9]+)*)\b"),
            ("reqwest", r"\breqwest\b", r"\breqwest\s*(?:v|version\s*)?([0-9]+(?:\.[0-9]+)*)\b"),
            ("gorm", r"\bgorm\b", r"\bgorm\s*(?:v|version\s*)?([0-9]+(?:\.[0-9]+)*)\b"),
            ("hibernate", r"\bhibernate\b", r"\bhibernate\s*(?:v|version\s*)?([0-9]+(?:\.[0-9]+)*)\b"),
            ("junit", r"\bjunit\b", r"\bjunit\s*(?:v|version\s*)?([0-9]+(?:\.[0-9]+)*)\b"),
            ("langchain", r"\blangchain\b", r"\blangchain\s*(?:v|version\s*)?([0-9]+(?:\.[0-9]+)*)\b"),
            ("langgraph", r"\blanggraph\b", r"\blanggraph\s*(?:v|version\s*)?([0-9]+(?:\.[0-9]+)*)\b"),
            ("llamaindex", r"\bllamaindex\b", r"\bllamaindex\s*(?:v|version\s*)?([0-9]+(?:\.[0-9]+)*)\b"),
        )

        for dep_name, pres_pat, ver_pat in known_lib_specs:
            if re.search(pres_pat, lower):
                versions: List[str] = []
                if ver_pat:
                    versions = self._extract_version_candidates(ver_pat, lower)
                # Pydantic v1/v2 API-level disambiguation when explicit version omitted
                if dep_name == "pydantic" and not versions:
                    has_v2_api = bool(re.search(r"\b(?:field_validator|model_validate|model_dump|configdict|from_attributes|model_validator)\b", lower))
                    has_v1_api = bool(re.search(r"\b(?:parse_obj|orm_mode|schema_extra)\b|@validator\b|\busing\s+validator\s+decorator\b", lower))
                    if has_v2_api and has_v1_api:
                        ambiguous_dep = True
                    elif has_v2_api:
                        versions = ["2"]
                    elif has_v1_api:
                        versions = ["1"]

                if len(versions) > 1:
                    ambiguous_dep = True
                found[dep_name] = versions[0] if versions else None

        # Explicit unrecognized third-party dependency cues with optional version
        unknown_lib_patterns = (
            r"\b(?:using|with|import)\s+(?:the\s+)?([a-zA-Z][a-zA-Z0-9._-]{1,35})\s+(?:library|package|crate|gem|sdk)\b(?:\s+v?([0-9]+(?:\.[0-9]+)*))?",
            r"\b(?:library|package|crate|gem|sdk)\s+(?:named|called)\s+([a-zA-Z][a-zA-Z0-9._-]{1,35})\b(?:\s+v?([0-9]+(?:\.[0-9]+)*))?",
            r"\b(?:using|with)\s+([a-zA-Z][a-zA-Z0-9._-]{2,35})\s+(?:v([0-9]+(?:\.[0-9]+)*)|([0-9]+\.[0-9]+(?:\.[0-9]+)*))\b",
            r"\bpip\s+install\s+([a-zA-Z][a-zA-Z0-9._-]{1,35})(?:==([0-9]+(?:\.[0-9]+)*))?",
            r"\bcargo\s+add\s+([a-zA-Z][a-zA-Z0-9_-]{1,35})(?:@([0-9]+(?:\.[0-9]+)*))?",
        )
        for pat in unknown_lib_patterns:
            for m in re.finditer(pat, text):
                raw_tok = m.group(1).strip().lower()
                if not raw_tok or raw_tok in _NON_LIBRARY_TOKENS or raw_tok in found:
                    continue
                # Pick first non-None captured version group after group(1)
                ver_val: Optional[str] = None
                for g_idx in range(2, len(m.groups()) + 1):
                    g_val = m.group(g_idx)
                    if g_val:
                        ver_val = g_val.strip().lower()
                        break
                found[raw_tok] = ver_val

        # Also check explicit pip/library unknown version marker e.g. "unknown version"
        if re.search(r"\b(?:some|unspecified|unknown)\s+(?:version|library|package|dependency)\b", lower):
            ambiguous_dep = True

        if ambiguous_dep:
            return DimensionValue(status=MetadataStatus.UNKNOWN, value=None)

        if found:
            items = [DependencyItem(name=name, version=found[name]) for name in sorted(found.keys())]
            return DimensionValue(status=MetadataStatus.KNOWN, value=items)

        return DimensionValue(status=MetadataStatus.NOT_APPLICABLE, value=None)

    def _detect_language(
        self,
        text: str,
        task_kind: str,
        framework: Optional[str],
        dep_names: Set[str],
    ) -> DimensionValue:
        lower = text.lower()
        matched: Set[str] = set()

        # 1. Disambiguate Go (including "Go 1.21" / "Go 1.22")
        if re.search(r"\bgolang\b", lower):
            matched.add("go")
        elif (
            re.search(
                r"\b(?:in|using|with|for)\s+go\b"
                r"|\bgo\s+(?:v?[0-9]+\.[0-9]+(?:\.[0-9]+)?|code|lang|language|slice|slices|routine|routines|channel|channels|struct|structs|func|package|module|program|runtime|loop)\b"
                r"|\bgoroutine",
                lower,
            )
            and not re.search(r"\b(?:let\'?s|how\s+to|where\s+to|going\s+to)\s+go\s+(?:to|through|ahead|back|over)\b", lower)
        ):
            matched.add("go")

        # 2. Disambiguate R language
        if re.search(
            r"\b(?:in|using|with)\s+r\b|\br\s+(?:language|lang|script|dataframe|data\.frame|package|packages|cran|stats)\b|\brstats\b|\bdata\.frame\s+in\s+r\b",
            lower,
        ):
            matched.add("r")

        # 3. Disambiguate C language (excluding C++, C#, Objective-C)
        if re.search(r"\b(?:in|using|with)\s+c\b|\bc\s+(?:language|lang|code|program|pointer|pointers|struct|structs|compiler|header)\b", lower):
            if not re.search(r"\bc\+\+|\bc#|\bobjective-c", lower):
                matched.add("c")

        # 4. Canonical explicit languages
        if re.search(r"\bpython(?:3)?\b|\bpy\b", lower):
            matched.add("python")
        if re.search(r"\btypescript\b|\bts\b", lower):
            matched.add("typescript")
        if re.search(r"\bjavascript\b|\bjs\b", lower) or (
            re.search(r"\bnode(?:\.js)?\b|\bnodejs\b", lower) and "typescript" not in matched
        ):
            matched.add("javascript")
        if re.search(r"\brust\b", lower):
            matched.add("rust")
        if re.search(r"\bc\+\+|\bcpp\b", lower):
            matched.add("c++")
        if re.search(r"\bc#|\bcsharp\b", lower):
            matched.add("c#")
        if re.search(r"\bjava\b(?!\s*script)", lower):
            matched.add("java")
        if re.search(r"\b(?:sql|postgresql|postgres|mysql|sqlite)\b", lower):
            matched.add("sql")
        if re.search(r"\b(?:bash|shell|zsh)\b", lower):
            matched.add("bash")
        if re.search(r"\bpowershell\b", lower):
            matched.add("powershell")
        if re.search(r"\bruby\b", lower):
            matched.add("ruby")
        if re.search(r"\bphp\b", lower):
            matched.add("php")
        if re.search(r"\bswift\b", lower):
            matched.add("swift")
        if re.search(r"\bkotlin\b", lower):
            matched.add("kotlin")
        if re.search(r"\belixir\b", lower):
            matched.add("elixir")
        if re.search(r"\bscala\b", lower):
            matched.add("scala")
        if re.search(r"\bdart\b", lower):
            matched.add("dart")
        if re.search(r"\bhaskell\b", lower):
            matched.add("haskell")
        if re.search(r"\bclojure\b", lower):
            matched.add("clojure")
        if re.search(r"\berlang\b", lower):
            matched.add("erlang")
        if re.search(r"\bperl\b", lower):
            matched.add("perl")
        if re.search(r"\blua\b", lower):
            matched.add("lua")
        if re.search(r"\bzig\b(?!-?zag)", lower):
            matched.add("zig")
        if re.search(r"\bocaml\b", lower):
            matched.add("ocaml")
        if re.search(r"\b(?:in|using|with)\s+julia\b|\bjulia\s+(?:code|lang|language|script|package|struct)\b", lower):
            matched.add("julia")
        if re.search(r"\bsolidity\b", lower):
            matched.add("solidity")
        if re.search(r"\bgroovy\b", lower):
            matched.add("groovy")
        if re.search(r"\bhtml\b", lower):
            matched.add("html")
        if re.search(r"\bcss\b", lower):
            matched.add("css")

        # Handle embedded SQL query inside a single host language (e.g. "SQL query in Python")
        if len(matched) > 1 and "sql" in matched:
            non_dsl = matched - _DSL_LANGUAGES
            if len(non_dsl) == 1:
                return DimensionValue(status=MetadataStatus.KNOWN, value=next(iter(non_dsl)))

        if len(matched) > 1:
            return DimensionValue(status=MetadataStatus.UNKNOWN, value=None)
        if len(matched) == 1:
            return DimensionValue(status=MetadataStatus.KNOWN, value=next(iter(matched)))

        # 5. Distinct code syntax patterns in text
        if re.search(r"\bdef\s+[a-zA-Z_][a-zA-Z0-9_]*\s*\(", text) or (
            "import " in text and "const " not in text and "from '" not in text and 'from "' not in text
        ):
            if not ("fn " in text or "pub fn " in text or "defmodule " in text):
                return DimensionValue(status=MetadataStatus.KNOWN, value="python")
        if re.search(r"\b(?:const|let|var)\s+[a-zA-Z_][a-zA-Z0-9_]*\s*=", text) or "console.log" in text or re.search(r"\bfunction(?:\s+[a-zA-Z_$][a-zA-Z0-9_$]*)?\s*\(", text):
            if re.search(r":\s*(?:string|number|boolean|any|void|never|unknown|Promise<.+>)\b", text):
                return DimensionValue(status=MetadataStatus.KNOWN, value="typescript")
            return DimensionValue(status=MetadataStatus.KNOWN, value="javascript")
        if re.search(r"\bfn\s+[a-zA-Z_][a-zA-Z0-9_]*\s*\(", text) or "pub fn " in text:
            return DimensionValue(status=MetadataStatus.KNOWN, value="rust")
        if re.search(r"\bdefmodule\s+[A-Z][A-Za-z0-9_.]*\s+do\b", text):
            return DimensionValue(status=MetadataStatus.KNOWN, value="elixir")
        if re.search(r"\bcase\s+class\s+[A-Z][A-Za-z0-9_]*\s*\(", text):
            return DimensionValue(status=MetadataStatus.KNOWN, value="scala")
        if re.search(r"\bSELECT\s+.+\s+FROM\b", text, re.IGNORECASE):
            return DimensionValue(status=MetadataStatus.KNOWN, value="sql")
        if "public static void main" in text or "System.out.println" in text:
            return DimensionValue(status=MetadataStatus.KNOWN, value="java")

        # 6. Unambiguous single-ecosystem frameworks and dependencies
        inferred_from_fw = (
            self.FRAMEWORK_UNAMBIGUOUS_LANGUAGES.get(framework) if framework else None
        )
        inferred_from_deps = {
            self.DEPENDENCY_UNAMBIGUOUS_LANGUAGES[d]
            for d in dep_names
            if d in self.DEPENDENCY_UNAMBIGUOUS_LANGUAGES
        }
        if inferred_from_fw:
            inferred_from_deps.add(inferred_from_fw)
        if len(inferred_from_deps) > 1:
            return DimensionValue(status=MetadataStatus.UNKNOWN, value=None)
        if len(inferred_from_deps) == 1:
            return DimensionValue(status=MetadataStatus.KNOWN, value=next(iter(inferred_from_deps)))

        # 7. Multi-language frameworks (React, Vue, Next.js, Express, Angular, Svelte):
        if framework and framework in self.MULTI_LANGUAGE_FRAMEWORKS:
            if task_kind == TaskKind.CODE_GENERATION:
                return DimensionValue(status=MetadataStatus.UNKNOWN, value=None)
            if task_kind == TaskKind.CONCEPTUAL:
                return DimensionValue(status=MetadataStatus.NOT_APPLICABLE, value=None)

        # 8. Unlisted/unrecognized language cue must yield UNKNOWN, never NOT_APPLICABLE
        m_explicit_lang = re.search(r"\b([a-zA-Z][a-zA-Z0-9+#._-]{1,20})\s+(?:programming\s+)?language\b", text)
        if m_explicit_lang:
            tok = m_explicit_lang.group(1).strip().lower()
            if tok not in _NON_LANGUAGE_CONCEPT_NOUNS:
                return DimensionValue(status=MetadataStatus.UNKNOWN, value=None)

        m_in_lang = re.search(
            r"\b(?:pattern\s+matching|traits|macros|generics|concurrency|ownership|written|implemented|syntax)\s+in\s+([A-Z][a-zA-Z0-9+#._-]{1,20}|[a-z]{2,15})\b",
            text,
        )
        if m_in_lang:
            tok = m_in_lang.group(1).strip().lower()
            if tok not in _NON_LANGUAGE_CONCEPT_NOUNS and (not framework or tok != framework):
                return DimensionValue(status=MetadataStatus.UNKNOWN, value=None)

        # Unlisted TitleCase language/technology after "in " in technical conceptual queries (ignores ALL-CAPS acronyms like CQRS/HTTP)
        for m_tc in re.finditer(r"\bin\s+([A-Z][a-z][a-zA-Z0-9+#._-]{1,20})\b", text):
            raw_tok = m_tc.group(1).strip("._-")
            low_tok = raw_tok.lower()
            if (
                raw_tok not in _NON_LANGUAGE_PROPER_NOUNS
                and low_tok not in _NON_LANGUAGE_CONCEPT_NOUNS
                and (not framework or low_tok != framework)
                and low_tok not in dep_names
            ):
                return DimensionValue(status=MetadataStatus.UNKNOWN, value=None)

        # 9. Conceptual query with no language cues whatsoever -> genuinely language-agnostic
        if task_kind == TaskKind.CONCEPTUAL:
            return DimensionValue(status=MetadataStatus.NOT_APPLICABLE, value=None)

        # 10. Non-conceptual query without established language -> UNKNOWN
        return DimensionValue(status=MetadataStatus.UNKNOWN, value=None)

    def _detect_versions(
        self,
        text: str,
        language: Optional[str],
        framework: Optional[str],
        runtime: Optional[str],
    ) -> Tuple[DimensionValue, DimensionValue, DimensionValue]:
        lower = text.lower()

        # 1. Language version (multiple conflicting versions in same query -> UNKNOWN)
        lang_versions: List[str] = []
        if language == "python":
            lang_versions = self._extract_version_candidates(r"\bpython\s*v?([23](?:\.[0-9]+)+|[23])\b", lower)
        elif language == "go":
            lang_versions = self._extract_version_candidates(r"\b(?:go|golang)\s*v?(1\.[0-9]+(?:\.[0-9]+)?|2\.[0-9]+(?:\.[0-9]+)?)\b", lower)
        elif language == "java":
            lang_versions = self._extract_version_candidates(r"\bjava\s*v?([0-9]+(?:\.[0-9]+)*)\b", lower)
        elif language == "c++":
            lang_versions = self._extract_version_candidates(r"\bc\+\+\s*(98|03|11|14|17|20|23|26)\b", lower)
        elif language == "rust":
            lang_versions = self._extract_version_candidates(r"\brust\s*(?:v?([0-9]+\.[0-9]+(?:\.[0-9]+)?)|edition\s*(2015|2018|2021|2024))\b", lower)
        elif language == "typescript":
            lang_versions = self._extract_version_candidates(r"\btypescript\s*v?([0-9]+(?:\.[0-9]+)*)\b", lower)
        elif language == "php":
            lang_versions = self._extract_version_candidates(r"\bphp\s*v?([578](?:\.[0-9]+)*)\b", lower)
        elif language == "ruby":
            lang_versions = self._extract_version_candidates(r"\bruby\s*v?([23]\.[0-9]+(?:\.[0-9]+)*)\b", lower)
        elif language == "elixir":
            lang_versions = self._extract_version_candidates(r"\belixir\s*v?([0-9]+\.[0-9]+(?:\.[0-9]+)*)\b", lower)
        elif language == "scala":
            lang_versions = self._extract_version_candidates(r"\bscala\s*v?([23](?:\.[0-9]+)*)\b", lower)
        elif language == "c#":
            lang_versions = self._extract_version_candidates(r"\bc#\s*v?([0-9]+(?:\.[0-9]+)*)\b", lower)
        elif language == "sql":
            lang_versions = self._extract_version_candidates(r"\b(?:postgresql|postgres|mysql|sqlite)\s*v?([0-9]+(?:\.[0-9]+)*)\b", lower)

        if len(lang_versions) > 1:
            lang_dim = DimensionValue(status=MetadataStatus.UNKNOWN, value=None)
        elif len(lang_versions) == 1:
            lang_dim = DimensionValue(status=MetadataStatus.KNOWN, value=lang_versions[0])
        else:
            lang_dim = DimensionValue(status=MetadataStatus.NOT_APPLICABLE, value=None)

        # 2. Framework version (supports v prefix & Spring Boot)
        fw_versions: List[str] = []
        if framework == "spring":
            fw_versions = self._extract_version_candidates(r"\bspring(?:\s+boot)?\s*v?([0-9]+(?:\.[0-9]+)*)\b", lower)
        elif framework:
            escaped_fw = re.escape(framework)
            fw_versions = self._extract_version_candidates(rf"\b{escaped_fw}\s*v?([0-9]+(?:\.[0-9]+)*)\b", lower)

        if len(fw_versions) > 1:
            fw_dim = DimensionValue(status=MetadataStatus.UNKNOWN, value=None)
        elif len(fw_versions) == 1:
            fw_dim = DimensionValue(status=MetadataStatus.KNOWN, value=fw_versions[0])
        else:
            fw_dim = DimensionValue(status=MetadataStatus.NOT_APPLICABLE, value=None)

        # 3. Runtime version (Node.js, Bun, Deno, CPython, PyPy, JVM)
        runtime_versions: List[str] = []
        if runtime == "node" or "node" in lower:
            runtime_versions = self._extract_version_candidates(r"\b(?:node(?:\.js)?|nodejs)\s*v?([0-9]+(?:\.[0-9]+)*)\b", lower)
        elif runtime == "bun" or re.search(r"\bbun\b", lower):
            runtime_versions = self._extract_version_candidates(r"\bbun\s*v?([0-9]+(?:\.[0-9]+)*)\b", lower)
        elif runtime == "deno" or re.search(r"\bdeno\b", lower):
            runtime_versions = self._extract_version_candidates(r"\bdeno\s*v?([0-9]+(?:\.[0-9]+)*)\b", lower)
        elif runtime in ("cpython", "pypy", "jvm"):
            runtime_versions = self._extract_version_candidates(rf"\b{runtime}\s*v?([0-9]+(?:\.[0-9]+)*)\b", lower)

        if len(runtime_versions) > 1:
            runtime_dim = DimensionValue(status=MetadataStatus.UNKNOWN, value=None)
        elif len(runtime_versions) == 1:
            runtime_dim = DimensionValue(status=MetadataStatus.KNOWN, value=runtime_versions[0])
        else:
            runtime_dim = DimensionValue(status=MetadataStatus.NOT_APPLICABLE, value=None)

        return lang_dim, fw_dim, runtime_dim

    def _detect_runtime(self, text: str) -> DimensionValue:
        lower = text.lower()
        has_node = bool(re.search(r"\b(?:node(?:\.js)?|nodejs)\b", lower))
        has_bun = bool(re.search(r"\bbun\b", lower))
        has_deno = bool(re.search(r"\bdeno\b", lower))
        has_cpython = bool(re.search(r"\bcpython\b", lower))
        has_pypy = bool(re.search(r"\bpypy\b", lower))
        has_wasm = bool(re.search(r"\b(?:wasm|webassembly|wasi)\b", lower))

        # Remove Virtual DOM / Shadow DOM architectural phrases before checking browser DOM runtime APIs.
        dom_cleaned = re.sub(r"\b(?:virtual|shadow)\s+dom\b|\bvdom\b", " ", lower)
        has_browser = bool(
            re.search(
                r"\b(?:browser|web\s+browser|in[\s\-]browser|client[\s\-]side|"
                r"window\.(?:document|location|localstorage|sessionstorage)|"
                r"document\.(?:queryselector|queryselectorall|getelementby|createelement)|"
                r"localstorage|sessionstorage|navigator\.(?:useragent|clipboard)|"
                r"dom\s+(?:manipulation|api|tree|element|elements|event|events|node|nodes|ready)|"
                r"web\s+apis?)\b",
                dom_cleaned,
            )
        )

        matches = []
        if has_node:
            matches.append("node")
        if has_bun:
            matches.append("bun")
        if has_deno:
            matches.append("deno")
        if has_cpython:
            matches.append("cpython")
        if has_pypy:
            matches.append("pypy")
        if has_wasm:
            matches.append("wasm")
        if has_browser:
            matches.append("browser")

        if len(matches) > 1:
            return DimensionValue(status=MetadataStatus.UNKNOWN, value=None)
        if len(matches) == 1:
            return DimensionValue(status=MetadataStatus.KNOWN, value=matches[0])

        # Unrecognized explicit runtime cue -> UNKNOWN rather than NOT_APPLICABLE
        m_rt = re.search(r"\b(?:in|on|using|with)\s+(?:the\s+)?([a-z][a-z0-9._-]{1,25})\s+runtime\b", lower)
        if m_rt:
            rt_tok = m_rt.group(1).strip("._-")
            if rt_tok not in _NON_LIBRARY_TOKENS and rt_tok not in ("async", "sync", "standard"):
                return DimensionValue(status=MetadataStatus.UNKNOWN, value=None)

        return DimensionValue(status=MetadataStatus.NOT_APPLICABLE, value=None)

    def _detect_execution_model(self, text: str) -> DimensionValue:
        lower = text.lower()
        has_async = bool(
            re.search(r"\b(?:async|asynchronous(?:ly)?|asyncio|asyncpg|await|coroutine|non[\s\-]blocking|aiohttp|async\s+def)\b", lower)
        )
        has_sync = bool(
            re.search(r"\b(?:sync|synchronous(?:ly)?|blocking|requests\.get)\b", lower)
        )
        if has_async and has_sync:
            return DimensionValue(status=MetadataStatus.UNKNOWN, value=None)
        if has_async:
            return DimensionValue(status=MetadataStatus.KNOWN, value="async")
        if has_sync:
            return DimensionValue(status=MetadataStatus.KNOWN, value="sync")

        return DimensionValue(status=MetadataStatus.NOT_APPLICABLE, value=None)

    def _detect_output_format(self, text: str) -> DimensionValue:
        lower = text.lower()
        has_json = bool(
            re.search(
                r"\b(?:as\s+(?:strict\s+|valid\s+)?json|in\s+(?:strict\s+|valid\s+)?json|"
                r"format(?:ted)?\s+as\s+(?:strict\s+|valid\s+)?json|json\s+format|"
                r"return\s+(?:only\s+)?(?:strict\s+|valid\s+)?json|output\s+(?:as\s+)?(?:only\s+)?(?:strict\s+|valid\s+)?json|"
                r"strict\s+json|valid\s+json\s+only|json\s+only|json\s+response|json\s+payload)\b",
                lower,
            )
        )
        has_plain = bool(
            re.search(r"\b(?:plain\s*text|as\s+(?:plain\s*)?text|raw\s+text|no\s+markdown|without\s+markdown)\b", lower)
        )
        has_md = bool(
            re.search(r"\b(?:as\s+markdown|in\s+markdown|markdown\s+format|output\s+as\s+markdown|return\s+markdown)\b", lower)
        )
        has_yaml = bool(
            re.search(r"\b(?:as\s+yaml|in\s+yaml|yaml\s+format|output\s+(?:as\s+)?yaml|return\s+(?:as\s+)?yaml|yaml\s+only)\b", lower)
        )
        has_xml = bool(
            re.search(r"\b(?:as\s+xml|in\s+xml|xml\s+format|output\s+(?:as\s+)?xml|return\s+(?:as\s+)?xml|xml\s+only)\b", lower)
        )
        has_csv = bool(
            re.search(r"\b(?:as\s+csv|in\s+csv|csv\s+format|output\s+(?:as\s+)?csv|return\s+(?:as\s+)?csv)\b", lower)
        )

        matches = []
        if has_json:
            matches.append("json")
        if has_plain:
            matches.append("plain_text")
        if has_md:
            matches.append("markdown")
        if has_yaml:
            matches.append("yaml")
        if has_xml:
            matches.append("xml")
        if has_csv:
            matches.append("csv")

        if len(matches) > 1:
            return DimensionValue(status=MetadataStatus.UNKNOWN, value=None)
        if len(matches) == 1:
            return DimensionValue(status=MetadataStatus.KNOWN, value=matches[0])

        return DimensionValue(status=MetadataStatus.NOT_APPLICABLE, value=None)

    def _detect_output_constraints(self, text: str) -> DimensionValue:
        lower = text.lower()
        items: List[ConstraintItem] = []

        if re.search(r"\b(?:strict\s+json|valid\s+json\s+only|json\s+only|only\s+(?:return|output)\s+json)\b", lower):
            items.append(ConstraintItem(key="strict_format", operator="equals", value="json_only"))
        if re.search(r"\b(?:json\s+schema|matching\s+(?:the\s+|this\s+)?schema|follow(?:ing)?\s+(?:the\s+|this\s+)?schema|conform(?:ing)?\s+to\s+(?:the\s+|this\s+)?schema)\b", lower):
            items.append(ConstraintItem(key="schema_adherence", operator="equals", value="required"))
        if re.search(r"\b(?:no\s+markdown|without\s+markdown|no\s+code\s+fences|without\s+backticks)\b", lower):
            items.append(ConstraintItem(key="markup", operator="equals", value="no_markdown"))
        if re.search(r"\b(?:code\s+only|only\s+code|just\s+the\s+code|no\s+prose|without\s+(?:any\s+)?explanation)\b", lower):
            items.append(ConstraintItem(key="code_only", operator="equals", value="true"))

        if items:
            return DimensionValue(status=MetadataStatus.KNOWN, value=items)
        return DimensionValue(status=MetadataStatus.NOT_APPLICABLE, value=None)

    def _detect_constraints(self, text: str) -> DimensionValue:
        lower = text.lower()
        items: List[ConstraintItem] = []

        # 1. Thread safety
        has_not_thread_safe = bool(re.search(
            r"\b(?:non[\s\-]thread[\s\-]safe|not[\s\-]thread[\s\-]safe|single[\s\-]threaded\s+only|"
            r"without\s+(?:a\s+)?(?:locks?|mutex(?:es)?|locking)|no\s+(?:locks?|mutex(?:es)?|locking)|"
            r"basic\s+singleton|naive\s+singleton)\b",
            lower,
        ))
        cleaned_for_positive = re.sub(r"\b(?:non|not)[\s\-]thread[\s\-]safe\b", "", lower)
        has_thread_safe = bool(re.search(
            r"\b(?:thread[\s\-]safe|threadsafe|multi[\s\-]threaded\s+safe|concurrency[\s\-]safe|"
            r"(?:with|using)\s+(?:a\s+)?(?:locks?|mutex(?:es)?|locking|threading\.lock)|"
            r"mutex\s+lock|double[\s\-]checked\s+locking)\b",
            cleaned_for_positive,
        ))
        if has_thread_safe and has_not_thread_safe:
            return DimensionValue(status=MetadataStatus.UNKNOWN, value=None)
        if has_thread_safe:
            items.append(ConstraintItem(key="thread_safety", operator="equals", value="thread_safe"))
        elif has_not_thread_safe:
            items.append(ConstraintItem(key="thread_safety", operator="equals", value="not_thread_safe"))

        # 2. Case sensitivity
        has_cs = bool(re.search(r"\b(?:case[\s\-]sensitive|match\s+case|sensitive\s+to\s+case)\b", lower))
        has_ci = bool(re.search(r"\b(?:case[\s\-]insensitive|ignore\s+case|insensitive\s+to\s+case)\b", lower))
        if has_cs and has_ci:
            return DimensionValue(status=MetadataStatus.UNKNOWN, value=None)
        if has_cs:
            items.append(ConstraintItem(key="case_sensitivity", operator="equals", value="sensitive"))
        elif has_ci:
            items.append(ConstraintItem(key="case_sensitivity", operator="equals", value="insensitive"))

        # 3. Hidden items (include vs exclude)
        has_inc_hid = bool(re.search(r"\b(?:includ(?:e|ing)|show)\s+(?:\w+\s+)*hidden\b", lower))
        has_exc_hid = bool(re.search(r"\b(?:exclud(?:e|ing)|ignore|no|without)\s+(?:\w+\s+)*hidden\b", lower))
        if has_inc_hid and has_exc_hid:
            return DimensionValue(status=MetadataStatus.UNKNOWN, value=None)
        if has_inc_hid:
            items.append(ConstraintItem(key="hidden_items", operator="equals", value="include"))
        elif has_exc_hid:
            items.append(ConstraintItem(key="hidden_items", operator="equals", value="exclude"))

        # 4. Traversal (recursive vs flat)
        has_rec = bool(re.search(r"\brecursive(?:ly)?\b", lower))
        has_flat = bool(re.search(r"\b(?:non[\s\-]recursive|flat|shallow)\b", lower))
        if has_rec and has_flat:
            return DimensionValue(status=MetadataStatus.UNKNOWN, value=None)
        if has_rec:
            items.append(ConstraintItem(key="traversal", operator="equals", value="recursive"))
        elif has_flat:
            items.append(ConstraintItem(key="traversal", operator="equals", value="flat"))

        # 5. Mutation (in-place vs immutable)
        has_in_place = bool(re.search(r"\bin[\s\-]place\b", lower))
        has_negated_copy = bool(re.search(r"\b(?:without|no|not)\s+(?:creating|allocating|making)?\s*(?:a\s+)?new\s+(?:copy|list|array)\b", lower))
        has_immutable = bool(re.search(r"\b(?:out[\s\-]of[\s\-]place|new\s+(?:copy|list|array)|immutable)\b", lower)) and not has_negated_copy
        if has_in_place and has_immutable:
            return DimensionValue(status=MetadataStatus.UNKNOWN, value=None)
        if has_in_place:
            items.append(ConstraintItem(key="mutation", operator="equals", value="in_place"))
        elif has_immutable:
            items.append(ConstraintItem(key="mutation", operator="equals", value="immutable"))

        if items:
            return DimensionValue(status=MetadataStatus.KNOWN, value=items)
        return DimensionValue(status=MetadataStatus.NOT_APPLICABLE, value=None)

    def _detect_freshness(self, text: str) -> DimensionValue:
        lower = text.lower()
        is_time_sensitive = bool(
            re.search(
                r"\b(?:"
                r"latest\s+(?:version|versions|release|releases|stable|lts|build|update|updates|cve|cves|vulnerability|vulnerabilities|benchmarks?|news|pricing|docs?|documentation|standard)"
                r"|newest\s+(?:version|release)"
                r"|most\s+recent\s+(?:version|release|update)"
                r"|current\s+(?:latest|stable|lts|release|version|recommended\s+version|price|pricing|weather|status|ranking|rates?)"
                r"|currently\s+(?:supported|maintained|recommended|latest|deprecated)"
                r"|as\s+of\s+(?:today|now|this\s+(?:week|month|year)|202[4-9]|203[0-9])"
                r"|right\s+now"
                r"|today\'?s"
                r"|this\s+(?:week|month|year)\'s"
                r"|up[\s\-]to[\s\-]date"
                r"|end[\s\-]of[\s\-]life"
                r"|eol\s+(?:date|schedule)"
                r"|still\s+(?:supported|maintained|active)"
                r"|recent\s+(?:cve|vulnerability|vulnerabilities|breaking\s+changes?)"
                r"|what\s+(?:is|are)\s+the\s+(?:latest|current)\s+(?:version|release)"
                r")\b",
                lower,
            )
        )
        if is_time_sensitive:
            return DimensionValue(status=MetadataStatus.KNOWN, value="time_sensitive")
        return DimensionValue(status=MetadataStatus.NOT_APPLICABLE, value=None)

    def _detect_context_dependency(self, text: str) -> DimensionValue:
        if not text:
            return DimensionValue(status=MetadataStatus.UNKNOWN, value=None)
        lower = text.lower().strip()

        # Explicit back-references to unseen prior messages or missing snippets
        if re.search(
            r"\b(?:the\s+above|above\s+(?:code|snippet|error|function|output|query|example|script)|"
            r"earlier\s+(?:code|message|response|answer)|"
            r"previous\s+(?:code|function|message|response|output|attempt|turn)|"
            r"same\s+change|continue\s+(?:from|with)|"
            r"as\s+(?:before|shown\s+above|mentioned\s+above))\b",
            lower,
        ):
            return DimensionValue(status=MetadataStatus.UNKNOWN, value=None)

        # Short bare deictic request referencing an absent snippet/error
        if re.match(
            r"^(?:fix|explain|debug|refactor|optimize|rewrite|update|convert|improve|"
            r"why\s+does|why\s+did|what\s+is\s+wrong\s+with|what\s+does)\s+"
            r"(?:this|that|it)(?:\s+(?:code|snippet|function|script|error|bug|issue|problem))?[\?\.\!]*$",
            lower,
        ):
            return DimensionValue(status=MetadataStatus.UNKNOWN, value=None)

        has_inline_detail = bool(
            "```" in text
            or "`" in text
            or len(text) > 70
            or re.search(r"(?:error:|exception:|traceback|[=:{};\(\)])", lower)
        )
        if not has_inline_detail and re.search(r"\b(?:fix|explain|debug|refactor|update)\s+(?:this|that|it)\b", lower):
            return DimensionValue(status=MetadataStatus.UNKNOWN, value=None)

        return DimensionValue(status=MetadataStatus.NOT_APPLICABLE, value=None)


def _evaluate_constraint_compatibility(
    req_item: ConstraintItem,
    cand_item: ConstraintItem,
) -> Tuple[bool, Optional[str]]:
    """
    Evaluates whether candidate constraint item satisfies the request constraint item.
    Supports all 16 combinations across 'equals', 'not_equals', 'in', 'not_in'.
    """
    r_op = req_item.operator
    c_op = cand_item.operator
    r_val = req_item.value.strip().lower()
    c_val = cand_item.value.strip().lower()

    def _parse_set(val: str) -> Set[str]:
        return {x.strip().lower() for x in val.split(",") if x.strip()}

    if r_op in ("in", "not_in"):
        r_set = _parse_set(r_val)
        if not r_set:
            return False, f"Request constraint '{req_item.key}' has empty set for operator '{r_op}'."
    else:
        r_set = set()

    if c_op in ("in", "not_in"):
        c_set = _parse_set(c_val)
        if not c_set:
            return False, f"Candidate constraint '{cand_item.key}' has empty set for operator '{c_op}'."
    else:
        c_set = set()

    if r_op == "equals":
        if c_op == "equals":
            if c_val != r_val:
                return False, f"Conflicting constraint for '{req_item.key}': candidate '{c_val}' != request '{r_val}'."
            return True, None
        elif c_op == "in":
            if c_set != {r_val}:
                return False, f"Candidate allowed set {sorted(c_set)} permits values other than required exact '{r_val}'."
            return True, None
        elif c_op == "not_equals":
            return False, f"Candidate has negative constraint '{cand_item.key} != {c_val}' which cannot guarantee required '{r_val}'."
        elif c_op == "not_in":
            return False, f"Candidate has negative constraint '{cand_item.key} not_in {sorted(c_set)}' which cannot guarantee required '{r_val}'."

    elif r_op == "in":
        if c_op == "equals":
            if c_val not in r_set:
                return False, f"Candidate value '{c_val}' is not in request allowed set {sorted(r_set)}."
            return True, None
        elif c_op == "in":
            if not c_set.issubset(r_set):
                return False, f"Candidate allowed set {sorted(c_set)} is not a subset of request allowed set {sorted(r_set)}."
            return True, None
        elif c_op == "not_equals":
            return False, f"Candidate has negative constraint '{cand_item.key} != {c_val}' which cannot guarantee subset of {sorted(r_set)}."
        elif c_op == "not_in":
            return False, f"Candidate has negative constraint '{cand_item.key} not_in {sorted(c_set)}' which cannot guarantee subset of {sorted(r_set)}."

    elif r_op == "not_equals":
        if c_op == "equals":
            if c_val == r_val:
                return False, f"Candidate enforces '{cand_item.key}={c_val}' which request explicitly forbids."
            return True, None
        elif c_op == "in":
            if r_val in c_set:
                return False, f"Candidate allowed set {sorted(c_set)} includes forbidden value '{r_val}'."
            return True, None
        elif c_op == "not_equals":
            if c_val != r_val:
                return False, f"Candidate only forbids '{c_val}', does not guarantee forbidding '{r_val}'."
            return True, None
        elif c_op == "not_in":
            if r_val not in c_set:
                return False, f"Candidate forbidden set {sorted(c_set)} does not include request forbidden value '{r_val}'."
            return True, None

    elif r_op == "not_in":
        if c_op == "equals":
            if c_val in r_set:
                return False, f"Candidate value '{c_val}' is in request forbidden set {sorted(r_set)}."
            return True, None
        elif c_op == "in":
            if not c_set.isdisjoint(r_set):
                return False, f"Candidate allowed set {sorted(c_set)} overlaps with request forbidden set {sorted(r_set)}."
            return True, None
        elif c_op == "not_equals":
            if r_set != {c_val}:
                return False, f"Candidate only forbids '{c_val}', does not forbid complete request set {sorted(r_set)}."
            return True, None
        elif c_op == "not_in":
            if not r_set.issubset(c_set):
                return False, f"Candidate does not forbid all values in request forbidden set (missing {sorted(r_set - c_set)})."
            return True, None

    return False, f"Unsupported constraint comparison for '{req_item.key}': request '{r_op}' vs candidate '{c_op}'."


class CompatibilityValidator:
    """
    Deterministic tri-state compatibility validator.
    Evaluates request vs candidate compatibility metadata under strict hard-mismatch precedence,
    provenance-bound evidence rules, output-artifact proof requirements, and fail-closed legacy handling.
    """

    def validate(
        self,
        req_meta: SemanticCompatibilityMetadata,
        cand_meta: SemanticCompatibilityMetadata,
        raw_candidate_metadata: Optional[Dict[str, Any]] = None,
    ) -> CompatibilityDecision:
        pending_uncertain: Optional[CompatibilityDecision] = None

        def _note_uncertain(decision: CompatibilityDecision) -> None:
            nonlocal pending_uncertain
            if pending_uncertain is None:
                pending_uncertain = decision

        # 0. Inspect untrusted or conflicting compatibility evidence first (does not mask hard conflicts)
        trusted_cand_ev: Optional[CompatibilityEvidence] = None
        if raw_candidate_metadata is not None:
            found_override_keys = sorted(
                k for k in UNTRUSTED_EVIDENCE_OVERRIDE_KEYS
                if k in raw_candidate_metadata and raw_candidate_metadata[k] is not None
            )
            if found_override_keys:
                _note_uncertain(
                    CompatibilityDecision(
                        status=CompatibilityStatus.UNCERTAIN,
                        reason_code=CompatibilityReasonCode.UNVERIFIED_COMPATIBILITY_EVIDENCE,
                        message=f"{CompatibilityReasonCode.UNVERIFIED_COMPATIBILITY_EVIDENCE}: Candidate carries unverified top-level compatibility flags.",
                        dimension="compatibility_evidence",
                        evidence={"untrusted_keys": found_override_keys},
                    )
                )

        cand_ev = cand_meta.compatibility_evidence
        if cand_ev is not None:
            if cand_ev.has_conflicting_claims():
                reason = (
                    CompatibilityReasonCode.UNVERIFIED_COMPATIBILITY_EVIDENCE
                    if cand_ev.evidence_tier != EVIDENCE_TIER_RESPONSE_VERIFIED
                    else CompatibilityReasonCode.AMBIGUOUS_COMPATIBILITY_EVIDENCE
                )
                _note_uncertain(
                    CompatibilityDecision(
                        status=CompatibilityStatus.UNCERTAIN,
                        reason_code=reason,
                        message=f"{reason}: Candidate compatibility evidence contains unverified or conflicting claims.",
                        dimension="compatibility_evidence",
                        evidence={"source": cand_ev.source, "evidence_tier": cand_ev.evidence_tier},
                    )
                )
            elif cand_ev.evidence_tier == EVIDENCE_TIER_RESPONSE_VERIFIED:
                if cand_ev.is_trusted_response_verified() and pending_uncertain is None:
                    trusted_cand_ev = cand_ev
                elif not cand_ev.is_trusted_response_verified():
                    _note_uncertain(
                        CompatibilityDecision(
                            status=CompatibilityStatus.UNCERTAIN,
                            reason_code=CompatibilityReasonCode.UNVERIFIED_COMPATIBILITY_EVIDENCE,
                            message=f"{CompatibilityReasonCode.UNVERIFIED_COMPATIBILITY_EVIDENCE}: Candidate evidence producer '{cand_ev.source}' is not trusted.",
                            dimension="compatibility_evidence",
                            evidence={"source": cand_ev.source, "evidence_tier": cand_ev.evidence_tier},
                        )
                    )

        req_ev = req_meta.compatibility_evidence
        if req_ev is not None and req_ev.has_conflicting_claims():
            _note_uncertain(
                CompatibilityDecision(
                    status=CompatibilityStatus.UNCERTAIN,
                    reason_code=CompatibilityReasonCode.UNVERIFIED_COMPATIBILITY_EVIDENCE,
                    message=f"{CompatibilityReasonCode.UNVERIFIED_COMPATIBILITY_EVIDENCE}: Request metadata carries untrusted or conflicting evidence claims.",
                    dimension="compatibility_evidence",
                    evidence={"source": req_ev.source, "evidence_tier": req_ev.evidence_tier},
                )
            )

        # 1. Task kind validation (hard checks return INCOMPATIBLE)
        if cand_meta.task_kind.status != MetadataStatus.KNOWN:
            code = CompatibilityReasonCode.INCOMPATIBLE_TASK_KIND
            return CompatibilityDecision(
                status=CompatibilityStatus.INCOMPATIBLE,
                reason_code=code,
                message=f"{code}: Candidate task kind must be KNOWN, got {cand_meta.task_kind.status}.",
                dimension="task_kind",
                evidence={"candidate_status": cand_meta.task_kind.status},
            )
        if cand_meta.task_kind.value not in TaskKind.ALL_VALUES:
            code = CompatibilityReasonCode.INVALID_METADATA_FORMAT
            return CompatibilityDecision(
                status=CompatibilityStatus.INCOMPATIBLE,
                reason_code=code,
                message=f"{code}: Unsupported candidate task kind value: '{cand_meta.task_kind.value}'.",
                dimension="task_kind",
                evidence={"candidate_task_kind": cand_meta.task_kind.value},
            )
        if req_meta.task_kind.status == MetadataStatus.UNKNOWN:
            _note_uncertain(
                CompatibilityDecision(
                    status=CompatibilityStatus.UNCERTAIN,
                    reason_code=CompatibilityReasonCode.UNKNOWN_MATERIAL_DIMENSION,
                    message=f"{CompatibilityReasonCode.UNKNOWN_MATERIAL_DIMENSION}: Request task kind is UNKNOWN.",
                    dimension="task_kind",
                )
            )
        elif req_meta.task_kind.value != cand_meta.task_kind.value:
            code = CompatibilityReasonCode.INCOMPATIBLE_TASK_KIND
            return CompatibilityDecision(
                status=CompatibilityStatus.INCOMPATIBLE,
                reason_code=code,
                message=f"{code}: Task kind mismatch: candidate '{cand_meta.task_kind.value}' != request '{req_meta.task_kind.value}'.",
                dimension="task_kind",
                evidence={"request": req_meta.task_kind.value, "candidate": cand_meta.task_kind.value},
            )

        is_conceptual_candidate = cand_meta.task_kind.value == TaskKind.CONCEPTUAL
        has_version_independent_proof = bool(
            trusted_cand_ev is not None
            and trusted_cand_ev.version_independent
            and is_conceptual_candidate
        )
        has_language_independent_proof = bool(
            trusted_cand_ev is not None
            and trusted_cand_ev.language_independent
            and is_conceptual_candidate
        )

        # Record top-priority root-cause uncertainties cleanly while still letting any
        # independently established hard incompatibility below take precedence.
        ctx_early = self._check_context_dependency(req_meta, cand_meta, trusted_cand_ev)
        if ctx_early is not None:
            _note_uncertain(ctx_early)

        fresh_early = self._check_freshness(req_meta, cand_meta, trusted_cand_ev)
        if fresh_early is not None:
            _note_uncertain(fresh_early)

        # 2. Language dimension
        lang_decision = self._check_language(
            req_meta.dimensions.get("language"),
            cand_meta.dimensions.get("language"),
            has_language_independent_proof,
        )
        if lang_decision is not None:
            if lang_decision.is_incompatible:
                return lang_decision
            _note_uncertain(lang_decision)

        # 3. Language version dimension
        lver_decision = self._check_version_dimension(
            dim_name="language_version",
            mismatch_code=CompatibilityReasonCode.LANGUAGE_VERSION_MISMATCH,
            r_dim=req_meta.dimensions.get("language_version"),
            c_dim=cand_meta.dimensions.get("language_version"),
            version_independent_proof=has_version_independent_proof,
            compatible_versions=(
                trusted_cand_ev.compatible_language_versions if trusted_cand_ev else ()
            ),
            same_parent=self._dims_known_and_equal(
                req_meta.dimensions.get("language"), cand_meta.dimensions.get("language")
            ),
        )
        if lver_decision is not None:
            if lver_decision.is_incompatible:
                return lver_decision
            _note_uncertain(lver_decision)

        # 4. Framework dimension
        fw_decision = self._check_scalar_dimension(
            dim_name="framework",
            mismatch_code=CompatibilityReasonCode.FRAMEWORK_MISMATCH,
            unknown_code=CompatibilityReasonCode.UNKNOWN_FRAMEWORK,
            r_dim=req_meta.dimensions.get("framework"),
            c_dim=cand_meta.dimensions.get("framework"),
        )
        if fw_decision is not None:
            if fw_decision.is_incompatible:
                return fw_decision
            _note_uncertain(fw_decision)

        # 5. Framework version dimension
        fver_decision = self._check_version_dimension(
            dim_name="framework_version",
            mismatch_code=CompatibilityReasonCode.FRAMEWORK_VERSION_MISMATCH,
            r_dim=req_meta.dimensions.get("framework_version"),
            c_dim=cand_meta.dimensions.get("framework_version"),
            version_independent_proof=has_version_independent_proof,
            compatible_versions=(
                trusted_cand_ev.compatible_framework_versions if trusted_cand_ev else ()
            ),
            same_parent=self._dims_known_and_equal(
                req_meta.dimensions.get("framework"), cand_meta.dimensions.get("framework")
            ),
        )
        if fver_decision is not None:
            if fver_decision.is_incompatible:
                return fver_decision
            _note_uncertain(fver_decision)

        # 6. Runtime and runtime_version dimensions
        rt_decision = self._check_scalar_dimension(
            dim_name="runtime",
            mismatch_code=CompatibilityReasonCode.RUNTIME_MISMATCH,
            unknown_code=CompatibilityReasonCode.UNKNOWN_RUNTIME,
            r_dim=req_meta.dimensions.get("runtime"),
            c_dim=cand_meta.dimensions.get("runtime"),
        )
        if rt_decision is not None:
            if rt_decision.is_incompatible:
                return rt_decision
            _note_uncertain(rt_decision)

        rtver_decision = self._check_scalar_dimension(
            dim_name="runtime_version",
            mismatch_code=CompatibilityReasonCode.RUNTIME_VERSION_MISMATCH,
            unknown_code=CompatibilityReasonCode.UNKNOWN_VERSION,
            r_dim=req_meta.dimensions.get("runtime_version"),
            c_dim=cand_meta.dimensions.get("runtime_version"),
        )
        if rtver_decision is not None:
            if rtver_decision.is_incompatible:
                return rtver_decision
            _note_uncertain(rtver_decision)

        # 7. Dependencies and dependency versions (v3)
        dep_decision = self._check_dependencies(
            req_meta,
            cand_meta,
            trusted_cand_ev,
            has_version_independent_proof,
        )
        if dep_decision is not None:
            if dep_decision.is_incompatible:
                return dep_decision
            _note_uncertain(dep_decision)

        # 8. Execution model
        exec_decision = self._check_scalar_dimension(
            dim_name="execution_model",
            mismatch_code=CompatibilityReasonCode.EXECUTION_MODEL_MISMATCH,
            unknown_code=CompatibilityReasonCode.UNKNOWN_EXECUTION_MODEL,
            r_dim=req_meta.dimensions.get("execution_model"),
            c_dim=cand_meta.dimensions.get("execution_model"),
        )
        if exec_decision is not None:
            if exec_decision.is_incompatible:
                return exec_decision
            _note_uncertain(exec_decision)

        # 9. Output format and output_constraints (plus Stage 3.5.3 artifact-proof gate)
        out_decision = self._check_output_contract(req_meta, cand_meta, trusted_cand_ev)
        if out_decision is not None:
            if out_decision.is_incompatible:
                return out_decision
            _note_uncertain(out_decision)

        # 10. General technical constraints
        const_decision = self._check_constraints_dimension(
            dim_name="constraints",
            mismatch_code=CompatibilityReasonCode.CONSTRAINT_MISMATCH,
            r_dim=req_meta.dimensions.get("constraints"),
            c_dim=cand_meta.dimensions.get("constraints"),
        )
        if const_decision is not None:
            if const_decision.is_incompatible:
                return const_decision
            _note_uncertain(const_decision)

        # 11. Legacy v2 fail-closed check (runs AFTER all hard incompatibilities are verified)
        if cand_meta.schema_version != CURRENT_SCHEMA_VERSION or req_meta.schema_version != CURRENT_SCHEMA_VERSION:
            code = CompatibilityReasonCode.MISSING_REQUIRED_METADATA
            return CompatibilityDecision(
                status=CompatibilityStatus.UNCERTAIN,
                reason_code=code,
                message=(
                    f"{code}: Metadata schema version mismatch (request v{req_meta.schema_version}, "
                    f"candidate v{cand_meta.schema_version}) cannot prove Stage 3.5.2 compatibility."
                ),
                dimension="schema_version",
                evidence={
                    "request_schema_version": req_meta.schema_version,
                    "candidate_schema_version": cand_meta.schema_version,
                },
            )

        # 13. Return any accumulated UNCERTAIN condition
        if pending_uncertain is not None:
            return pending_uncertain

        return CompatibilityDecision(
            status=CompatibilityStatus.COMPATIBLE,
            reason_code=CompatibilityReasonCode.COMPATIBLE,
            message=f"{CompatibilityReasonCode.COMPATIBLE}: Candidate satisfied all domain requirements.",
            dimension=None,
            evidence={
                "schema_version": cand_meta.schema_version,
                "task_kind": cand_meta.task_kind.value,
                "evidence_source": cand_ev.source if cand_ev else None,
            },
        )

    @staticmethod
    def _dims_known_and_equal(r_dim: Optional[DimensionValue], c_dim: Optional[DimensionValue]) -> bool:
        if not r_dim or not c_dim:
            return False
        if r_dim.status != MetadataStatus.KNOWN or c_dim.status != MetadataStatus.KNOWN:
            return False
        return str(r_dim.value).strip().lower() == str(c_dim.value).strip().lower()

    def _check_language(
        self,
        r_dim: Optional[DimensionValue],
        c_dim: Optional[DimensionValue],
        language_independent_proof: bool,
    ) -> Optional[CompatibilityDecision]:
        if not r_dim or not c_dim:
            code = CompatibilityReasonCode.INVALID_METADATA_FORMAT
            return CompatibilityDecision(
                status=CompatibilityStatus.INCOMPATIBLE,
                reason_code=code,
                message=f"{code}: Missing dimension 'language' in metadata.",
                dimension="language",
            )
        if r_dim.status == MetadataStatus.UNKNOWN or c_dim.status == MetadataStatus.UNKNOWN:
            code = CompatibilityReasonCode.UNKNOWN_LANGUAGE
            return CompatibilityDecision(
                status=CompatibilityStatus.UNCERTAIN,
                reason_code=code,
                message=f"{code}: Language dimension has UNKNOWN state and cannot be established.",
                dimension="language",
                evidence={"request_status": r_dim.status, "candidate_status": c_dim.status},
            )
        if r_dim.status == MetadataStatus.KNOWN and c_dim.status == MetadataStatus.KNOWN:
            r_val = str(r_dim.value).lower().strip()
            c_val = str(c_dim.value).lower().strip()
            if r_val != c_val:
                if language_independent_proof:
                    return None
                code = CompatibilityReasonCode.LANGUAGE_MISMATCH
                return CompatibilityDecision(
                    status=CompatibilityStatus.INCOMPATIBLE,
                    reason_code=code,
                    message=f"{code}: Candidate language '{c_dim.value}' != request '{r_dim.value}'.",
                    dimension="language",
                    evidence={"request": r_val, "candidate": c_val},
                )
        elif r_dim.status != c_dim.status:
            if language_independent_proof:
                return None
            code = CompatibilityReasonCode.LANGUAGE_MISMATCH
            return CompatibilityDecision(
                status=CompatibilityStatus.INCOMPATIBLE,
                reason_code=code,
                message=f"{code}: Language requirement state mismatch ({r_dim.status} vs {c_dim.status}).",
                dimension="language",
                evidence={"request": r_dim.to_dict(), "candidate": c_dim.to_dict()},
            )
        return None

    def _check_scalar_dimension(
        self,
        dim_name: str,
        mismatch_code: CompatibilityReasonCode,
        unknown_code: CompatibilityReasonCode,
        r_dim: Optional[DimensionValue],
        c_dim: Optional[DimensionValue],
    ) -> Optional[CompatibilityDecision]:
        if not r_dim or not c_dim:
            code = CompatibilityReasonCode.INVALID_METADATA_FORMAT
            return CompatibilityDecision(
                status=CompatibilityStatus.INCOMPATIBLE,
                reason_code=code,
                message=f"{code}: Missing dimension '{dim_name}' in metadata.",
                dimension=dim_name,
            )
        if r_dim.status == MetadataStatus.UNKNOWN or c_dim.status == MetadataStatus.UNKNOWN:
            return CompatibilityDecision(
                status=CompatibilityStatus.UNCERTAIN,
                reason_code=unknown_code,
                message=f"{unknown_code}: Dimension '{dim_name}' has UNKNOWN state and cannot be established.",
                dimension=dim_name,
                evidence={"request_status": r_dim.status, "candidate_status": c_dim.status},
            )
        if r_dim.status == MetadataStatus.KNOWN and c_dim.status == MetadataStatus.KNOWN:
            r_val = str(r_dim.value).lower().strip()
            c_val = str(c_dim.value).lower().strip()
            if r_val != c_val:
                return CompatibilityDecision(
                    status=CompatibilityStatus.INCOMPATIBLE,
                    reason_code=mismatch_code,
                    message=f"{mismatch_code}: Candidate {dim_name} '{c_dim.value}' != request '{r_dim.value}'.",
                    dimension=dim_name,
                    evidence={"request": r_val, "candidate": c_val},
                )
        elif r_dim.status == MetadataStatus.KNOWN and c_dim.status == MetadataStatus.NOT_APPLICABLE:
            return CompatibilityDecision(
                status=CompatibilityStatus.INCOMPATIBLE,
                reason_code=mismatch_code,
                message=f"{mismatch_code}: Request requires {dim_name}='{r_dim.value}' but candidate has NOT_APPLICABLE.",
                dimension=dim_name,
                evidence={"request": r_dim.value, "candidate": None},
            )
        elif r_dim.status == MetadataStatus.NOT_APPLICABLE and c_dim.status == MetadataStatus.KNOWN:
            return CompatibilityDecision(
                status=CompatibilityStatus.INCOMPATIBLE,
                reason_code=mismatch_code,
                message=f"{mismatch_code}: Candidate specifies {dim_name}='{c_dim.value}' not requested by request.",
                dimension=dim_name,
                evidence={"request": None, "candidate": c_dim.value},
            )
        return None

    def _check_version_dimension(
        self,
        dim_name: str,
        mismatch_code: CompatibilityReasonCode,
        r_dim: Optional[DimensionValue],
        c_dim: Optional[DimensionValue],
        version_independent_proof: bool,
        compatible_versions: Tuple[str, ...],
        same_parent: bool,
    ) -> Optional[CompatibilityDecision]:
        if not r_dim or not c_dim:
            code = CompatibilityReasonCode.INVALID_METADATA_FORMAT
            return CompatibilityDecision(
                status=CompatibilityStatus.INCOMPATIBLE,
                reason_code=code,
                message=f"{code}: Missing dimension '{dim_name}' in metadata.",
                dimension=dim_name,
            )
        if r_dim.status == MetadataStatus.UNKNOWN or c_dim.status == MetadataStatus.UNKNOWN:
            return CompatibilityDecision(
                status=CompatibilityStatus.UNCERTAIN,
                reason_code=CompatibilityReasonCode.UNKNOWN_VERSION,
                message=f"{CompatibilityReasonCode.UNKNOWN_VERSION}: Dimension '{dim_name}' has UNKNOWN state.",
                dimension=dim_name,
                evidence={"request_status": r_dim.status, "candidate_status": c_dim.status},
            )

        differs = False
        r_val = str(r_dim.value).lower().strip() if r_dim.status == MetadataStatus.KNOWN else None
        c_val = str(c_dim.value).lower().strip() if c_dim.status == MetadataStatus.KNOWN else None

        if r_val != c_val:
            differs = True

        if not differs:
            return None

        if version_independent_proof:
            return None

        if same_parent and compatible_versions and r_val is not None and c_val is not None:
            if r_val in compatible_versions and c_val in compatible_versions:
                return None

        return CompatibilityDecision(
            status=CompatibilityStatus.INCOMPATIBLE,
            reason_code=mismatch_code,
            message=f"{mismatch_code}: Candidate {dim_name} '{c_val}' != request '{r_val}'.",
            dimension=dim_name,
            evidence={"request": r_val, "candidate": c_val},
        )

    def _check_dependencies(
        self,
        req_meta: SemanticCompatibilityMetadata,
        cand_meta: SemanticCompatibilityMetadata,
        trusted_cand_ev: Optional[CompatibilityEvidence],
        version_independent_proof: bool,
    ) -> Optional[CompatibilityDecision]:
        r_deps = req_meta.dimensions.get("dependencies")
        c_deps = cand_meta.dimensions.get("dependencies")
        if r_deps is None or c_deps is None:
            # Handled as legacy schema v2 -> UNCERTAIN unless invalid v3 schema
            if req_meta.schema_version >= CURRENT_SCHEMA_VERSION and cand_meta.schema_version >= CURRENT_SCHEMA_VERSION:
                code = CompatibilityReasonCode.INVALID_METADATA_FORMAT
                return CompatibilityDecision(
                    status=CompatibilityStatus.INCOMPATIBLE,
                    reason_code=code,
                    message=f"{code}: Missing required 'dependencies' dimension.",
                    dimension="dependencies",
                )
            return None

        if r_deps.status == MetadataStatus.UNKNOWN or c_deps.status == MetadataStatus.UNKNOWN:
            code = CompatibilityReasonCode.UNKNOWN_DEPENDENCY
            return CompatibilityDecision(
                status=CompatibilityStatus.UNCERTAIN,
                reason_code=code,
                message=f"{code}: Dependency requirements are UNKNOWN or ambiguous.",
                dimension="dependencies",
            )

        r_map: Dict[str, Optional[str]] = {}
        if r_deps.status == MetadataStatus.KNOWN and isinstance(r_deps.value, list):
            for it in r_deps.value:
                dep = it if isinstance(it, DependencyItem) else DependencyItem.from_dict(it)
                r_map[dep.name] = dep.version

        c_map: Dict[str, Optional[str]] = {}
        if c_deps.status == MetadataStatus.KNOWN and isinstance(c_deps.value, list):
            for it in c_deps.value:
                dep = it if isinstance(it, DependencyItem) else DependencyItem.from_dict(it)
                c_map[dep.name] = dep.version

        if set(r_map.keys()) != set(c_map.keys()):
            code = CompatibilityReasonCode.DEPENDENCY_MISMATCH
            return CompatibilityDecision(
                status=CompatibilityStatus.INCOMPATIBLE,
                reason_code=code,
                message=f"{code}: Dependency sets differ: request={sorted(r_map.keys())} vs candidate={sorted(c_map.keys())}.",
                dimension="dependencies",
                evidence={"request": sorted(r_map.keys()), "candidate": sorted(c_map.keys())},
            )

        pending_dep_uncertain: Optional[CompatibilityDecision] = None
        compat_dep_tags = set(trusted_cand_ev.compatible_dependency_versions) if trusted_cand_ev else set()

        for dep_name in sorted(r_map.keys()):
            r_ver = r_map[dep_name]
            c_ver = c_map[dep_name]
            if r_ver is not None and c_ver is not None:
                if r_ver != c_ver:
                    tag_r = f"{dep_name}:{r_ver}"
                    tag_c = f"{dep_name}:{c_ver}"
                    if version_independent_proof or (tag_r in compat_dep_tags and tag_c in compat_dep_tags):
                        continue
                    code = CompatibilityReasonCode.DEPENDENCY_VERSION_MISMATCH
                    return CompatibilityDecision(
                        status=CompatibilityStatus.INCOMPATIBLE,
                        reason_code=code,
                        message=f"{code}: Version mismatch for dependency '{dep_name}': candidate '{c_ver}' != request '{r_ver}'.",
                        dimension="dependencies",
                        evidence={"dependency": dep_name, "request_version": r_ver, "candidate_version": c_ver},
                    )
            else:
                # One or both sides lack a verified version pin for a materialized library
                if version_independent_proof:
                    continue
                if pending_dep_uncertain is None:
                    code = CompatibilityReasonCode.UNKNOWN_VERSION
                    pending_dep_uncertain = CompatibilityDecision(
                        status=CompatibilityStatus.UNCERTAIN,
                        reason_code=code,
                        message=(
                            f"{code}: Dependency '{dep_name}' lacks an established compatible version "
                            f"(request={r_ver}, candidate={c_ver})."
                        ),
                        dimension="dependencies",
                        evidence={"dependency": dep_name, "request_version": r_ver, "candidate_version": c_ver},
                    )

        return pending_dep_uncertain

    def _check_output_contract(
        self,
        req_meta: SemanticCompatibilityMetadata,
        cand_meta: SemanticCompatibilityMetadata,
        trusted_cand_ev: Optional[CompatibilityEvidence],
    ) -> Optional[CompatibilityDecision]:
        # 1. Requested output format comparison (hard mismatch check first)
        fmt_decision = self._check_scalar_dimension(
            dim_name="output_format",
            mismatch_code=CompatibilityReasonCode.OUTPUT_FORMAT_MISMATCH,
            unknown_code=CompatibilityReasonCode.UNKNOWN_OUTPUT_FORMAT,
            r_dim=req_meta.dimensions.get("output_format"),
            c_dim=cand_meta.dimensions.get("output_format"),
        )
        if fmt_decision is not None:
            return fmt_decision

        # 2. Explicit output constraints comparison (hard mismatch check)
        r_out_c = req_meta.dimensions.get("output_constraints")
        c_out_c = cand_meta.dimensions.get("output_constraints")
        if r_out_c is not None and c_out_c is not None:
            oc_decision = self._check_constraints_dimension(
                dim_name="output_constraints",
                mismatch_code=CompatibilityReasonCode.OUTPUT_CONSTRAINT_MISMATCH,
                r_dim=r_out_c,
                c_dim=c_out_c,
            )
            if oc_decision is not None:
                return oc_decision

        # 3. Response-artifact proof gate for strict/structured output formats & constraints
        r_fmt = req_meta.dimensions.get("output_format")
        req_fmt_val = str(r_fmt.value).strip().lower() if r_fmt and r_fmt.status == MetadataStatus.KNOWN else None
        has_strict_out_constraint = bool(r_out_c and r_out_c.status == MetadataStatus.KNOWN)

        if req_fmt_val in _STRUCTURED_OUTPUT_FORMATS or has_strict_out_constraint:
            if trusted_cand_ev is None:
                code = CompatibilityReasonCode.UNVERIFIED_OUTPUT_ARTIFACT
                return CompatibilityDecision(
                    status=CompatibilityStatus.UNCERTAIN,
                    reason_code=code,
                    message=(
                        f"{code}: Structured output requirement cannot rely solely on prompt-derived metadata "
                        f"without response-verified artifact evidence."
                    ),
                    dimension="output_format",
                    evidence={
                        "requested_output_format": req_fmt_val,
                        "verified_output_format": None,
                        "verified_json_valid": False,
                    },
                )

            if req_fmt_val is not None and trusted_cand_ev.verified_output_format != req_fmt_val:
                code = CompatibilityReasonCode.UNVERIFIED_OUTPUT_ARTIFACT
                return CompatibilityDecision(
                    status=CompatibilityStatus.UNCERTAIN,
                    reason_code=code,
                    message=(
                        f"{code}: Verified candidate output format '{trusted_cand_ev.verified_output_format}' "
                        f"does not establish requested '{req_fmt_val}'."
                    ),
                    dimension="output_format",
                    evidence={
                        "requested_output_format": req_fmt_val,
                        "verified_output_format": trusted_cand_ev.verified_output_format,
                    },
                )

            if req_fmt_val == "json" and not trusted_cand_ev.verified_json_valid:
                code = CompatibilityReasonCode.UNVERIFIED_OUTPUT_ARTIFACT
                return CompatibilityDecision(
                    status=CompatibilityStatus.UNCERTAIN,
                    reason_code=code,
                    message=f"{code}: JSON output requires response-verified JSON validity proof.",
                    dimension="output_format",
                    evidence={"requested_output_format": "json", "verified_json_valid": False},
                )

            # Schema-adherence verification belongs to Stage 3.5.3 unless explicitly verified
            if r_out_c and r_out_c.status == MetadataStatus.KNOWN and isinstance(r_out_c.value, list):
                for item in r_out_c.value:
                    c_item = item if isinstance(item, ConstraintItem) else ConstraintItem.from_dict(item)
                    if c_item.key == "schema_adherence" and trusted_cand_ev.artifact_schema_version is None:
                        code = CompatibilityReasonCode.UNVERIFIED_OUTPUT_ARTIFACT
                        return CompatibilityDecision(
                            status=CompatibilityStatus.UNCERTAIN,
                            reason_code=code,
                            message=f"{code}: Explicit schema adherence requires Stage 3.5.3 response schema verification.",
                            dimension="output_constraints",
                            evidence={"constraint": "schema_adherence", "artifact_schema_version": None},
                        )

        return None

    def _check_constraints_dimension(
        self,
        dim_name: str,
        mismatch_code: CompatibilityReasonCode,
        r_dim: Optional[DimensionValue],
        c_dim: Optional[DimensionValue],
    ) -> Optional[CompatibilityDecision]:
        if not r_dim or not c_dim:
            return None
        if r_dim.status == MetadataStatus.UNKNOWN or c_dim.status == MetadataStatus.UNKNOWN:
            code = CompatibilityReasonCode.UNKNOWN_MATERIAL_DIMENSION
            return CompatibilityDecision(
                status=CompatibilityStatus.UNCERTAIN,
                reason_code=code,
                message=f"{code}: Dimension '{dim_name}' has UNKNOWN state.",
                dimension=dim_name,
            )

        r_items: Dict[str, ConstraintItem] = {}
        if r_dim.status == MetadataStatus.KNOWN and isinstance(r_dim.value, list):
            for it in r_dim.value:
                item_obj = it if isinstance(it, ConstraintItem) else ConstraintItem.from_dict(it)
                r_items[item_obj.key] = item_obj

        c_items: Dict[str, ConstraintItem] = {}
        if c_dim.status == MetadataStatus.KNOWN and isinstance(c_dim.value, list):
            for it in c_dim.value:
                item_obj = it if isinstance(it, ConstraintItem) else ConstraintItem.from_dict(it)
                c_items[item_obj.key] = item_obj

        for k in sorted(set(r_items.keys()).union(set(c_items.keys()))):
            r_item = r_items.get(k)
            c_item = c_items.get(k)
            if r_item is not None and c_item is not None:
                compat, err = _evaluate_constraint_compatibility(r_item, c_item)
                if not compat:
                    return CompatibilityDecision(
                        status=CompatibilityStatus.INCOMPATIBLE,
                        reason_code=mismatch_code,
                        message=f"{mismatch_code}: {err}",
                        dimension=dim_name,
                        evidence={"key": k, "request": r_item.to_dict(), "candidate": c_item.to_dict()},
                    )
            elif r_item is not None and c_item is None:
                return CompatibilityDecision(
                    status=CompatibilityStatus.INCOMPATIBLE,
                    reason_code=mismatch_code,
                    message=f"{mismatch_code}: Request requires constraint '{k} {r_item.operator} {r_item.value}' not satisfied by candidate.",
                    dimension=dim_name,
                    evidence={"key": k, "request": r_item.to_dict(), "candidate": None},
                )
            elif r_item is None and c_item is not None:
                return CompatibilityDecision(
                    status=CompatibilityStatus.INCOMPATIBLE,
                    reason_code=mismatch_code,
                    message=f"{mismatch_code}: Candidate enforces constraint '{k} {c_item.operator} {c_item.value}' not requested.",
                    dimension=dim_name,
                    evidence={"key": k, "request": None, "candidate": c_item.to_dict()},
                )
        return None

    def _check_freshness(
        self,
        req_meta: SemanticCompatibilityMetadata,
        cand_meta: SemanticCompatibilityMetadata,
        trusted_cand_ev: Optional[CompatibilityEvidence],
    ) -> Optional[CompatibilityDecision]:
        r_fresh = req_meta.dimensions.get("freshness")
        c_fresh = cand_meta.dimensions.get("freshness")
        if r_fresh is None or c_fresh is None:
            return None
        if r_fresh.status != MetadataStatus.NOT_APPLICABLE or c_fresh.status != MetadataStatus.NOT_APPLICABLE:
            if trusted_cand_ev is not None and trusted_cand_ev.freshness_verified:
                return None
            code = CompatibilityReasonCode.FRESHNESS_NOT_ESTABLISHED
            return CompatibilityDecision(
                status=CompatibilityStatus.UNCERTAIN,
                reason_code=code,
                message=f"{code}: Time-sensitive or freshness-dependent query cannot reuse unverified cached content.",
                dimension="freshness",
                evidence={"request_status": r_fresh.status, "candidate_status": c_fresh.status},
            )
        return None

    def _check_context_dependency(
        self,
        req_meta: SemanticCompatibilityMetadata,
        cand_meta: SemanticCompatibilityMetadata,
        trusted_cand_ev: Optional[CompatibilityEvidence],
    ) -> Optional[CompatibilityDecision]:
        r_ctx = req_meta.dimensions.get("context_dependency")
        c_ctx = cand_meta.dimensions.get("context_dependency")
        if r_ctx is None or c_ctx is None:
            return None
        if r_ctx.status != MetadataStatus.NOT_APPLICABLE:
            code = CompatibilityReasonCode.CONTEXT_DEPENDENCY_UNRESOLVED
            return CompatibilityDecision(
                status=CompatibilityStatus.UNCERTAIN,
                reason_code=code,
                message=f"{code}: Single-turn request exhibits unresolved prior-context dependencies.",
                dimension="context_dependency",
                evidence={"request_status": r_ctx.status},
            )
        if c_ctx.status != MetadataStatus.NOT_APPLICABLE:
            if trusted_cand_ev is not None and trusted_cand_ev.context_independent:
                return None
            code = CompatibilityReasonCode.CONTEXT_DEPENDENCY_UNRESOLVED
            return CompatibilityDecision(
                status=CompatibilityStatus.UNCERTAIN,
                reason_code=code,
                message=f"{code}: Candidate exhibits unresolved context dependency.",
                dimension="context_dependency",
                evidence={"candidate_status": c_ctx.status},
            )
        return None


_DEFAULT_VALIDATOR = CompatibilityValidator()


def compare_compatibility_decision(
    req_meta: SemanticCompatibilityMetadata,
    cand_meta: SemanticCompatibilityMetadata,
    raw_candidate_metadata: Optional[Dict[str, Any]] = None,
) -> CompatibilityDecision:
    """Returns the structured tri-state CompatibilityDecision for two metadata records."""
    return _DEFAULT_VALIDATOR.validate(req_meta, cand_meta, raw_candidate_metadata=raw_candidate_metadata)


def compare_compatibility(
    req_meta: SemanticCompatibilityMetadata,
    cand_meta: SemanticCompatibilityMetadata,
) -> Tuple[bool, str, str]:
    """
    Backwards-compatible boolean adapter over compare_compatibility_decision().
    Fail-closed semantics: only CompatibilityStatus.COMPATIBLE maps to True;
    both INCOMPATIBLE and UNCERTAIN map to False.
    """
    decision = compare_compatibility_decision(req_meta, cand_meta)
    return decision.is_compatible, str(decision.reason_code), decision.message
