"""
Deterministic semantic cache policy and candidate evaluation engine.
Enforces query eligibility, technical/programming query classification,
credential detection, candidate compatibility, similarity thresholds, and ambiguity rejection.
No LLM calls are made for eligibility or classification.
"""

import re
from typing import Any, Dict, List, Optional, Tuple

from .contracts import EligibilityResult
from ..core.contracts import GenerationRequest
from ..vector_store.contracts import VectorSearchResult


# Regex patterns for detecting credentials, keys, tokens, and private material
_SENSITIVE_PATTERNS = [
    re.compile(r"sk-[a-zA-Z0-9_\-]{20,}", re.IGNORECASE),
    re.compile(r"gsk_[a-zA-Z0-9_\-]{20,}", re.IGNORECASE),
    re.compile(r"gh[pousr]_[a-zA-Z0-9]{20,}", re.IGNORECASE),
    re.compile(r"xox[baprs]-[a-zA-Z0-9_\-]{10,}", re.IGNORECASE),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"bearer\s+[a-zA-Z0-9_\-\.]{25,}", re.IGNORECASE),
    re.compile(r"(?:password|passwd|pwd)\s*[:=]\s*\S+", re.IGNORECASE),
    re.compile(r"api[_\-]?key\s*[:=]\s*\S+", re.IGNORECASE),
    re.compile(r"secret[_\-]?key\s*[:=]\s*\S+", re.IGNORECASE),
    re.compile(r"(?:postgres|mysql|mongodb|redis):\/\/[^:\s]+:[^@\s]+@", re.IGNORECASE),
]

# Technical & programming keywords, languages, frameworks, and syntax indicators
_TECHNICAL_KEYWORDS = {
    # Languages & runtimes
    "python", "javascript", "typescript", "rust", "golang", "go", "java", "c++",
    "c#", "c", "sql", "html", "css", "bash", "shell", "sh", "zsh", "powershell",
    "php", "ruby", "swift", "kotlin", "scala", "r", "perl", "lua", "elixir",
    # Frameworks & tools
    "react", "vue", "angular", "next.js", "nextjs", "fastapi", "flask", "django",
    "express", "node", "nodejs", "spring", "docker", "dockerfile", "kubernetes",
    "k8s", "git", "github", "postgres", "postgresql", "mysql", "sqlite", "redis",
    "linux", "ubuntu", "debian", "arch", "webpack", "vite", "vitest", "pytest",
    # Concepts & queries
    "algorithm", "function", "class", "method", "variable", "interface", "struct",
    "array", "list", "dict", "hashmap", "queue", "stack", "tree", "graph",
    "pointer", "reference", "recursion", "async", "await", "coroutine", "thread",
    "mutex", "lock", "concurrency", "deadlock", "race condition", "regex",
    "regular expression", "endpoint", "api", "rest", "graphql", "json", "yaml",
    "xml", "http", "https", "status code", "header", "payload", "jwt",
    # Error & debugging terms
    "error", "exception", "traceback", "stacktrace", "bug", "syntax error",
    "type error", "typeerror", "referenceerror", "nullpointer", "segfault",
    "segmentation fault", "undefined", "nan", "debug", "refactor", "optimize",
    "implementation", "implement", "unit test", "mock", "patch",
}

_CODE_SYNTAX_MARKERS = [
    "```",
    "`",
    "def ",
    "class ",
    "import ",
    "from ",
    "async def ",
    "const ",
    "let ",
    "var ",
    "function ",
    "SELECT ",
    "FROM ",
    "WHERE ",
    "INSERT INTO ",
    "UPDATE ",
    "DELETE FROM ",
    "CREATE TABLE ",
    "public static void ",
    "fn ",
    "pub fn ",
    "struct ",
    "impl ",
    "console.log",
    "print(",
    "printf(",
    "System.out.println",
    "return ",
]


