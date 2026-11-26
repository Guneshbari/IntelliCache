"""
Stage 3.6 Deterministic Context Dependency Classifier and Versioned Context Fingerprinter.

Analyzes full conversation context (system instructions, prior turns, schemas, code
snippets, configurations, framework decisions, and completeness state) without network
or LLM calls. Fails closed (CONTEXT_UNCERTAIN) when required history is missing or
ambiguous, prevents raw private conversation transcripts from being persisted inside
shared semantic vector metadata, and prevents irrelevant chatter from blocking safe
cross-conversation semantic reuse of standalone technical answers.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Sequence, Set, Tuple

from .contracts import (
    CONTEXT_CONTRACT_SCHEMA_VERSION,
    CONTEXT_INDEPENDENT_SENTINEL,
    ContextClass,
    ContextCompatibilityContract,
)
from ..privacy import (
    CACHE_VISIBILITY_PRIVATE,
    CACHE_VISIBILITY_TENANT_SHARED,
    CACHE_VISIBILITY_UNKNOWN,
    derive_scoped_exact_key,
    normalize_cache_visibility,
    normalize_principal_id,
)

if TYPE_CHECKING:
    from ..core.contracts import ChatMessage, GenerationRequest


SENSITIVE_MATERIAL_PATTERNS = (
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
)

CLIENT_FORBIDDEN_CONTEXT_KEYS = frozenset({
    "context_fingerprint",
    "context_class",
    "context_version",
    "context_contract",
    "context_metadata",
})

# Explicit backward-pointing references that always depend on prior turns.
_HARD_BACKREF_RE = re.compile(
    r"\b(?:"
    r"the\s+above|"
    r"above\s+(?:code|snippet|error|function|output|query|example|script|schema|config|implementation|table)|"
    r"(?:the\s+)?previous\s+(?:implementation|code|snippet|function|method|class|message|response|output|attempt|turn|approach|version|schema|config|example|query)|"
    r"earlier\s+(?:code|snippet|implementation|message|response|answer|turn|discussion|version|schema|config)|"
    r"as\s+(?:discussed\s+earlier|mentioned\s+(?:earlier|above)|shown\s+above|noted\s+earlier|before)|"
    r"from\s+earlier|"
    r"continue(?:\s+(?:from|with|building|implementing|that|this))?|"
    r"the\s+same\s+(?:change|function|implementation|schema|query|code|snippet|class|endpoint|config)|"
    r"based\s+on\s+(?:the|that|this|our)\s+(?:schema|config|configuration|code|implementation|snippet|discussion|conversation|context|table|setup)|"
    r"using\s+(?:the|that|this|our)\s+(?:previous|above|existing)\s+\w+"
    r")\b",
    re.IGNORECASE,
)

# Deictic actions referencing an antecedent object unless complete inline code/schema appears in query.
_DEICTIC_ACTION_RE = re.compile(
    r"\b(?:"
    r"(?:modify|update|refactor|fix|explain|debug|extend|optimize|rewrite|convert|improve|change|simplify|adapt|review|test|document)\s+"
    r"(?:it|that|this|them|the\s+(?:code|snippet|function|method|class|implementation|script|query|schema|table|config|configuration|endpoint|component|error|response|output))\b|"
    r"make\s+(?:it|that|this)\s+\w+|"
    r"(?:add|apply|attach|append|wire)\b[^\.!\?]{0,60}\bto\s+(?:it|that|this|the\s+(?:code|function|method|class|query|schema|table|endpoint|config|component))\b|"
    r"(?:what|how)\s+about\s+\S+|"
    r"(?:does|will|would|can|could|is)\s+(?:that|it|this)\s+\w+|"
    r"(?:why|how)\s+(?:does|did|would|can)\s+(?:it|that|this)\b|"
    r"what\s+(?:is\s+wrong\s+with|does)\s+(?:it|that|this)\b|"
    r"(?:show|give|provide)(?:\s+me)?\s+an?\s+example\b(?!\s+(?:in|of|for)\s+\w{2,})"
    r")",
    re.IGNORECASE,
)

_EXTERNAL_POSSESSIVE_ENTITY_RE = re.compile(
    r"\b(?:our|my|the\s+(?:existing|current|previous|above|defined|shared))\s+"
    r"(?:schema|database|tables?|codebase|config|configuration|implementation|code|service|endpoint|api|component|model|timeout|retries|settings)\b",
    re.IGNORECASE,
)

_PERSONAL_CONTEXT_RE = re.compile(
    r"(?:\b(?:my\s+(?:name|email|phone|address|company|org|account)|i\s+work\s+at|confidential|internal\s+only|proprietary)\b|"
    r"/home/[a-zA-Z0-9_.\-]+/|/Users/[a-zA-Z0-9_.\-]+/|C:\\Users\\)",
    re.IGNORECASE,
)

_DDL_SCHEMA_RE = re.compile(
    r"(?:CREATE\s+TABLE|ALTER\s+TABLE|REFERENCES\s+\w+|PRIMARY\s+KEY|FOREIGN\s+KEY|\btable\s+[a-zA-Z_]\w*\s*\([^)]+\))",
    re.IGNORECASE,
)

_CONFIG_SIGNAL_RE = re.compile(
    r"(?:^\s*\"[a-zA-Z0-9_\-]+\"\s*:|^\s*[a-zA-Z0-9_\-]{2,}\s*:\s*\S+|^\s*[A-Z][A-Z0-9_]{2,}\s*=\s*\S+)",
    re.MULTILINE,
)

_DECISION_SIGNAL_RE = re.compile(
    r"\b(?:we\s+(?:are\s+)?us(?:e|ing)|our\s+(?:stack|backend|app|project|service)\s+uses?|"
    r"target(?:ing)?\s+(?:python|node|react|fastapi|django|postgresql|postgres|java|go|typescript)|"
    r"always\s+(?:output|return|use|respond|include|exclude)|"
    r"must\s+(?:use|output|return|include|support)|"
    r"do\s+not\s+use|never\s+use)\b",
    re.IGNORECASE,
)

_SQL_QUERY_DOMAIN_TOKENS = frozenset({
    "sql", "query", "select", "insert", "update", "delete", "join", "where", "index",
    "table", "tables", "column", "columns", "row", "rows", "record", "records",
    "schema", "migration", "foreign", "primary", "database", "postgres", "postgresql",
    "mysql", "sqlite", "filter", "aggregate", "count",
})

_CONFIG_DOMAIN_TOKENS = frozenset({
    "config", "configuration", "setting", "settings", "timeout", "retries", "retry",
    "pool", "pool_size", "port", "host", "env", "yaml", "toml", "json", "limit",
    "threshold", "interval", "backoff", "parameter", "parameters", "flag", "flags",
})

_CONCEPTUAL_SELF_CONTAINED_STARTERS = (
    "what is ",
    "what does ",
    "how do i ",
    "how to ",
    "explain ",
    "define ",
    "write a ",
    "create a ",
    "implement a ",
)

_STOP_IDENTIFIERS = frozenset({
    "python", "javascript", "typescript", "java", "rust", "golang", "sql", "json", "yaml",
    "xml", "csv", "http", "https", "code", "function", "method", "class", "string", "list",
    "dict", "array", "file", "data", "helper", "write", "create", "return", "true", "false",
    "none", "null", "import", "from", "with", "using", "based", "async", "await", "react",
    "fastapi", "django", "flask", "reverse", "sort", "merge", "active", "default", "value",
    "values", "error", "handling",
})


def contains_sensitive_material(text: str) -> bool:
    """Returns True if text contains API keys, private keys, credentials, or DSN secrets."""
    if not text:
        return False
    return any(pattern.search(text) for pattern in SENSITIVE_MATERIAL_PATTERNS)


def _normalize_text(text: Optional[str]) -> str:
    if not isinstance(text, str):
        return ""
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    return "\n".join(lines).strip()


def _has_inline_artifact(text: str) -> bool:
    if "```" in text or _DDL_SCHEMA_RE.search(text):
        return True
    if re.search(r"(?:^|\n)\s*(?:def |async def |class |function |SELECT |INSERT |UPDATE )", text):
        return True
    if re.search(r"\{[^{}]{8,}\}", text) and ":" in text:
        return True
    return False


def _extract_identifiers(text: str) -> Set[str]:
    tokens = set(re.findall(r"\b[a-zA-Z_][a-zA-Z0-9_]{2,}\b", (text or "").lower()))
    return {tok for tok in tokens if tok not in _STOP_IDENTIFIERS}


def _extract_table_and_column_tokens(text: str) -> Set[str]:
    found: Set[str] = set()
    for m in re.finditer(r"\bCREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([a-zA-Z_]\w*)\s*\(([^;]+?)\)", text, re.IGNORECASE | re.DOTALL):
        found.add(m.group(1).lower())
        for col_line in m.group(2).split(","):
            parts = col_line.strip().split()
            if parts and parts[0].upper() not in ("PRIMARY", "FOREIGN", "CONSTRAINT", "UNIQUE", "CHECK"):
                col = parts[0].strip('"`[]').lower()
                if col:
                    found.add(col)
    return found


def _has_code_construct(text: str) -> bool:
    return bool(
        "```" in text
        or re.search(r"(?:^|\n)\s*(?:def |async def |class |function |export |interface |struct |impl |const |let |public\s+)", text)
    )


def _is_chitchat_turn(text: str) -> bool:
    cleaned = re.sub(r"[^a-z0-9\s]", "", (text or "").lower()).strip()
    if not cleaned:
        return True
    chitchat_exact = {
        "hi", "hello", "hey", "thanks", "thank you", "ok", "okay", "sure", "got it",
        "cool", "great", "good morning", "good afternoon", "good evening", "bye",
    }
    if cleaned in chitchat_exact:
        return True
    words = cleaned.split()
    if len(words) <= 5 and all(w in {"hi", "hello", "hey", "how", "are", "you", "thanks", "thank", "ok", "okay", "good"} for w in words):
        return True
    return False


@dataclass(frozen=True)
class ContextAssessment:
    """Complete Stage 3.6 server-computed context classification and material context state."""
    contract: ContextCompatibilityContract
    effective_query: str
    prior_messages_count: int
    relevant_context_hint_text: str = ""
    contains_sensitive_material: bool = False
    contains_personal_context: bool = False
    ignored_client_context_override: bool = False

    @property
    def context_class(self) -> ContextClass:
        return self.contract.context_class

    @property
    def context_fingerprint(self) -> Optional[str]:
        return self.contract.context_fingerprint


@dataclass(frozen=True)
class ScopeResolution:
    """Resolved cache storage/lookup visibility & ownership invariant shared by exact and semantic stores."""
    effective_visibility: str
    effective_owner: Optional[str]
    allow_shared_reads: bool
    can_cache_exact: bool
    can_admit_semantic: bool
    exact_storage_key: Optional[str]
    privacy_rejection_reason: Optional[str] = None


def resolve_context_cache_scope(
    request: "GenerationRequest",
    assessment: ContextAssessment,
    fingerprint: str,
) -> ScopeResolution:
    """
    Computes identical visibility, owner_principal_id, and exact storage key for both
    exact_cache persistence/lookup and semantic vector/variant persistence/lookup so the
    PostgreSQL FK (cache_namespace, fingerprint) -> cache_entries never diverges while
    guaranteeing conversation-bound or personal context-dependent records stay isolated.
    """
    req_vis = normalize_cache_visibility(getattr(request, "visibility", CACHE_VISIBILITY_PRIVATE))
    req_own = normalize_principal_id(getattr(request, "principal_id", None))
    allow_shared = bool(getattr(request, "allow_shared_reads", False))
    conv_id = (getattr(request, "conversation_id", None) or "").strip()
    ctx_class = assessment.context_class

    if req_vis == CACHE_VISIBILITY_UNKNOWN:
        return ScopeResolution(
            effective_visibility=CACHE_VISIBILITY_UNKNOWN,
            effective_owner=None,
            allow_shared_reads=False,
            can_cache_exact=False,
            can_admit_semantic=False,
            exact_storage_key=None,
            privacy_rejection_reason="VISIBILITY_MISSING_OR_UNKNOWN",
        )

    if req_vis == CACHE_VISIBILITY_PRIVATE:
        if req_own is None:
            return ScopeResolution(
                effective_visibility=CACHE_VISIBILITY_PRIVATE,
                effective_owner=None,
                allow_shared_reads=False,
                can_cache_exact=False,
                can_admit_semantic=False,
                exact_storage_key=None,
                privacy_rejection_reason="PRINCIPAL_IDENTITY_MISSING",
            )
        exact_key = derive_scoped_exact_key(fingerprint, CACHE_VISIBILITY_PRIVATE, req_own)
        if assessment.contains_sensitive_material:
            return ScopeResolution(
                effective_visibility=CACHE_VISIBILITY_PRIVATE,
                effective_owner=req_own,
                allow_shared_reads=False,
                can_cache_exact=True,
                can_admit_semantic=False,
                exact_storage_key=exact_key,
                privacy_rejection_reason="ADMISSION_REJECTED_CONTEXT_PRIVACY",
            )
        can_admit = ctx_class != ContextClass.CONTEXT_UNCERTAIN
        reason = "ADMISSION_REJECTED_CONTEXT_UNCERTAIN" if not can_admit else None
        return ScopeResolution(
            effective_visibility=CACHE_VISIBILITY_PRIVATE,
            effective_owner=req_own,
            allow_shared_reads=bool(allow_shared and not assessment.contains_personal_context),
            can_cache_exact=True,
            can_admit_semantic=can_admit,
            exact_storage_key=exact_key,
            privacy_rejection_reason=reason,
        )

    # req_vis == CACHE_VISIBILITY_TENANT_SHARED
    if assessment.contains_sensitive_material:
        exact_key = derive_scoped_exact_key(fingerprint, CACHE_VISIBILITY_TENANT_SHARED, None)
        return ScopeResolution(
            effective_visibility=CACHE_VISIBILITY_TENANT_SHARED,
            effective_owner=None,
            allow_shared_reads=False,
            can_cache_exact=True,
            can_admit_semantic=False,
            exact_storage_key=exact_key,
            privacy_rejection_reason="ADMISSION_REJECTED_CONTEXT_PRIVACY",
        )

    needs_private_isolation = assessment.contains_personal_context or (
        ctx_class == ContextClass.CONTEXT_DEPENDENT and bool(conv_id)
    )
    if needs_private_isolation:
        if req_own is not None:
            exact_key = derive_scoped_exact_key(fingerprint, CACHE_VISIBILITY_PRIVATE, req_own)
            can_admit = ctx_class != ContextClass.CONTEXT_UNCERTAIN
            reason = "ADMISSION_REJECTED_CONTEXT_UNCERTAIN" if not can_admit else None
            return ScopeResolution(
                effective_visibility=CACHE_VISIBILITY_PRIVATE,
                effective_owner=req_own,
                allow_shared_reads=False,
                can_cache_exact=True,
                can_admit_semantic=can_admit,
                exact_storage_key=exact_key,
                privacy_rejection_reason=reason,
            )
        exact_key = derive_scoped_exact_key(fingerprint, CACHE_VISIBILITY_TENANT_SHARED, None)
        return ScopeResolution(
            effective_visibility=CACHE_VISIBILITY_TENANT_SHARED,
            effective_owner=None,
            allow_shared_reads=False,
            can_cache_exact=True,
            can_admit_semantic=False,
            exact_storage_key=exact_key,
            privacy_rejection_reason="ADMISSION_REJECTED_CONTEXT_PRIVACY",
        )

    exact_key = derive_scoped_exact_key(fingerprint, CACHE_VISIBILITY_TENANT_SHARED, None)
    can_admit = ctx_class != ContextClass.CONTEXT_UNCERTAIN
    reason = "ADMISSION_REJECTED_CONTEXT_UNCERTAIN" if not can_admit else None
    return ScopeResolution(
        effective_visibility=CACHE_VISIBILITY_TENANT_SHARED,
        effective_owner=None,
        allow_shared_reads=False,
        can_cache_exact=True,
        can_admit_semantic=can_admit,
        exact_storage_key=exact_key,
        privacy_rejection_reason=reason,
    )


def compute_source_conversation_digest(request: "GenerationRequest") -> str:
    """
    Computes an opaque, non-reversible identifier distinguishing independent source
    conversations/contexts without exposing raw conversation_id or transcripts.
    """
    conv_id = (getattr(request, "conversation_id", None) or "").strip()
    if conv_id:
        digest = hashlib.sha256(f"conv:{conv_id}".encode("utf-8")).hexdigest()[:24]
        return f"conv:{digest}"
    msgs = getattr(request, "messages", None) or []
    prior_serial = [
        {"role": getattr(m, "role", ""), "content": _normalize_text(getattr(m, "content", ""))}
        for m in msgs[:-1]
    ]
    digest = hashlib.sha256(
        json.dumps(prior_serial, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:24]
    return f"stateless:{digest}"


def _extract_prior_context_materials(
    prior_messages: Sequence[Any],
    system_texts: Sequence[str],
) -> Dict[str, Any]:
    """
    Extracts normalized, canonicalizable response-affecting artifacts from system instructions
    and prior conversation turns while ignoring filler greetings and formatting noise.
    """
    domains: Set[str] = set()
    material_payload: Dict[str, Any] = {}
    hint_fragments: List[str] = []
    identifiers: Set[str] = set()
    schema_entities: Set[str] = set()
    config_keys: Set[str] = set()
    personal_detected = False
    sensitive_detected = False

    norm_system = [_normalize_text(s) for s in system_texts if _normalize_text(s)]
    if norm_system:
        domains.add("system")
        material_payload["system"] = norm_system
        hint_fragments.extend(norm_system)
        for s in norm_system:
            identifiers.update(_extract_identifiers(s))
            if contains_sensitive_material(s):
                sensitive_detected = True
            if _PERSONAL_CONTEXT_RE.search(s):
                personal_detected = True

    schema_blocks: List[str] = []
    code_blocks: List[str] = []
    config_blocks: List[str] = []
    decision_blocks: List[str] = []
    substantive_turns: List[Dict[str, str]] = []
    last_assistant_artifact: Optional[str] = None

    for msg in prior_messages:
        role = str(getattr(msg, "role", "")).strip().lower()
        raw_content = getattr(msg, "content", "")
        content = _normalize_text(raw_content)
        if not content:
            continue
        if contains_sensitive_material(content):
            sensitive_detected = True
        if _PERSONAL_CONTEXT_RE.search(content):
            personal_detected = True
        if role == "system":
            continue
        if _is_chitchat_turn(content):
            continue

        msg_has_ddl = bool(_DDL_SCHEMA_RE.search(content))
        msg_has_code = _has_code_construct(content)
        msg_has_cfg = bool(_CONFIG_SIGNAL_RE.search(content) or re.search(r"\b(?:timeout|pool_size|retries|backoff|port|host|env)\b", content, re.IGNORECASE))
        msg_has_dec = bool(_DECISION_SIGNAL_RE.search(content))

        if msg_has_ddl:
            domains.add("schema")
            schema_blocks.append(content)
            schema_entities.update(_extract_table_and_column_tokens(content))
            hint_fragments.append(content)
        if msg_has_code:
            domains.add("code")
            code_blocks.append(content)
            hint_fragments.append(content)
        if msg_has_cfg and not msg_has_ddl:
            domains.add("config")
            config_blocks.append(content)
            config_keys.update(re.findall(r"\b([a-zA-Z_][a-zA-Z0-9_]{2,})\s*[:=]", content.lower()))
            hint_fragments.append(content)
        if msg_has_dec:
            domains.add("decisions")
            domains.add("framework")
            domains.add("constraints")
            decision_blocks.append(content)
            hint_fragments.append(content)

        if msg_has_ddl or msg_has_code or msg_has_cfg or msg_has_dec:
            identifiers.update(_extract_identifiers(content))
            substantive_turns.append({"role": role, "content": content})

        if role == "assistant" and (msg_has_ddl or msg_has_code or msg_has_cfg or msg_has_dec or len(content) >= 24):
            last_assistant_artifact = content

    if schema_blocks:
        material_payload["schema"] = schema_blocks
    if code_blocks:
        material_payload["code"] = code_blocks
    if config_blocks:
        material_payload["config"] = config_blocks
    if decision_blocks:
        material_payload["decisions"] = decision_blocks
    if last_assistant_artifact is not None:
        domains.add("assistant_artifact")
        material_payload["assistant_artifact_digest"] = hashlib.sha256(
            last_assistant_artifact.encode("utf-8")
        ).hexdigest()
        hint_fragments.append(last_assistant_artifact)

    # Preserve canonical turn order hash over substantive technical turns only (excludes greetings/filler)
    if substantive_turns:
        material_payload["substantive_turns"] = substantive_turns

    return {
        "domains": tuple(sorted(domains)),
        "material_payload": material_payload,
        "hint_text": "\n".join(hint_fragments),
        "identifiers": identifiers,
        "schema_entities": schema_entities,
        "config_keys": config_keys,
        "personal_detected": personal_detected,
        "sensitive_detected": sensitive_detected,
        "has_substantive_history": bool(substantive_turns or norm_system or last_assistant_artifact),
    }


def _compute_context_fingerprint(material_payload: Dict[str, Any]) -> str:
    canonical = {
        "context_version": CONTEXT_CONTRACT_SCHEMA_VERSION,
        "materials": material_payload,
    }
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    return f"ctx-v1:{digest}"


def _is_prior_material_relevant_to_query(
    effective_query: str,
    extracted: Dict[str, Any],
) -> Tuple[bool, str]:
    """
    Determines whether non-empty extracted prior context materially affects the
    correctness of an apparently standalone query, or is demonstrably unrelated.
    """
    domains: Tuple[str, ...] = extracted["domains"]
    if "system" in domains:
        return True, "MATERIAL_SYSTEM_INSTRUCTIONS"
    if "decisions" in domains or "constraints" in domains:
        return True, "MATERIAL_CONVERSATION_DECISIONS"

    q_lower = effective_query.lower()
    q_words = set(re.findall(r"[a-zA-Z_][a-zA-Z0-9_]{1,}", q_lower))

    # Schema overlap: prior DDL + SQL/query/table/record/column terminology or entity match
    if "schema" in domains:
        if q_words.intersection(_SQL_QUERY_DOMAIN_TOKENS) or q_words.intersection(extracted["schema_entities"]):
            return True, "MATERIAL_DATABASE_SCHEMA"

    # Config overlap: prior config + config/setting/parameter vocabulary or key match
    if "config" in domains:
        if q_words.intersection(_CONFIG_DOMAIN_TOKENS) or q_words.intersection(extracted["config_keys"]):
            return True, "MATERIAL_CONFIGURATION_CONTEXT"

    # Shared non-generic code/entity identifiers across prior implementation and query
    q_ids = _extract_identifiers(effective_query)
    shared_ids = q_ids.intersection(extracted["identifiers"])
    if shared_ids and ("code" in domains or "schema" in domains or "config" in domains):
        return True, "MATERIAL_SHARED_ENTITY_IDENTIFIERS"

    # If effective query mentions generic implementation nouns ("the function", "the query", "it", etc.)
    if re.search(r"\b(?:the\s+(?:function|class|method|script|snippet|query|table|endpoint|service|config|error|output))\b", q_lower):
        if "code" in domains or "schema" in domains or "config" in domains or "assistant_artifact" in domains:
            return True, "MATERIAL_IMPLICIT_PRIOR_ARTIFACT"

    # If query is clearly self-contained conceptual/standalone ("How do I reverse a string in Python?", etc.)
    # and shares zero material domains/identifiers/directives with prior turns, allow CONTEXT_INDEPENDENT.
    is_clear_standalone = (
        any(q_lower.startswith(starter) for starter in _CONCEPTUAL_SELF_CONTAINED_STARTERS)
        and not _HARD_BACKREF_RE.search(effective_query)
        and not _DEICTIC_ACTION_RE.search(effective_query)
        and not _EXTERNAL_POSSESSIVE_ENTITY_RE.search(effective_query)
    )
    if is_clear_standalone and not shared_ids:
        return False, "IRRELEVANT_PRIOR_HISTORY_EXCLUDED"

    # Non-trivial prior code/assistant artifact + non-clear-standalone follow-up -> treat conservatively as material context
    if extracted["has_substantive_history"]:
        return True, "CONSERVATIVE_PRIOR_CONTEXT_COUPLING"

    return False, "NON_MATERIAL_PRIOR_CHATTER"


def analyze_request_context(request: "GenerationRequest") -> ContextAssessment:
    """
    Deterministically classifies a GenerationRequest into CONTEXT_INDEPENDENT,
    CONTEXT_DEPENDENT, or CONTEXT_UNCERTAIN and computes a canonical context
    fingerprint when CONTEXT_DEPENDENT. Never reads untrusted client context fields.
    """
    messages = list(getattr(request, "messages", None) or [])
    req_meta_extra = getattr(request, "metadata", None)
    if not isinstance(req_meta_extra, dict):
        req_meta_extra = {}

    ignored_client_override = any(k in req_meta_extra for k in CLIENT_FORBIDDEN_CONTEXT_KEYS)

    # Locate the latest non-empty user message as the effective query
    last_user_idx = -1
    for idx in range(len(messages) - 1, -1, -1):
        m = messages[idx]
        if getattr(m, "role", None) == "user" and _normalize_text(getattr(m, "content", "")):
            last_user_idx = idx
            break

    if last_user_idx < 0:
        contract = ContextCompatibilityContract(
            context_class=ContextClass.CONTEXT_UNCERTAIN,
            context_fingerprint=None,
            material_domains=(),
            has_system_instructions=False,
            history_turn_count=0,
            source_conversation_present=bool((getattr(request, "conversation_id", None) or "").strip()),
            eligible_for_shared_reuse=False,
            classification_reason="NO_EFFECTIVE_USER_MESSAGE",
        )
        return ContextAssessment(
            contract=contract,
            effective_query="",
            prior_messages_count=len(messages),
            ignored_client_context_override=ignored_client_override,
        )

    effective_query = _normalize_text(messages[last_user_idx].content)
    prior_messages = messages[:last_user_idx]

    system_texts: List[str] = []
    for m in messages:
        if getattr(m, "role", None) == "system":
            norm_s = _normalize_text(getattr(m, "content", ""))
            if norm_s:
                system_texts.append(norm_s)
    raw_sys_meta = req_meta_extra.get("system_prompt")
    if isinstance(raw_sys_meta, str) and _normalize_text(raw_sys_meta):
        norm_meta_sys = _normalize_text(raw_sys_meta)
        if norm_meta_sys not in system_texts:
            system_texts.append(norm_meta_sys)

    history_turn_count = sum(1 for m in prior_messages if getattr(m, "role", None) == "user")
    conv_present = bool((getattr(request, "conversation_id", None) or "").strip())
    has_system = bool(system_texts)

    # Guard 1: Server-side ContextBuilder marked history compacted or incomplete -> always CONTEXT_UNCERTAIN
    ctx_status = str(req_meta_extra.get("context_status") or "").strip().lower()
    if ctx_status in ("compacted", "incomplete"):
        contract = ContextCompatibilityContract(
            context_class=ContextClass.CONTEXT_UNCERTAIN,
            context_fingerprint=None,
            material_domains=(),
            has_system_instructions=has_system,
            history_turn_count=history_turn_count,
            source_conversation_present=conv_present,
            eligible_for_shared_reuse=False,
            classification_reason=f"CONTEXT_STATUS_{ctx_status.upper()}",
        )
        return ContextAssessment(
            contract=contract,
            effective_query=effective_query,
            prior_messages_count=len(prior_messages),
            contains_sensitive_material=any(contains_sensitive_material(getattr(m, "content", "")) for m in messages),
            ignored_client_context_override=ignored_client_override,
        )

    extracted = _extract_prior_context_materials(prior_messages, system_texts)
    sensitive_here = bool(extracted["sensitive_detected"] or contains_sensitive_material(effective_query))
    personal_here = bool(extracted["personal_detected"] or _PERSONAL_CONTEXT_RE.search(effective_query))

    has_inline = _has_inline_artifact(effective_query)
    has_hard_backref = bool(_HARD_BACKREF_RE.search(effective_query))
    has_deictic = bool(_DEICTIC_ACTION_RE.search(effective_query)) and not has_inline
    has_external_ref = bool(_EXTERNAL_POSSESSIVE_ENTITY_RE.search(effective_query)) and not has_inline

    is_explicit_followup = has_hard_backref or has_deictic or has_external_ref

    # Guard 2: Follow-up / back-reference query checks
    if is_explicit_followup:
        # Check for non-empty substantive prior context; missing or empty/chitchat history -> CONTEXT_UNCERTAIN
        if not prior_messages or not extracted["has_substantive_history"]:
            # If only a non-trivial assistant text existed in prior_messages (e.g. prose answer), allow?
            # Pure chitchat ("hi") or empty prior_messages has NO substantive technical referent -> fail closed!
            contract = ContextCompatibilityContract(
                context_class=ContextClass.CONTEXT_UNCERTAIN,
                context_fingerprint=None,
                material_domains=extracted["domains"],
                has_system_instructions=has_system,
                history_turn_count=history_turn_count,
                source_conversation_present=conv_present,
                eligible_for_shared_reuse=False,
                classification_reason="FOLLOWUP_MISSING_OR_UNVERIFIABLE_HISTORY",
            )
            return ContextAssessment(
                contract=contract,
                effective_query=effective_query,
                prior_messages_count=len(prior_messages),
                contains_sensitive_material=sensitive_here,
                contains_personal_context=personal_here,
                ignored_client_context_override=ignored_client_override,
            )

        ctx_fp = _compute_context_fingerprint(extracted["material_payload"])
        contract = ContextCompatibilityContract(
            context_class=ContextClass.CONTEXT_DEPENDENT,
            context_fingerprint=ctx_fp,
            material_domains=extracted["domains"],
            has_system_instructions=has_system,
            history_turn_count=history_turn_count,
            source_conversation_present=conv_present,
            eligible_for_shared_reuse=False,
            classification_reason="EXPLICIT_FOLLOWUP_WITH_PRIOR_CONTEXT",
        )
        return ContextAssessment(
            contract=contract,
            effective_query=effective_query,
            prior_messages_count=len(prior_messages),
            relevant_context_hint_text=extracted["hint_text"],
            contains_sensitive_material=sensitive_here,
            contains_personal_context=personal_here,
            ignored_client_context_override=ignored_client_override,
        )

    # Guard 3: Prior history or system instructions exist without explicit deictic phrasing
    if prior_messages or system_texts:
        is_material, mat_reason = _is_prior_material_relevant_to_query(effective_query, extracted)
        if is_material:
            if not extracted["material_payload"]:
                contract = ContextCompatibilityContract(
                    context_class=ContextClass.CONTEXT_UNCERTAIN,
                    context_fingerprint=None,
                    material_domains=extracted["domains"],
                    has_system_instructions=has_system,
                    history_turn_count=history_turn_count,
                    source_conversation_present=conv_present,
                    eligible_for_shared_reuse=False,
                    classification_reason="MATERIAL_CONTEXT_WITHOUT_STRUCTURAL_ARTIFACTS",
                )
                return ContextAssessment(
                    contract=contract,
                    effective_query=effective_query,
                    prior_messages_count=len(prior_messages),
                    contains_sensitive_material=sensitive_here,
                    contains_personal_context=personal_here,
                    ignored_client_context_override=ignored_client_override,
                )

            ctx_fp = _compute_context_fingerprint(extracted["material_payload"])
            contract = ContextCompatibilityContract(
                context_class=ContextClass.CONTEXT_DEPENDENT,
                context_fingerprint=ctx_fp,
                material_domains=extracted["domains"],
                has_system_instructions=has_system,
                history_turn_count=history_turn_count,
                source_conversation_present=conv_present,
                eligible_for_shared_reuse=False,
                classification_reason=mat_reason,
            )
            return ContextAssessment(
                contract=contract,
                effective_query=effective_query,
                prior_messages_count=len(prior_messages),
                relevant_context_hint_text=extracted["hint_text"],
                contains_sensitive_material=sensitive_here,
                contains_personal_context=personal_here,
                ignored_client_context_override=ignored_client_override,
            )

        # Prior turns were greetings or demonstrably irrelevant to this self-contained technical question
        contract = ContextCompatibilityContract(
            context_class=ContextClass.CONTEXT_INDEPENDENT,
            context_fingerprint=CONTEXT_INDEPENDENT_SENTINEL,
            material_domains=(),
            has_system_instructions=False,
            history_turn_count=history_turn_count,
            source_conversation_present=conv_present,
            eligible_for_shared_reuse=not sensitive_here and not personal_here,
            classification_reason=mat_reason,
        )
        return ContextAssessment(
            contract=contract,
            effective_query=effective_query,
            prior_messages_count=len(prior_messages),
            contains_sensitive_material=sensitive_here,
            contains_personal_context=personal_here,
            ignored_client_context_override=ignored_client_override,
        )

    # Guard 4: Pure single-turn query with zero back-references and zero system instructions
    contract = ContextCompatibilityContract(
        context_class=ContextClass.CONTEXT_INDEPENDENT,
        context_fingerprint=CONTEXT_INDEPENDENT_SENTINEL,
        material_domains=(),
        has_system_instructions=False,
        history_turn_count=0,
        source_conversation_present=conv_present,
        eligible_for_shared_reuse=not sensitive_here and not personal_here,
        classification_reason="STANDALONE_TECHNICAL",
    )
    return ContextAssessment(
        contract=contract,
        effective_query=effective_query,
        prior_messages_count=0,
        contains_sensitive_material=sensitive_here,
        contains_personal_context=personal_here,
        ignored_client_context_override=ignored_client_override,
    )
