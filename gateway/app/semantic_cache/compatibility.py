"""
Deterministic compatibility extraction and validation engine for IntelliCache (Stage 3.1.1).
Extracts, normalizes, and compares technical query requirements without external LLM calls.
Enforces that domain incompatibility (language, framework, runtime, version, constraints,
output format) cannot be overridden by dense vector similarity.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Sequence, Set, Tuple

from .contracts import (
    CURRENT_SCHEMA_VERSION,
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

    EXTRACTOR_VERSION = "compat-extractor-v2"

    # Only frameworks with single, unambiguous implementation language associations
    FRAMEWORK_UNAMBIGUOUS_LANGUAGES: Dict[str, str] = {
        "fastapi": "python",
        "flask": "python",
        "django": "python",
        "spring": "java",
    }

    # Frameworks supporting both JavaScript and TypeScript requiring explicit evidence for code generation
    MULTI_LANGUAGE_FRAMEWORKS = frozenset({
        "react",
        "vue",
        "angular",
        "next.js",
        "express",
    })

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

        runtime = self._detect_runtime(text)
        runtime_val = runtime.value if runtime.status == MetadataStatus.KNOWN else None

        lang_ver, fw_ver, runtime_ver = self._detect_versions(text, language_val, framework_val, runtime_val)
        execution_model = self._detect_execution_model(text)
        output_format = self._detect_output_format(text)
        constraints = self._detect_constraints(text)

        dimensions: Dict[str, DimensionValue] = {
            "language": language,
            "language_version": lang_ver,
            "framework": framework,
            "framework_version": fw_ver,
            "runtime": runtime,
            "runtime_version": runtime_ver,
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
            schema_version=CURRENT_SCHEMA_VERSION,
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

        # React: guard against ordinary English ("people react", "chemical reaction", "body reacts")
        if re.search(r"\breact(?:\.js|js)\b", lower) or re.search(
            r"\breact\s+(?:component|components|hook|hooks|router|app|native|jsx|tsx|render|vdom|virtual\s+dom|props|state|context|query|element|elements|dev|developer)\b|\b(?:in|using|with)\s+react\b|\breact\s+1[6-9]\b",
            lower,
        ):
            if not re.search(r"\b(?:people|body|skin|chemicals?|how\s+will\s+they|users?)\s+react\b", lower):
                return DimensionValue(status=MetadataStatus.KNOWN, value="react")

        # Vue
        if re.search(r"\bvue(?:\.js|js)?\b", lower):
            return DimensionValue(status=MetadataStatus.KNOWN, value="vue")

        # Angular
        if re.search(r"\bangular(?:\.js|js)?\b", lower):
            return DimensionValue(status=MetadataStatus.KNOWN, value="angular")

        # Next.js: guard against ordinary English ("next step", "next time", "next line", "what is next")
        if re.search(r"\bnext(?:\.js|js)\b|\bnext\s+(?:1[3-5]|app\s+router|pages\s+router|router|server\s+action|server\s+component|auth)\b", lower):
            return DimensionValue(status=MetadataStatus.KNOWN, value="next.js")

        # FastAPI
        if re.search(r"\bfastapi\b", lower):
            return DimensionValue(status=MetadataStatus.KNOWN, value="fastapi")

        # Flask
        if re.search(r"\bflask\b", lower):
            return DimensionValue(status=MetadataStatus.KNOWN, value="flask")

        # Django
        if re.search(r"\bdjango\b", lower):
            return DimensionValue(status=MetadataStatus.KNOWN, value="django")

        # Express: guard against ordinary English ("express intent", "express my opinion", "express feeling", "express train")
        if re.search(r"\bexpress(?:\.js|js)\b|\bexpress\s+(?:app|server|router|middleware|route|routes|endpoint|endpoints)\b|\b(?:in|using|with)\s+express\b", lower):
            if not re.search(r"\b(?:express\s+(?:intent|opinion|feeling|feelings|gratitude|train|bus|delivery))\b", lower):
                return DimensionValue(status=MetadataStatus.KNOWN, value="express")

        # Spring: guard against ordinary English ("spring season", "spring cleaning", "spring into action")
        if re.search(r"\bspring\s+(?:boot|framework|security|data|mvc|cloud|batch|context|bean|beans)\b|\b@spring\b", lower):
            return DimensionValue(status=MetadataStatus.KNOWN, value="spring")

        # Container runtimes / frameworks
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
        if re.search(r"\bdef\s+[a-zA-Z_][a-zA-Z0-9_]*\s*\(", text) or ("import " in text and not "const " in text and not "from '" in text and not "from \"" in text):
            if not ("fn " in text or "pub fn " in text):
                return DimensionValue(status=MetadataStatus.KNOWN, value="python")
        if re.search(r"\b(?:const|let|var)\s+[a-zA-Z_][a-zA-Z0-9_]*\s*=", text) or "console.log" in text or re.search(r"\bfunction(?:\s+[a-zA-Z_$][a-zA-Z0-9_$]*)?\s*\(", text):
            # Check for TypeScript type annotations
            if re.search(r":\s*(?:string|number|boolean|any|void|never|unknown|Promise<.+>)\b", text):
                return DimensionValue(status=MetadataStatus.KNOWN, value="typescript")
            return DimensionValue(status=MetadataStatus.KNOWN, value="javascript")
        if re.search(r"\bfn\s+[a-zA-Z_][a-zA-Z0-9_]*\s*\(", text) or "pub fn " in text:
            return DimensionValue(status=MetadataStatus.KNOWN, value="rust")
        if re.search(r"\bSELECT\s+.+\s+FROM\b", text, re.IGNORECASE):
            return DimensionValue(status=MetadataStatus.KNOWN, value="sql")
        if "public static void main" in text or "System.out.println" in text:
            return DimensionValue(status=MetadataStatus.KNOWN, value="java")

        # 6. Unambiguous single-ecosystem frameworks (FastAPI -> Python, Spring -> Java)
        if framework and framework in self.FRAMEWORK_UNAMBIGUOUS_LANGUAGES:
            return DimensionValue(status=MetadataStatus.KNOWN, value=self.FRAMEWORK_UNAMBIGUOUS_LANGUAGES[framework])

        # 7. Multi-language frameworks (React, Vue, Next.js, Express, Angular):
        # Do NOT silently assume JavaScript for code generation without explicit evidence!
        if framework and framework in self.MULTI_LANGUAGE_FRAMEWORKS:
            if task_kind == TaskKind.CODE_GENERATION:
                return DimensionValue(status=MetadataStatus.UNKNOWN, value=None)
            if task_kind == TaskKind.CONCEPTUAL:
                return DimensionValue(status=MetadataStatus.NOT_APPLICABLE, value=None)

        # 8. Conceptual query without explicit language: NOT_APPLICABLE
        if task_kind == TaskKind.CONCEPTUAL:
            return DimensionValue(status=MetadataStatus.NOT_APPLICABLE, value=None)

        # 9. Code generation without language: UNKNOWN (material dimension fails closed)
        return DimensionValue(status=MetadataStatus.UNKNOWN, value=None)

    def _detect_versions(
        self,
        text: str,
        language: Optional[str],
        framework: Optional[str],
        runtime: Optional[str],
    ) -> Tuple[DimensionValue, DimensionValue, DimensionValue]:
        lower = text.lower()

        # 1. Language version (captures full patch version like 3.12.1, 3.12.9)
        lang_ver: Optional[str] = None
        if language == "python":
            m_py = re.search(r"\bpython\s*([23](?:\.[0-9]+)+|[23])\b", lower)
            if m_py:
                lang_ver = m_py.group(1)
        elif language == "java":
            m_java = re.search(r"\bjava\s*([0-9]+(?:\.[0-9]+)*)\b", lower)
            if m_java:
                lang_ver = m_java.group(1)
        elif language == "c++":
            m_cpp = re.search(r"\bc\+\+\s*(11|14|17|20|23)\b", lower)
            if m_cpp:
                lang_ver = m_cpp.group(1)

        # 2. Framework version (captures full patch version like 18.2.1, 18.2.4)
        fw_ver: Optional[str] = None
        if framework:
            escaped_fw = re.escape(framework)
            m_fw = re.search(rf"\b{escaped_fw}\s*([0-9]+(?:\.[0-9]+)*)\b", lower)
            if m_fw:
                fw_ver = m_fw.group(1)

        # 3. Runtime version (Node.js, Bun, Deno) - separated from language_version
        runtime_ver: Optional[str] = None
        if runtime == "node" or "node" in lower:
            m_node = re.search(r"\bnode(?:\.js)?\s*([0-9]+(?:\.[0-9]+)*)\b", lower)
            if m_node:
                runtime_ver = m_node.group(1)
        elif runtime == "bun" or "bun" in lower:
            m_bun = re.search(r"\bbun\s*([0-9]+(?:\.[0-9]+)*)\b", lower)
            if m_bun:
                runtime_ver = m_bun.group(1)
        elif runtime == "deno" or "deno" in lower:
            m_deno = re.search(r"\bdeno\s*([0-9]+(?:\.[0-9]+)*)\b", lower)
            if m_deno:
                runtime_ver = m_deno.group(1)

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
        runtime_dim = (
            DimensionValue(status=MetadataStatus.KNOWN, value=runtime_ver)
            if runtime_ver
            else DimensionValue(status=MetadataStatus.NOT_APPLICABLE, value=None)
        )
        return lang_dim, fw_dim, runtime_dim

    def _detect_runtime(self, text: str) -> DimensionValue:
        lower = text.lower()
        has_node = bool(re.search(r"\bnode(?:\.js)?\b", lower))
        has_bun = bool(re.search(r"\bbun\b", lower))
        has_deno = bool(re.search(r"\bdeno\b", lower))
        has_cpython = bool(re.search(r"\bcpython\b", lower))
        has_pypy = bool(re.search(r"\bpypy\b", lower))
        has_browser = bool(re.search(r"\b(?:browser|client[\s\-]side|dom|window\.document)\b", lower))

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
        if has_browser:
            matches.append("browser")

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

        # 1. Thread safety (FIX-04)
        has_not_thread_safe = bool(re.search(
            r"\b(?:non[\s\-]thread[\s\-]safe|not\s+thread[\s\-]safe|single[\s\-]threaded\s+only|"
            r"without\s+(?:a\s+)?(?:locks?|mutex(?:es)?|locking)|no\s+(?:locks?|mutex(?:es)?|locking)|"
            r"basic\s+singleton|naive\s+singleton)\b",
            lower,
        ))
        has_thread_safe = bool(re.search(
            r"\b(?:thread[\s\-]safe|threadsafe|multi[\s\-]threaded\s+safe|concurrency[\s\-]safe|"
            r"(?:with|using)\s+(?:a\s+)?(?:locks?|mutex(?:es)?|locking|threading\.lock)|"
            r"mutex\s+lock|double[\s\-]checked\s+locking)\b",
            lower,
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


def _evaluate_constraint_compatibility(
    req_item: ConstraintItem,
    cand_item: ConstraintItem,
) -> Tuple[bool, Optional[str]]:
    """
    Evaluates whether candidate constraint item satisfies the request constraint item.
    Supports 'equals', 'not_equals', 'in', 'not_in'.
    """
    r_op = req_item.operator
    c_op = cand_item.operator
    r_val = req_item.value
    c_val = cand_item.value

    def _parse_set(val: str) -> Set[str]:
        return {x.strip().lower() for x in val.split(",") if x.strip()}

    if r_op == "equals":
        if c_op == "equals":
            if r_val.lower() != c_val.lower():
                return False, f"Conflicting constraint for '{req_item.key}': candidate '{c_val}' != request '{r_val}'."
            return True, None
        elif c_op == "not_equals":
            if r_val.lower() == c_val.lower():
                return False, f"Conflicting constraint for '{req_item.key}': candidate explicitly forbids '{c_val}' which request requires."
            return False, f"Candidate has negative constraint '{cand_item.key} != {c_val}' but request requires explicit '{r_val}'."
        elif c_op == "in":
            c_set = _parse_set(c_val)
            if r_val.lower() not in c_set:
                return False, f"Request requires '{req_item.key}={r_val}' which is not in candidate allowed set {sorted(c_set)}."
            return True, None
        elif c_op == "not_in":
            c_set = _parse_set(c_val)
            if r_val.lower() in c_set:
                return False, f"Request requires '{req_item.key}={r_val}' which candidate explicitly excludes {sorted(c_set)}."
            return False, f"Candidate has negative constraint '{cand_item.key} not_in {sorted(c_set)}' but request requires explicit '{r_val}'."

    elif r_op == "not_equals":
        if c_op == "equals":
            if c_val.lower() == r_val.lower():
                return False, f"Candidate enforces '{cand_item.key}={c_val}' which request explicitly forbids."
            return True, None
        elif c_op == "not_equals":
            if c_val.lower() != r_val.lower():
                return False, f"Request forbids '{req_item.key}={r_val}' but candidate only forbids '{c_val}'."
            return True, None
        elif c_op == "in":
            c_set = _parse_set(c_val)
            if r_val.lower() in c_set:
                return False, f"Request forbids '{req_item.key}={r_val}' which is present in candidate allowed set {sorted(c_set)}."
            return True, None
        elif c_op == "not_in":
            c_set = _parse_set(c_val)
            if r_val.lower() not in c_set:
                return False, f"Candidate does not forbid '{req_item.key}={r_val}'."
            return True, None

    elif r_op == "in":
        r_set = _parse_set(r_val)
        if c_op == "equals":
            if c_val.lower() not in r_set:
                return False, f"Candidate value '{c_val}' is not in request allowed set {sorted(r_set)}."
            return True, None
        elif c_op == "in":
            c_set = _parse_set(c_val)
            if not c_set.issubset(r_set):
                return False, f"Candidate allowed set {sorted(c_set)} is not a subset of request allowed set {sorted(r_set)}."
            return True, None
        elif c_op in ("not_equals", "not_in"):
            return False, f"Request requires '{req_item.key} in {sorted(r_set)}' but candidate specifies negative constraint '{c_op} {c_val}'."

    elif r_op == "not_in":
        r_set = _parse_set(r_val)
        if c_op == "equals":
            if c_val.lower() in r_set:
                return False, f"Candidate value '{c_val}' is in request forbidden set {sorted(r_set)}."
            return True, None
        elif c_op == "not_in":
            c_set = _parse_set(c_val)
            if not r_set.issubset(c_set):
                return False, f"Candidate does not forbid all values in request forbidden set (missing {sorted(r_set - c_set)})."
            return True, None
        elif c_op == "not_equals":
            if not (len(r_set) == 1 and c_val.lower() in r_set):
                return False, f"Candidate only forbids '{c_val}', does not forbid complete request set {sorted(r_set)}."
            return True, None
        elif c_op == "in":
            c_set = _parse_set(c_val)
            if c_set.intersection(r_set):
                return False, f"Candidate allowed set {sorted(c_set)} overlaps with request forbidden set {sorted(r_set)}."
            return True, None

    return False, f"Unsupported constraint comparison for '{req_item.key}': request '{r_op}' vs candidate '{c_op}'."


def compare_compatibility(
    req_meta: SemanticCompatibilityMetadata,
    cand_meta: SemanticCompatibilityMetadata,
) -> Tuple[bool, str, str]:
    """
    Compares two SemanticCompatibilityMetadata instances deterministically.
    Enforces symmetric dimension-state comparison: no material dimension is treated
    as an automatic wildcard when one side has KNOWN and the other has NOT_APPLICABLE.

    Returns:
        (is_compatible, reason_code, diagnostic_message)
    """
    # 1. Compare task kinds (candidate task_kind MUST be KNOWN and valid)
    if cand_meta.task_kind.status != MetadataStatus.KNOWN:
        code = CompatibilityReasonCode.INCOMPATIBLE_TASK_KIND
        return False, code, f"{code}: Candidate task kind must be KNOWN, got {cand_meta.task_kind.status}."
    if cand_meta.task_kind.value not in TaskKind.ALL_VALUES:
        code = CompatibilityReasonCode.INVALID_METADATA_FORMAT
        return False, code, f"{code}: Unsupported candidate task kind value: '{cand_meta.task_kind.value}'."

    if req_meta.task_kind.status == MetadataStatus.UNKNOWN:
        code = CompatibilityReasonCode.UNKNOWN_MATERIAL_DIMENSION
        return False, code, f"{code}: Request task kind is UNKNOWN."

    if req_meta.task_kind.value != cand_meta.task_kind.value:
        code = CompatibilityReasonCode.INCOMPATIBLE_TASK_KIND
        return False, code, f"{code}: Task kind mismatch: candidate '{cand_meta.task_kind.value}' != request '{req_meta.task_kind.value}'."

    # 2. Check material dimensions (symmetric NOT_APPLICABLE vs KNOWN policy)
    dims_to_check = [
        ("language", CompatibilityReasonCode.LANGUAGE_MISMATCH),
        ("language_version", CompatibilityReasonCode.LANGUAGE_VERSION_MISMATCH),
        ("framework", CompatibilityReasonCode.FRAMEWORK_MISMATCH),
        ("framework_version", CompatibilityReasonCode.FRAMEWORK_VERSION_MISMATCH),
        ("runtime", CompatibilityReasonCode.RUNTIME_MISMATCH),
        ("runtime_version", CompatibilityReasonCode.RUNTIME_VERSION_MISMATCH),
        ("execution_model", CompatibilityReasonCode.EXECUTION_MODEL_MISMATCH),
        ("output_format", CompatibilityReasonCode.OUTPUT_FORMAT_MISMATCH),
    ]

    for dim_name, mismatch_code in dims_to_check:
        r_dim = req_meta.dimensions.get(dim_name)
        c_dim = cand_meta.dimensions.get(dim_name)

        if not r_dim or not c_dim:
            code = CompatibilityReasonCode.INVALID_METADATA_FORMAT
            return False, code, f"{code}: Missing dimension '{dim_name}' in metadata."

        # UNKNOWN check: if either side has UNKNOWN state, fail closed
        if r_dim.status == MetadataStatus.UNKNOWN or c_dim.status == MetadataStatus.UNKNOWN:
            code = CompatibilityReasonCode.UNKNOWN_MATERIAL_DIMENSION
            return False, code, f"{code}: Dimension '{dim_name}' has UNKNOWN state and cannot be established."

        # KNOWN vs KNOWN
        if r_dim.status == MetadataStatus.KNOWN and c_dim.status == MetadataStatus.KNOWN:
            if str(r_dim.value).lower().strip() != str(c_dim.value).lower().strip():
                return False, mismatch_code, f"{mismatch_code}: Candidate {dim_name} '{c_dim.value}' != request '{r_dim.value}'."

        # KNOWN vs NOT_APPLICABLE (request specifies dimension, candidate does not satisfy it)
        elif r_dim.status == MetadataStatus.KNOWN and c_dim.status == MetadataStatus.NOT_APPLICABLE:
            return False, mismatch_code, f"{mismatch_code}: Request requires {dim_name}='{r_dim.value}' but candidate has NOT_APPLICABLE."

        # NOT_APPLICABLE vs KNOWN (candidate specifies a material dimension not requested by request)
        elif r_dim.status == MetadataStatus.NOT_APPLICABLE and c_dim.status == MetadataStatus.KNOWN:
            return False, mismatch_code, f"{mismatch_code}: Candidate specifies {dim_name}='{c_dim.value}' not requested by request."

    # 3. Compare constraints
    r_const = req_meta.dimensions.get("constraints")
    c_const = cand_meta.dimensions.get("constraints")
    if r_const and c_const:
        if r_const.status == MetadataStatus.UNKNOWN or c_const.status == MetadataStatus.UNKNOWN:
            code = CompatibilityReasonCode.UNKNOWN_MATERIAL_DIMENSION
            return False, code, f"{code}: Constraints dimension has UNKNOWN state."

        r_items: Dict[str, ConstraintItem] = {}
        if r_const.status == MetadataStatus.KNOWN and isinstance(r_const.value, list):
            for it in r_const.value:
                item_obj = it if isinstance(it, ConstraintItem) else ConstraintItem.from_dict(it)
                r_items[item_obj.key] = item_obj

        c_items: Dict[str, ConstraintItem] = {}
        if c_const.status == MetadataStatus.KNOWN and isinstance(c_const.value, list):
            for it in c_const.value:
                item_obj = it if isinstance(it, ConstraintItem) else ConstraintItem.from_dict(it)
                c_items[item_obj.key] = item_obj

        # Check for conflicting or missing constraints
        all_keys = set(r_items.keys()).union(set(c_items.keys()))
        for k in all_keys:
            r_item = r_items.get(k)
            c_item = c_items.get(k)
            if r_item is not None and c_item is not None:
                compat, err = _evaluate_constraint_compatibility(r_item, c_item)
                if not compat:
                    code = CompatibilityReasonCode.CONSTRAINT_MISMATCH
                    return False, code, f"{code}: {err}"
            elif r_item is not None and c_item is None:
                code = CompatibilityReasonCode.CONSTRAINT_MISMATCH
                return False, code, f"{code}: Request requires constraint '{k} {r_item.operator} {r_item.value}' not satisfied by candidate."
            elif r_item is None and c_item is not None:
                code = CompatibilityReasonCode.CONSTRAINT_MISMATCH
                return False, code, f"{code}: Candidate enforces constraint '{k} {c_item.operator} {c_item.value}' not requested."

    return True, CompatibilityReasonCode.COMPATIBLE, f"{CompatibilityReasonCode.COMPATIBLE}: Candidate satisfied all domain requirements."