class SemanticCachePolicy:
    """
    Evaluates request eligibility and candidate compatibility.
    Deterministic, zero external network calls.
    """

    def __init__(
        self,
        min_similarity: float = 0.92,
        min_margin: float = 0.03,
        top_k: int = 5,
        configured_model: str = "nomic-embed-text",
    ):
        self.min_similarity = float(min_similarity)
        self.min_margin = float(min_margin)
        self.top_k = int(top_k)
        self.configured_model = str(configured_model).strip()

    def check_eligibility(self, request: GenerationRequest) -> EligibilityResult:
        """
        Determines if an incoming GenerationRequest is eligible for semantic caching.
        Permits only single-turn user programming/technical queries without secrets or context dependencies.
        """
        if request.stream:
            return EligibilityResult(False, "Streaming requests are not eligible for semantic caching.")

        if request.bypass_cache:
            return EligibilityResult(False, "Cache bypass explicitly requested.")

        if request.conversation_id and request.conversation_id.strip():
            return EligibilityResult(False, "Multi-turn conversation context or conversation ID present.")

        if not request.messages or len(request.messages) != 1:
            msg_count = len(request.messages) if request.messages else 0
            return EligibilityResult(
                False,
                f"Only single-turn user requests are eligible for semantic caching (received {msg_count} messages).",
            )

        single_msg = request.messages[0]
        if single_msg.role != "user":
            return EligibilityResult(
                False,
                f"Single turn must have role 'user' (received '{single_msg.role}').",
            )

        query_text = (single_msg.content or "").strip()
        if len(query_text) < 3:
            return EligibilityResult(False, "Query text is too short to establish semantic intent.")

        # Check for system prompt in metadata or payload
        if request.metadata.get("system_prompt") or any(m.role == "system" for m in request.messages):
            return EligibilityResult(False, "System prompts are not eligible for semantic caching.")

        # Check for credentials or secrets in query text
        for pattern in _SENSITIVE_PATTERNS:
            if pattern.search(query_text):
                return EligibilityResult(
                    False,
                    "Query appears to contain sensitive credentials, keys, or secrets.",
                )

        # Technical/programming query classification
        if not self._is_technical_query(query_text):
            return EligibilityResult(
                False,
                "Query does not match technical or programming criteria.",
            )

        return EligibilityResult(True, "Eligible for semantic caching.")

    def _is_technical_query(self, text: str) -> bool:
        """Determines if query contains technical markers, keywords, or code structures."""
        # Check code markers
        for marker in _CODE_SYNTAX_MARKERS:
            if marker in text:
                return True

        # Tokenize words for keyword matching
        words = set(re.findall(r"[a-zA-Z0-9_#\+\.\-]+", text.lower()))
        if words.intersection(_TECHNICAL_KEYWORDS):
            return True

        # Common programming question structure ("how to ...", "how do i ...", "write a function ...")
        lower_text = text.lower()
        if any(lower_text.startswith(prefix) for prefix in ("how to ", "how do i ", "write a ", "implement ", "explain ")):
            if any(term in lower_text for term in ("code", "script", "program", "query", "loop", "error", "file", "test", "data")):
                return True

        return False

    def is_candidate_compatible(
        self,
        candidate: VectorSearchResult,
        request: GenerationRequest,
    ) -> Tuple[bool, str]:
        """
        Verifies that a nearest-neighbour vector candidate matches all required
        compatibility criteria: namespace, embedding model, LLM provider, LLM model,
        temperature, max_tokens, and valid cached response.
        """
        if not candidate.cached_response or not candidate.cached_response.strip():
            return False, "Candidate has no associated active exact-cache response."

        if candidate.cache_namespace != request.namespace:
            return False, f"Namespace mismatch: candidate '{candidate.cache_namespace}' != request '{request.namespace}'."

        if candidate.model != self.configured_model:
            return False, f"Embedding model mismatch: candidate '{candidate.model}' != configured '{self.configured_model}'."

        meta = candidate.metadata or {}
        if not meta:
            return False, "Candidate is missing required generation compatibility metadata."

        llm_provider = meta.get("llm_provider")
        if not llm_provider or llm_provider != request.provider:
            return False, f"LLM provider mismatch: candidate '{llm_provider}' != request '{request.provider}'."

        llm_model = meta.get("llm_model")
        if not llm_model or llm_model != request.model:
            return False, f"LLM model mismatch: candidate '{llm_model}' != request '{request.model}'."

        cand_temp = meta.get("temperature")
        if cand_temp is None:
            return False, "Candidate metadata missing temperature configuration."
        try:
            if abs(float(cand_temp) - float(request.temperature)) > 1e-4:
                return False, f"Temperature mismatch: candidate {cand_temp} != request {request.temperature}."
        except (ValueError, TypeError):
            return False, "Candidate has invalid temperature in metadata."

        cand_max_tokens = meta.get("max_tokens")
        if cand_max_tokens != request.max_tokens:
            return False, f"max_tokens mismatch: candidate {cand_max_tokens} != request {request.max_tokens}."

        return True, "Compatible"

    def evaluate_candidates(
        self,
        candidates: List[VectorSearchResult],
        request: GenerationRequest,
    ) -> Tuple[Optional[VectorSearchResult], Optional[str]]:
        """
        Evaluates nearest-neighbour candidates in order of similarity.
        Applies compatibility checks, minimum similarity threshold, and ambiguity margin.

        Returns:
            (winning_candidate, None) on successful match, or
            (None, rejection_reason) on rejection.
        """
        if not candidates:
            return None, "No vector candidates retrieved."

        # Filter candidates by compatibility
        compatible_candidates: List[VectorSearchResult] = []
        for cand in candidates:
            is_compat, reason = self.is_candidate_compatible(cand, request)
            if is_compat:
                compatible_candidates.append(cand)

        if not compatible_candidates:
            return None, "No compatible candidates satisfied generation metadata requirements."

        top_cand = compatible_candidates[0]

        # 1. Minimum similarity threshold check
        if top_cand.similarity < self.min_similarity:
            return (
                None,
                f"Similarity below threshold: {top_cand.similarity:.4f} < {self.min_similarity:.4f}.",
            )

        # 2. Ambiguity check: if top candidate is too close to another candidate with a different answer
        top_response = (top_cand.cached_response or "").strip()
        for other in compatible_candidates[1:]:
            margin = top_cand.similarity - other.similarity
            if margin < self.min_margin:
                other_response = (other.cached_response or "").strip()
                if other_response != top_response:
                    return (
                        None,
                        f"Ambiguous candidate: nearest alternative candidate margin ({margin:.4f}) "
                        f"is below minimum margin ({self.min_margin:.4f}) with a different answer.",
                    )

        return top_cand, None
