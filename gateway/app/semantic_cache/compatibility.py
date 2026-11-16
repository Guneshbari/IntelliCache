"""
Deterministic compatibility extraction and validation engine for IntelliCache (Stage 3.1).
Extracts, normalizes, and compares technical query requirements without external LLM calls.
Enforces that domain incompatibility (language, framework, runtime, version, constraints,
output format) cannot be overridden by dense vector similarity.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Sequence, Tuple

from .contracts import (
    CompatibilityReasonCode,
    ConstraintItem,
    DimensionValue,
    EmbeddingContract,
    GenerationContract,
    MetadataStatus,
    REQUIRED_DIMENSIONS,
    SemanticCompatibilityMetadata,
    TaskKind,
)

if TYPE_CHECKING:
    from ..core.contracts import GenerationRequest


class CompatibilityExtractor:
    """
    Deterministic requirement extractor.
    Extracts structured domain dimensions from query text and request parameters.
    Ensures identical text and extractor version always yield identical metadata.
    """

    EXTRACTOR_VERSION = "compat-extractor-v1"

    FRAMEWORK_DEFAULT_LANGUAGES: Dict[str, str] = {
        "react": "javascript",
        "vue": "javascript",
        "angular": "typescript",
        "next.js": "javascript",
        "express": "javascript",
        "fastapi": "python",
        "flask": "python",
        "django": "python",
        "spring": "java",
    }

    def extract(self, request: GenerationRequest, configured_model: str) -> SemanticCompatibilityMetadata:
        """Extracts complete versioned metadata from a GenerationRequest."""
        query_text = ""
        if request.messages:
            query_text = request.messages[0].content or ""

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
        max_tokens: Optional[int] = 1000,
        configured_model: str = "nomic-embed-text",
    ) -> SemanticCompatibilityMetadata:
        """Extracts metadata from raw query text and generation parameters."""
        task_kind = self._detect_task_kind(text)
        framework = self._detect_framework(text)
        framework_val = framework.value if framework.status == MetadataStatus.KNOWN else None

        language = self._detect_language(text, task_kind.value, framework_val)
        language_val = language.value if language.status == MetadataStatus.KNOWN else None

        lang_ver, fw_ver = self._detect_versions(text, language_val, framework_val)
        runtime = self._detect_runtime(text)
        execution_model = self._detect_execution_model(text)
        output_format = self._detect_output_format(text)
        constraints = self._detect_constraints(text)

        dimensions: Dict[str, DimensionValue] = {
            "language": language,
            "language_version": lang_ver,
            "framework": framework,
            "framework_version": fw_ver,
            "runtime": runtime,
            "execution_model": execution_model,
            "output_format": output_format,
            "constraints": constraints,
        }

        generation_contract = GenerationContract(
            provider=provider,
            model=model,
            temperature=float(temperature),
            max_tokens=max_tokens,
        )

        embedding_contract = EmbeddingContract(
            model=configured_model,
        )

        return SemanticCompatibilityMetadata(
            schema_version=1,
            extractor_version=self.EXTRACTOR_VERSION,
            task_kind=task_kind,
            dimensions=dimensions,
            generation_contract=generation_contract,
            embedding_contract=embedding_contract,
        )

    def _detect_task_kind(self, text: str) -> DimensionValue:
        lower = text.lower()

        # Debugging and error questions
        error_indicators = (
            "traceback", "exception", "stacktrace", "syntax error", "type error",
            "typeerror", "referenceerror", "nullpointer", "segfault", "fix error",
            "why do i get ", "error:",
        )
        if any(k in lower for k in error_indicators):
            return DimensionValue(status=MetadataStatus.KNOWN, value=TaskKind.DEBUGGING_EXPLANATION)

        # Conceptual questions ("what is", "difference between", "how does", "why does")
        # unless code/syntax/command/function is asked
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

        # Code generation / implementation questions
        return DimensionValue(status=MetadataStatus.KNOWN, value=TaskKind.CODE_GENERATION)

    def _detect_framework(self, text: str) -> DimensionValue:
        lower = text.lower()
        if re.search(r"\breact(?:\.js|js)?\b", lower):
            return DimensionValue(status=MetadataStatus.KNOWN, value="react")
        if re.search(r"\bvue(?:\.js|js)?\b", lower):
            return DimensionValue(status=MetadataStatus.KNOWN, value="vue")
        if re.search(r"\bangular(?:\.js|js)?\b", lower):
            return DimensionValue(status=MetadataStatus.KNOWN, value="angular")
        if re.search(r"\bnext(?:\.js|js)?\b", lower):
            return DimensionValue(status=MetadataStatus.KNOWN, value="next.js")
        if re.search(r"\bfastapi\b", lower):
            return DimensionValue(status=MetadataStatus.KNOWN, value="fastapi")
        if re.search(r"\bflask\b", lower):
            return DimensionValue(status=MetadataStatus.KNOWN, value="flask")
        if re.search(r"\bdjango\b", lower):
            return DimensionValue(status=MetadataStatus.KNOWN, value="django")
        if re.search(r"\bexpress(?:\.js|js)?\b", lower):
            return DimensionValue(status=MetadataStatus.KNOWN, value="express")
        if re.search(r"\bspring(?:\s+boot)?\b", lower):
            return DimensionValue(status=MetadataStatus.KNOWN, value="spring")
        if re.search(r"\bdocker(?:file)?\b", lower):
            return DimensionValue(status=MetadataStatus.KNOWN, value="docker")
        if re.search(r"\b(?:kubernetes|k8s)\b", lower):
            return DimensionValue(status=MetadataStatus.KNOWN, value="kubernetes")

        return DimensionValue(status=MetadataStatus.NOT_APPLICABLE, value=None)

    def _detect_language(self, text: str, task_kind: str, framework: Optional[str]) -> DimensionValue:
        lower = text.lower()

        # 1. Disambiguate Go language
        if re.search(r"\bgolang\b", lower):
            return DimensionValue(status=MetadataStatus.KNOWN, value="go")
        if re.search(r"\b(?:in|using|with)\s+go\b|\bgo\s+(?:code|lang|language|slice|slices|routine|routines|channel|channels|struct|structs|func|package|module|program)\b|\bgoroutine", lower):
            # Guard against English phrases: "how to go to", "let's go ahead", "where to go"
            if not re.search(r"\b(?:let\'?s|how\s+to|where\s+to|going\s+to)\s+go\s+(?:to|through|ahead|back|over)\b", lower):
                return DimensionValue(status=MetadataStatus.KNOWN, value="go")

        # 2. Disambiguate R language
        if re.search(r"\b(?:in|using|with)\s+r\b|\br\s+(?:language|lang|script|dataframe|data\.frame|package|packages|cran|stats)\b|\brstats\b", lower):
            return DimensionValue(status=MetadataStatus.KNOWN, value="r")

        # 3. Disambiguate C language
        if re.search(r"\b(?:in|using|with)\s+c\b|\bc\s+(?:language|lang|code|program|pointer|pointers|struct|structs|compiler|header)\b", lower):
            if not re.search(r"\bc\+\+|\bc#|\bobjective-c", lower):
                return DimensionValue(status=MetadataStatus.KNOWN, value="c")

        # 4. Canonical languages with word boundaries
        if re.search(r"\bpython(?:3)?\b|\bpy\b", lower):
            return DimensionValue(status=MetadataStatus.KNOWN, value="python")
        if re.search(r"\btypescript\b|\bts\b", lower):
            return DimensionValue(status=MetadataStatus.KNOWN, value="typescript")
        if re.search(r"\bjavascript\b|\bjs\b|\bnode(?:\.js)?\b|\bnodejs\b", lower):
            return DimensionValue(status=MetadataStatus.KNOWN, value="javascript")
        if re.search(r"\brust\b", lower):
            return DimensionValue(status=MetadataStatus.KNOWN, value="rust")
        if re.search(r"\bc\+\+|\bcpp\b", lower):
            return DimensionValue(status=MetadataStatus.KNOWN, value="c++")
        if re.search(r"\bc#|\bcsharp\b", lower):
            return DimensionValue(status=MetadataStatus.KNOWN, value="c#")
        if re.search(r"\bjava\b(?!\s*script)", lower):
            return DimensionValue(status=MetadataStatus.KNOWN, value="java")
        if re.search(r"\b(?:sql|postgresql|postgres|mysql|sqlite)\b", lower):
            return DimensionValue(status=MetadataStatus.KNOWN, value="sql")
        if re.search(r"\b(?:bash|shell|zsh)\b", lower):
            return DimensionValue(status=MetadataStatus.KNOWN, value="bash")
        if re.search(r"\bruby\b", lower):
            return DimensionValue(status=MetadataStatus.KNOWN, value="ruby")
        if re.search(r"\bphp\b", lower):
            return DimensionValue(status=MetadataStatus.KNOWN, value="php")
        if re.search(r"\bswift\b", lower):
            return DimensionValue(status=MetadataStatus.KNOWN, value="swift")
        if re.search(r"\bkotlin\b", lower):
            return DimensionValue(status=MetadataStatus.KNOWN, value="kotlin")
        if re.search(r"\bhtml\b", lower):
            return DimensionValue(status=MetadataStatus.KNOWN, value="html")
        if re.search(r"\bcss\b", lower):
            return DimensionValue(status=MetadataStatus.KNOWN, value="css")

        # 5. Distinct code syntax patterns in text
        if re.search(r"\bdef\s+[a-zA-Z_][a-zA-Z0-9_]*\s*\(", text) or ("import " in text and not "const " in text):
            if not ("fn " in text or "pub fn " in text):
                return DimensionValue(status=MetadataStatus.KNOWN, value="python")
        if re.search(r"\b(?:const|let|var)\s+[a-zA-Z_][a-zA-Z0-9_]*\s*=", text) or "console.log" in text or re.search(r"\bfunction(?:\s+[a-zA-Z_$][a-zA-Z0-9_$]*)?\s*\(", text):
            return DimensionValue(status=MetadataStatus.KNOWN, value="javascript")
        if re.search(r"\bfn\s+[a-zA-Z_][a-zA-Z0-9_]*\s*\(", text) or "pub fn " in text:
            return DimensionValue(status=MetadataStatus.KNOWN, value="rust")
        if re.search(r"\bSELECT\s+.+\s+FROM\b", text, re.IGNORECASE):
            return DimensionValue(status=MetadataStatus.KNOWN, value="sql")
        if "public static void main" in text or "System.out.println" in text:
            return DimensionValue(status=MetadataStatus.KNOWN, value="java")

        # 5. If framework is present, infer canonical ecosystem language
        if framework and framework in self.FRAMEWORK_DEFAULT_LANGUAGES:
            return DimensionValue(status=MetadataStatus.KNOWN, value=self.FRAMEWORK_DEFAULT_LANGUAGES[framework])

        # 6. Conceptual query without explicit language: NOT_APPLICABLE
        if task_kind == TaskKind.CONCEPTUAL:
            return DimensionValue(status=MetadataStatus.NOT_APPLICABLE, value=None)

        # 7. Code generation without language: UNKNOWN (material dimension fails closed)
        return DimensionValue(status=MetadataStatus.UNKNOWN, value=None)

    def _detect_versions(
        self,
        text: str,
        language: Optional[str],
        framework: Optional[str],
    ) -> Tuple[DimensionValue, DimensionValue]:
        lower = text.lower()

        # Language version
        lang_ver: Optional[str] = None
        if language == "python":
            m_py = re.search(r"\bpython\s*([23](?:\.[0-9]+)?)\b", lower)
            if m_py:
                lang_ver = m_py.group(1)
        elif language in ("javascript", "typescript"):
            m_node = re.search(r"\bnode(?:\.js)?\s*([0-9]{1,2}(?:\.[0-9]+)?)\b", lower)
            if m_node:
                lang_ver = m_node.group(1)

        # Framework version
        fw_ver: Optional[str] = None
        if framework:
            escaped_fw = re.escape(framework)
            m_fw = re.search(rf"\b{escaped_fw}\s*([0-9]+(?:\.[0-9]+)?)\b", lower)
            if m_fw:
                fw_ver = m_fw.group(1)

        lang_dim = (
            DimensionValue(status=MetadataStatus.KNOWN, value=lang_ver)
            if lang_ver
            else DimensionValue(status=MetadataStatus.NOT_APPLICABLE, value=None)
        )
        fw_dim = (
            DimensionValue(status=MetadataStatus.KNOWN, value=fw_ver)
            if fw_ver
            else DimensionValue(status=MetadataStatus.NOT_APPLICABLE, value=None)
        )
        return lang_dim, fw_dim

    def _detect_runtime(self, text: str) -> DimensionValue:
        lower = text.lower()
        has_node = bool(re.search(r"\bnode(?:\.js)?\b", lower))
        has_bun = bool(re.search(r"\bbun\b", lower))
        has_deno = bool(re.search(r"\bdeno\b", lower))
        has_cpython = bool(re.search(r"\bcpython\b", lower))
        has_pypy = bool(re.search(r"\bpypy\b", lower))

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

        if len(matches) > 1:
            return DimensionValue(status=MetadataStatus.UNKNOWN, value=None)
        if len(matches) == 1:
            return DimensionValue(status=MetadataStatus.KNOWN, value=matches[0])

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
            re.search(r"\b(?:as\s+(?:strict\s+)?json|in\s+(?:strict\s+)?json|format(?:ted)?\s+as\s+(?:strict\s+)?json|json\s+format|return\s+(?:strict\s+)?json|output\s+(?:as\s+)?(?:strict\s+)?json|strict\s+json|json\s+response|json\s+payload)\b", lower)
        )
        has_plain = bool(
            re.search(r"\b(?:plain\s*text|as\s+(?:plain\s*)?text|raw\s+text|no\s+markdown|without\s+markdown)\b", lower)
        )
        has_md = bool(
            re.search(r"\b(?:as\s+markdown|in\s+markdown|markdown\s+format)\b", lower)
        )

        matches = []
        if has_json:
            matches.append("json")
        if has_plain:
            matches.append("plain_text")
        if has_md:
            matches.append("markdown")

        if len(matches) > 1:
            return DimensionValue(status=MetadataStatus.UNKNOWN, value=None)
        if len(matches) == 1:
            return DimensionValue(status=MetadataStatus.KNOWN, value=matches[0])

        return DimensionValue(status=MetadataStatus.NOT_APPLICABLE, value=None)

    def _detect_constraints(self, text: str) -> DimensionValue:
        lower = text.lower()
        items: List[ConstraintItem] = []

        # 1. Case sensitivity
        has_cs = bool(re.search(r"\b(?:case[\s\-]sensitive|match\s+case|sensitive\s+to\s+case)\b", lower))
        has_ci = bool(re.search(r"\b(?:case[\s\-]insensitive|ignore\s+case|insensitive\s+to\s+case)\b", lower))
        if has_cs and has_ci:
            return DimensionValue(status=MetadataStatus.UNKNOWN, value=None)
        if has_cs:
            items.append(ConstraintItem(key="case_sensitivity", operator="equals", value="sensitive"))
        elif has_ci:
            items.append(ConstraintItem(key="case_sensitivity", operator="equals", value="insensitive"))

        # 2. Hidden items (include vs exclude)
        has_inc_hid = bool(re.search(r"\b(?:includ(?:e|ing)|show)\s+(?:\w+\s+)*hidden\b", lower))
        has_exc_hid = bool(re.search(r"\b(?:exclud(?:e|ing)|ignore|no|without)\s+(?:\w+\s+)*hidden\b", lower))
        if has_inc_hid and has_exc_hid:
            return DimensionValue(status=MetadataStatus.UNKNOWN, value=None)
        if has_inc_hid:
            items.append(ConstraintItem(key="hidden_items", operator="equals", value="include"))
        elif has_exc_hid:
            items.append(ConstraintItem(key="hidden_items", operator="equals", value="exclude"))

        # 3. Traversal (recursive vs flat)
        has_rec = bool(re.search(r"\brecursive(?:ly)?\b", lower))
        has_flat = bool(re.search(r"\b(?:non[\s\-]recursive|flat|shallow)\b", lower))
        if has_rec and has_flat:
            return DimensionValue(status=MetadataStatus.UNKNOWN, value=None)
        if has_rec:
            items.append(ConstraintItem(key="traversal", operator="equals", value="recursive"))
        elif has_flat:
            items.append(ConstraintItem(key="traversal", operator="equals", value="flat"))

        # 4. Mutation (in-place vs immutable)
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


def compare_compatibility(
    req_meta: SemanticCompatibilityMetadata,
    cand_meta: SemanticCompatibilityMetadata,
) -> Tuple[bool, str, str]:
    """
    Compares two SemanticCompatibilityMetadata instances deterministically.
    Returns:
        (is_compatible, reason_code, diagnostic_message)
    """
    # 1. Compare task kinds
    if req_meta.task_kind.status == MetadataStatus.KNOWN and cand_meta.task_kind.status == MetadataStatus.KNOWN:
        if req_meta.task_kind.value != cand_meta.task_kind.value:
            code = CompatibilityReasonCode.INCOMPATIBLE_TASK_KIND
            return False, code, f"{code}: Task kind mismatch: candidate '{cand_meta.task_kind.value}' != request '{req_meta.task_kind.value}'."

    # 2. Check dimensions
    dims_to_check = [
        ("language", CompatibilityReasonCode.LANGUAGE_MISMATCH),
        ("language_version", CompatibilityReasonCode.LANGUAGE_VERSION_MISMATCH),
        ("framework", CompatibilityReasonCode.FRAMEWORK_MISMATCH),
        ("framework_version", CompatibilityReasonCode.FRAMEWORK_VERSION_MISMATCH),
        ("runtime", CompatibilityReasonCode.RUNTIME_MISMATCH),
        ("execution_model", CompatibilityReasonCode.EXECUTION_MODEL_MISMATCH),
        ("output_format", CompatibilityReasonCode.OUTPUT_FORMAT_MISMATCH),
    ]

    for dim_name, mismatch_code in dims_to_check:
        r_dim = req_meta.dimensions.get(dim_name)
        c_dim = cand_meta.dimensions.get(dim_name)

        if not r_dim or not c_dim:
            code = CompatibilityReasonCode.INVALID_METADATA_FORMAT
            return False, code, f"{code}: Missing dimension '{dim_name}' in metadata."

        # UNKNOWN check for material dimensions:
        if r_dim.status == MetadataStatus.UNKNOWN or c_dim.status == MetadataStatus.UNKNOWN:
            code = CompatibilityReasonCode.UNKNOWN_MATERIAL_DIMENSION
            return False, code, f"{code}: Dimension '{dim_name}' has UNKNOWN state and cannot be established."

        # KNOWN vs KNOWN
        if r_dim.status == MetadataStatus.KNOWN and c_dim.status == MetadataStatus.KNOWN:
            if str(r_dim.value).lower().strip() != str(c_dim.value).lower().strip():
                return False, mismatch_code, f"{mismatch_code}: Candidate {dim_name} '{c_dim.value}' != request '{r_dim.value}'."

        # KNOWN vs NOT_APPLICABLE
        elif r_dim.status == MetadataStatus.KNOWN and c_dim.status == MetadataStatus.NOT_APPLICABLE:
            return False, mismatch_code, f"{mismatch_code}: Request requires {dim_name}='{r_dim.value}' but candidate has NOT_APPLICABLE."

        elif r_dim.status == MetadataStatus.NOT_APPLICABLE and c_dim.status == MetadataStatus.KNOWN:
            # If candidate specifies a concrete dimension and request did not:
            # For language, if request task_kind is CODE_GENERATION, language must be KNOWN anyway.
            # If request task_kind is CONCEPTUAL and candidate specifies a language, candidate is over-specialized:
            if dim_name in ("language", "execution_model", "output_format"):
                return False, mismatch_code, f"{mismatch_code}: Candidate specifies {dim_name}='{c_dim.value}' not required by request."

    # 3. Compare constraints
    r_const = req_meta.dimensions.get("constraints")
    c_const = cand_meta.dimensions.get("constraints")
    if r_const and c_const:
        if r_const.status == MetadataStatus.UNKNOWN or c_const.status == MetadataStatus.UNKNOWN:
            code = CompatibilityReasonCode.UNKNOWN_MATERIAL_DIMENSION
            return False, code, f"{code}: Constraints dimension has UNKNOWN state."

        r_items: Dict[str, str] = {}
        if r_const.status == MetadataStatus.KNOWN and isinstance(r_const.value, list):
            for it in r_const.value:
                k = it.key if hasattr(it, "key") else it.get("key")
                v = it.value if hasattr(it, "value") else it.get("value")
                r_items[k] = v

        c_items: Dict[str, str] = {}
        if c_const.status == MetadataStatus.KNOWN and isinstance(c_const.value, list):
            for it in c_const.value:
                k = it.key if hasattr(it, "key") else it.get("key")
                v = it.value if hasattr(it, "value") else it.get("value")
                c_items[k] = v

        # Check for conflicting constraint keys
        all_keys = set(r_items.keys()).union(set(c_items.keys()))
        for k in all_keys:
            r_val = r_items.get(k)
            c_val = c_items.get(k)
            if r_val is not None and c_val is not None:
                if r_val != c_val:
                    code = CompatibilityReasonCode.CONSTRAINT_MISMATCH
                    return False, code, f"{code}: Conflicting constraint for '{k}': candidate '{c_val}' != request '{r_val}'."
            elif r_val is not None and c_val is None:
                code = CompatibilityReasonCode.CONSTRAINT_MISMATCH
                return False, code, f"{code}: Request requires constraint '{k}={r_val}' not satisfied by candidate."
            elif r_val is None and c_val is not None:
                code = CompatibilityReasonCode.CONSTRAINT_MISMATCH
                return False, code, f"{code}: Candidate enforces constraint '{k}={c_val}' not requested."

    return True, CompatibilityReasonCode.COMPATIBLE, f"{CompatibilityReasonCode.COMPATIBLE}: Candidate satisfied all domain requirements."
