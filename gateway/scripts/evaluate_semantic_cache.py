#!/usr/bin/env python3
"""
IntelliCache — Semantic Cache Offline Evaluation & Benchmarking Harness.

Evaluates semantic retrieval correctness, candidate ambiguity, safety,
threshold grid sweeps, and lookup runtime latencies without requiring live LLM inference calls.

Key Principles:
1. No silent fallback: explicit providers fail fast unless --allow-mock-fallback is passed.
2. Distinct benchmark modes: synthetic component, real embedding in-memory, or optional pgvector e2e.
3. Strict dataset validation: verifies schema, unique IDs, positive label occurrences, and disjoint splits.
4. Methodological separation: sweeps occur strictly on development; held-out test is evaluated locked.
5. Accurate accounting: safe reuse (TP) vs incorrect reuse (FP) are distinguished; savings only count TP.

Usage:
    python scripts/evaluate_semantic_cache.py --dataset evaluation/dataset.json --split development
    python scripts/evaluate_semantic_cache.py --dataset evaluation/dataset.json --split test
    python scripts/evaluate_semantic_cache.py --sweep
    python scripts/evaluate_semantic_cache.py --benchmark --benchmark-mode component
    python scripts/evaluate_semantic_cache.py --provider ollama
"""

from __future__ import annotations

import argparse
import asyncio
import csv
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import re
import sys
import time
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple
from urllib.parse import urlsplit, urlunsplit

import asyncpg

# Ensure gateway package root is in sys.path
SCRIPT_DIR = Path(__file__).resolve().parent
GATEWAY_ROOT = SCRIPT_DIR.parent
if str(GATEWAY_ROOT) not in sys.path:
    sys.path.insert(0, str(GATEWAY_ROOT))

from app.cache.interfaces import BaseCache, CacheEntry
from app.cache.migration import run_migrations
from app.cache.postgres_cache import PostgresCache
from app.core.contracts import ChatMessage, GenerationRequest, UsageMetrics
from app.embeddings.contracts import EmbeddingResult, EmbeddingTask
from app.embeddings.errors import EmbeddingError
from app.embeddings.interfaces import EmbeddingProvider
from app.embeddings.ollama_adapter import OllamaEmbeddingProvider
from app.semantic_cache.policy import SemanticCachePolicy
from app.vector_store.contracts import (
    VectorRecord,
    VectorSearchResult,
    cosine_distance_to_similarity,
)
from app.vector_store.errors import (
    VectorDimensionMismatchError,
    VectorInvalidError,
    VectorStoreConfigurationError,
    VectorStoreConnectionError,
    VectorStoreError,
)
from app.vector_store.interfaces import BaseVectorStore
from app.vector_store.migration import (
    check_pgvector_available,
    run_vector_migrations,
)
from app.vector_store.postgres_store import PostgresVectorStore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
)
logger = logging.getLogger("intellicache.evaluator")

VALID_SPLITS = {"development", "test"}
VALID_CATEGORIES = {
    "positive_paraphrase",
    "different_language",
    "version_difference",
    "error_difference",
    "constraint_difference",
    "runtime_difference",
    "output_difference",
    "ambiguous_candidates",
    "unrelated_technical",
}


# =============================================================================
# 1. Deterministic Mock Embedding Provider (Offline CI / Local)
# =============================================================================

class DeterministicMockEmbeddingProvider(EmbeddingProvider):
    """
    Offline deterministic embedding provider for evaluation and benchmarking.
    Generates normalized 768-dimensional float vectors with controlled semantic
    properties based on intent keys and vocabulary discriminators.
    Requires no external daemon or network connection.
    """

    def __init__(self, model: str = "nomic-embed-text", dimensions: int = 768):
        self._model = model
        self._dimensions = dimensions

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def dimensions(self) -> int:
        return self._dimensions

    async def embed(self, text: str, task: EmbeddingTask = EmbeddingTask.QUERY) -> EmbeddingResult:
        vec = self.compute_vector(text, task)
        return EmbeddingResult(
            vector=vec,
            model=self._model,
            dimensions=self._dimensions,
        )

    def compute_vector(self, text: str, task: EmbeddingTask = EmbeddingTask.QUERY) -> Tuple[float, ...]:
        clean = (text or "").lower().strip()
        dim = self._dimensions
        vec = [0.0] * dim

        # Semantic concept anchors
        concepts = {
            "py_reverse": ["reverse", "reversed", "reversing", "slice", "slicing", "string"],
            "py_dict_merge": ["merge", "merging", "combine", "union", "dict", "dicts", "dictionary", "dictionaries"],
            "py_file_exists": ["file", "exists", "exist", "pathlib", "isfile"],
            "js_str_to_int": ["convert", "parse", "string", "int", "integer", "parseint", "number"],
            "py_json": ["json", "load", "loads", "dump", "dumps", "file"],
            "py_dedup": ["duplicate", "duplicates", "dedup", "deduplicate", "unique", "set"],
            "py_datetime": ["datetime", "utc", "timestamp", "now", "timezone"],
            "ts_map": ["map", "entries", "iterate", "loop", "looping"],
            "py_venv": ["venv", "virtual", "environment", "activate"],
            "py_fstring": ["f-string", "fstring", "format", "decimal", "float"],
            "git_submodule": ["git", "clone", "submodule", "submodules", "recurse"],
            "py_list_empty": ["empty", "list", "pythonic", "elements"],
            "py_flatten": ["flatten", "2d", "matrix", "nested"],
            "py_sort_dict": ["sort", "sorted", "key", "dicts", "dictionaries"],
            "py_asyncio": ["async", "asyncio", "coroutine", "run", "entrypoint"],
            "py_pip": ["pip", "requirements", "requirements.txt", "install"],
            "py_copy": ["copy", "deepcopy", "clone"],
            "py_env": ["environment", "environ", "getenv", "variable", "variables"],
            "js_array_concat": ["array", "arrays", "concat", "concatenate", "spread"],
            "py_write_file": ["write", "save", "text", "file"],
            "js_object_keys": ["object", "keys", "properties"],
            "ts_str_lower": ["lowercase", "tolowercase", "string"],
            "py_len_str": ["length", "len", "characters", "count"],
            "js_json_parse": ["parse", "json", "object", "string"],
            "py_getcwd": ["cwd", "getcwd", "directory", "current"],
            "py_int_to_str": ["cast", "convert", "int", "integer", "str", "string"],
            "js_has_prop": ["hasown", "property", "key", "exists"],
            "py_while_true": ["infinite", "perpetual", "loop", "while"],
            "py_str_split": ["split", "delimiter", "separator", "words"],
            "py_max_list": ["max", "maximum", "highest", "largest"],
            "py_str_strip": ["strip", "trim", "whitespace", "spaces"],
            "js_array_join": ["join", "separator", "array"],
            "py_list_index": ["index", "find", "item", "element"],
            "terminal_screen": ["clear", "terminal", "screen", "cls", "console"],
            "db_migration": ["migration", "migrations", "migrate", "alembic", "django"],
            "xml_parser": ["xml", "parse", "parsing", "elementtree", "beautifulsoup"],
            "web_server": ["web", "server", "http", "flask", "fastapi"],
            "async_test": ["test", "testing", "async", "pytest", "unittest"],
            "containerize": ["docker", "container", "containerize", "dockerfile"],
            "postgres_conn": ["postgres", "postgresql", "database", "connect", "connection"],
            "state_mgmt": ["state", "management", "redux", "zustand"],
            "react_style": ["style", "styling", "tailwind", "styled-components"],
            "js_linter": ["linter", "eslint", "biome", "linting"],
            "scheduled_job": ["schedule", "scheduled", "job", "celery", "apscheduler"],
            "js_bundler": ["bundle", "bundling", "bundler", "vite", "webpack"],
        }

        # Concept anchor alignment
        for concept, kw_list in concepts.items():
            matches = sum(1 for kw in kw_list if kw in clean)
            if matches >= 2:
                seed = int(hashlib.sha256(concept.encode()).hexdigest(), 16)
                for i in range(dim):
                    comp = ((seed * (i + 1) * 31) % 1000) / 1000.0 - 0.5
                    vec[i] += comp * matches * 2.0

        # Discriminators to ensure hard negatives separate cleanly
        discriminators = [
            "python", "javascript", "typescript", "rust", "golang", "go", "java", "c++", "c#", "ruby", "swift",
            "next.js 14", "next.js", "pages router", "app router", "pydantic v1", "pydantic v2", "react 18", "react 19", "vue 2", "vue 3",
            "node 18", "node 14", "node.js 18", "node.js 14", "class components", "functional components",
            "typeerror", "indexerror", "keyerror", "attributeerror", "referenceerror", "cors", "401 unauthorized", "recursionerror", "memoryerror",
            "zerodivisionerror", "valueerror", "modulenotfounderror", "importerror", "syntaxerror", "filenotfounderror", "permissionerror", "merge conflict",
            "in-place", "copy", "ascending", "descending", "case-sensitive", "case-insensitive", "order",
            "singleton", "o(log n)", "linear scan", "runtimeerror", "non-blocking", "blocking", "deep clone",
            "browser", "node.js", "linux", "windows", "macos", "redis", "proc/meminfo", "vm_stat", "sigterm", "sigint", "cookie",
            "keys", "values", "boolean", "count", "string", "file", "seconds", "iso-8601", "unique", "frequency", "tuples", "csv", "bytes", "mb/gb"
        ]
        for disc in discriminators:
            if disc in clean:
                seed = int(hashlib.sha256(disc.encode()).hexdigest(), 16)
                for i in range(dim):
                    comp = ((seed * (i + 1) * 31) % 1000) / 1000.0 - 0.5
                    vec[i] += comp * 8.0

        # Word-level hashes
        words = re.findall(r"[a-z0-9_#\+\.\-]+", clean)
        for w in words:
            h = int(hashlib.sha256(w.encode()).hexdigest(), 16) % dim
            vec[h] += 1.0

        # Normalize to unit length
        norm = math.sqrt(sum(x * x for x in vec))
        if norm > 0:
            vec = [x / norm for x in vec]
        else:
            vec[0] = 1.0

        return tuple(vec)

    async def close(self) -> None:
        pass


# =============================================================================
# 2. Evaluation Data Models & Strict Dataset Validation
# =============================================================================

@dataclass
class CandidateData:
    id: str
    query_text: str
    cached_response: str
    answer_label: str
    metadata: Dict[str, Any] = field(default_factory=dict)
    prompt_tokens: int = 120
    completion_tokens: int = 250
    total_tokens: int = 370
    original_latency_ms: float = 1250.0


@dataclass
class EvaluationCase:
    id: str
    query: str
    category: str
    split: str
    expected_answer_label: Optional[str]
    rationale: str
    candidates: List[CandidateData]


@dataclass
class EvaluationOutcome:
    case_id: str
    category: str
    split: str
    expected_answer_label: Optional[str]
    safe_reuse_expected: bool
    accepted_candidate_id: Optional[str]
    accepted_answer_label: Optional[str]
    top_similarity: float
    margin: Optional[float]
    decision: str  # "safe_reuse" | "incorrect_reuse" | "safe_rejection" | "incorrect_rejection"
    classification: str  # "TP" | "FP" | "TN" | "FN"
    rejection_reason: Optional[str]
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    original_latency_ms: float = 0.0
    indexing_latency_ms: float = 0.0
    lookup_latency_ms: float = 0.0


@dataclass
class EvaluationMetrics:
    total_cases: int = 0
    tp: int = 0
    fp: int = 0
    fn: int = 0
    tn: int = 0
    precision: float = 0.0
    recall: float = 0.0
    f1_score: float = 0.0
    false_positive_rate: float = 0.0
    rejection_rate: float = 0.0
    accuracy: float = 0.0
    category_breakdown: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    
    # Separated reuse accounting
    safe_reuse_count: int = 0
    incorrect_reuse_count: int = 0
    safe_provider_calls_avoided: int = 0
    hazardous_provider_calls_avoided: int = 0
    total_accepted_hits: int = 0

    # Resource savings estimated ONLY from safe reuse (TP)
    safe_tokens_saved: int = 0
    safe_latency_saved_ms: float = 0.0

    # Hazardous tokens served on incorrect reuse (reported separately, NOT as savings)
    hazardous_tokens_involved: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def get_dataset_hash(dataset_path: str | Path) -> str:
    """Computes SHA-256 digest of the dataset file for traceability."""
    p = Path(dataset_path)
    if not p.is_file():
        return "unknown"
    with open(p, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def load_dataset(dataset_path: str | Path) -> List[EvaluationCase]:
    """
    Loads and rigorously validates the evaluation dataset from JSON.
    Rejects malformed labels, duplicate IDs, missing fields, or split leakage.
    Never silently repairs invalid data.
    """
    p = Path(dataset_path)
    if not p.is_file():
        raise FileNotFoundError(f"Evaluation dataset file not found at: {p}")

    with open(p, "r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, list):
        raise ValueError(f"Dataset root must be a list of case objects, got {type(data).__name__}")

    cases: List[EvaluationCase] = []
    seen_ids: Set[str] = set()
    dev_ids: Set[str] = set()
    test_ids: Set[str] = set()

    for idx, item in enumerate(data):
        if not isinstance(item, dict):
            raise ValueError(f"Dataset entry at index {idx} must be a JSON object, got {type(item).__name__}")

        cid = item.get("id")
        if not cid or not isinstance(cid, str) or not cid.strip():
            raise ValueError(f"Entry {idx} is missing a non-empty string 'id'")
        cid = cid.strip()
        if cid in seen_ids:
            raise ValueError(f"Duplicate case id found: '{cid}'")
        seen_ids.add(cid)

        split = item.get("split")
        if split not in VALID_SPLITS:
            raise ValueError(f"Case '{cid}' has invalid split '{split}'. Must be one of {sorted(VALID_SPLITS)}")
        if split == "development":
            dev_ids.add(cid)
        else:
            test_ids.add(cid)

        category = item.get("category")
        if category not in VALID_CATEGORIES:
            raise ValueError(f"Case '{cid}' has invalid category '{category}'. Must be one of {sorted(VALID_CATEGORIES)}")

        query = item.get("query")
        if not query or not isinstance(query, str) or len(query.strip()) < 3:
            raise ValueError(f"Case '{cid}' must contain a non-empty 'query' string of at least 3 characters")

        raw_candidates = item.get("candidates")
        if not isinstance(raw_candidates, list) or len(raw_candidates) == 0:
            raise ValueError(f"Case '{cid}' must provide a non-empty 'candidates' list (received {raw_candidates})")

        candidates: List[CandidateData] = []
        candidate_labels: Set[str] = set()
        seen_cand_ids: Set[str] = set()

        for c_idx, c_item in enumerate(raw_candidates):
            if not isinstance(c_item, dict):
                raise ValueError(f"Candidate {c_idx} in case '{cid}' must be an object")

            cand_id = c_item.get("id")
            if not cand_id or not isinstance(cand_id, str) or not cand_id.strip():
                raise ValueError(f"Candidate {c_idx} in case '{cid}' is missing a valid 'id'")
            cand_id = cand_id.strip()
            if cand_id in seen_cand_ids:
                raise ValueError(f"Duplicate candidate id '{cand_id}' within case '{cid}'")
            seen_cand_ids.add(cand_id)

            cand_query = c_item.get("query_text")
            if not cand_query or not isinstance(cand_query, str) or not cand_query.strip():
                raise ValueError(f"Candidate '{cand_id}' in case '{cid}' is missing valid 'query_text'")

            cand_resp = c_item.get("cached_response")
            if not cand_resp or not isinstance(cand_resp, str) or not cand_resp.strip():
                raise ValueError(f"Candidate '{cand_id}' in case '{cid}' is missing valid 'cached_response'")

            cand_label = c_item.get("answer_label")
            if not cand_label or not isinstance(cand_label, str) or not cand_label.strip():
                raise ValueError(f"Candidate '{cand_id}' in case '{cid}' is missing valid 'answer_label'")
            cand_label = cand_label.strip()
            candidate_labels.add(cand_label)

            candidates.append(
                CandidateData(
                    id=cand_id,
                    query_text=cand_query.strip(),
                    cached_response=cand_resp.strip(),
                    answer_label=cand_label,
                    metadata=c_item.get("metadata", {}),
                    prompt_tokens=int(c_item.get("prompt_tokens", 120)),
                    completion_tokens=int(c_item.get("completion_tokens", 250)),
                    total_tokens=int(c_item.get("total_tokens", 370)),
                    original_latency_ms=float(c_item.get("original_latency_ms", 1250.0)),
                )
            )

        expected_label = item.get("expected_answer_label")
        if expected_label is not None:
            if not isinstance(expected_label, str) or not expected_label.strip():
                raise ValueError(f"Case '{cid}' has an invalid expected_answer_label: {expected_label}")
            expected_label = expected_label.strip()
            # Positive cases MUST have their expected label present among candidates
            if expected_label not in candidate_labels:
                raise ValueError(
                    f"Positive case '{cid}' expects answer_label '{expected_label}', "
                    f"but that label does not appear in any candidate: {sorted(candidate_labels)}"
                )
        else:
            # Negative cases: expected_answer_label must be None
            pass

        cases.append(
            EvaluationCase(
                id=cid,
                query=query.strip(),
                category=category,
                split=split,
                expected_answer_label=expected_label,
                rationale=str(item.get("rationale", "")).strip(),
                candidates=candidates,
            )
        )

    # Disjointness check
    overlap = dev_ids.intersection(test_ids)
    if overlap:
        raise ValueError(f"Development and test splits must be strictly disjoint, but found overlapping IDs: {overlap}")

    return cases


# =============================================================================
# 3. Core Decision Quality & Evaluation Engine
# =============================================================================

def validate_test_database_url(url: Optional[str]) -> str:
    """
    Validates that a database URL is non-empty, contains 'test' in the database name,
    and does not fall back to production DATABASE_URL.
    """
    if not url or not url.strip():
        raise ValueError(
            "TEST_DATABASE_URL must be explicitly configured when running in 'pgvector' mode. "
            "Never use production DATABASE_URL for evaluation or benchmarking."
        )
    clean_url = url.strip()
    parsed = urlsplit(clean_url)
    db_name = (parsed.path or "").lstrip("/").lower()
    if "test" not in db_name:
        raise ValueError(
            f"TEST_DATABASE_URL database name '{db_name}' must contain 'test' to prevent "
            f"accidental writes or contamination of non-test databases."
        )
    return clean_url


def redact_database_url(url: str) -> str:
    """Redacts credentials from a database connection URL for safe logging and reporting."""
    if not url:
        return ""
    try:
        parsed = urlsplit(url)
        netloc = parsed.netloc
        if "@" in netloc:
            _, host_part = netloc.split("@", 1)
            netloc = f"***:***@{host_part}"
        return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))
    except Exception:
        return "postgresql://***:***@redacted"


def compute_candidate_fingerprint(dataset_hash: str, case_id: str, candidate_id: str) -> str:
    """Computes a deterministic, collision-resistant candidate fingerprint."""
    raw = f"{dataset_hash}:{case_id}:{candidate_id}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def compute_latency_stats(data: Sequence[float]) -> Dict[str, Any]:
    """
    Calculates sample_count, min, mean, median (p50), p90, p95, and p99 from a sequence of latency numbers (ms).
    Adds warning if sample size is under 10.
    """
    n = len(data)
    if not data:
        return {
            "sample_count": 0,
            "min": 0.0,
            "mean": 0.0,
            "p50": 0.0,
            "p90": 0.0,
            "p95": 0.0,
            "p99": 0.0,
        }
    s = sorted(data)
    stats: Dict[str, Any] = {
        "sample_count": n,
        "min": round(s[0], 3),
        "mean": round(sum(s) / n, 3),
        "p50": round(s[int(n * 0.50)], 3),
        "p90": round(s[int(n * 0.90)], 3),
        "p95": round(s[min(n - 1, int(n * 0.95))], 3),
        "p99": round(s[min(n - 1, int(n * 0.99))], 3),
    }
    if 0 < n < 10:
        stats["warning"] = "Sample size < 10; tail percentiles (p95, p99) may be statistically unstable."
    return stats


def compute_metrics_from_counts(
    tp: int, fp: int, fn: int, tn: int
) -> Tuple[float, float, float, float, float, float]:
    """
    Computes precision, recall, f1, fpr, rejection_rate, and accuracy from confusion matrix.
    Safe against division by zero.
    """
    total = tp + fp + fn + tn
    precision = (tp / (tp + fp)) if (tp + fp) > 0 else 0.0
    recall = (tp / (tp + fn)) if (tp + fn) > 0 else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) > 0 else 0.0
    fpr = (fp / (fp + tn)) if (fp + tn) > 0 else 0.0
    rejection_rate = ((tn + fn) / total) if total > 0 else 0.0
    accuracy = ((tp + tn) / total) if total > 0 else 0.0
    return precision, recall, f1, fpr, rejection_rate, accuracy


def calculate_cosine_similarity(vec1: Sequence[float], vec2: Sequence[float]) -> Tuple[float, float]:
    """
    Calculates cosine distance and cosine similarity consistently with pgvector.
    similarity = 1.0 - distance
    """
    dot = sum(a * b for a, b in zip(vec1, vec2))
    sim = max(-1.0, min(1.0, float(dot)))
    dist = max(0.0, min(2.0, 1.0 - sim))
    return dist, sim


async def evaluate_single_case(
    case: EvaluationCase,
    policy: SemanticCachePolicy,
    provider: EmbeddingProvider,
    cache: Optional[BaseCache] = None,
    vector_store: Optional[BaseVectorStore] = None,
    dataset_hash: str = "eval",
    run_id: str = "eval_run",
) -> EvaluationOutcome:
    """
    Evaluates a single evaluation case against candidate questions.
    If both `cache` and `vector_store` are provided, performs genuine PostgreSQL/pgvector
    candidate indexing, nearest-neighbour retrieval, and policy evaluation.
    Otherwise, executes the standard in-memory cosine ranking path.
    """
    if cache is not None and vector_store is not None:
        case_ns = f"eval_{run_id}_{case.id}"
        cand_map: Dict[str, CandidateData] = {}
        try:
            # Stage A: Candidate indexing / storage
            t_idx_start = time.perf_counter()
            # Foreign-key constraint order: exact-cache entry FIRST, then vector upsert
            for cand in case.candidates:
                cand_embed = await provider.embed(cand.query_text, task=EmbeddingTask.DOCUMENT)
                fp = compute_candidate_fingerprint(dataset_hash, case.id, cand.id)
                cand_map[fp] = cand

                meta = dict(cand.metadata)
                meta.setdefault("llm_provider", "groq")
                meta.setdefault("llm_model", "llama-3.3-70b-versatile")
                meta.setdefault("temperature", 0.7)
                meta.setdefault("max_tokens", 1000)

                cache_entry = CacheEntry(
                    response=cand.cached_response,
                    model=meta["llm_model"],
                    usage=UsageMetrics(
                        prompt_tokens=cand.prompt_tokens,
                        completion_tokens=cand.completion_tokens,
                        total_tokens=cand.total_tokens,
                    ),
                    latency_ms=cand.original_latency_ms,
                    metadata=meta,
                    provider=meta["llm_provider"],
                )
                await cache.set(fp, cache_entry, namespace=case_ns)

                vec_rec = VectorRecord(
                    cache_namespace=case_ns,
                    fingerprint=fp,
                    query_text=cand.query_text,
                    model=policy.configured_model,
                    vector=cand_embed.vector,
                    metadata=meta,
                )
                await vector_store.upsert(vec_rec)
            indexing_latency_ms = (time.perf_counter() - t_idx_start) * 1000.0

            # Stage B: Semantic cache lookup (strictly timed separately from indexing)
            t_lookup_start = time.perf_counter()

            # 1. Embed query
            query_embed = await provider.embed(case.query, task=EmbeddingTask.QUERY)

            # 2. Execute actual nearest-neighbour retrieval in PostgreSQL
            vs_candidates = await vector_store.search(
                query_vector=query_embed.vector,
                top_k=policy.top_k,
                namespace=case_ns,
                model=policy.configured_model,
            )

            top_sim = vs_candidates[0].similarity if vs_candidates else 0.0
            margin = None
            if len(vs_candidates) > 1:
                margin = vs_candidates[0].similarity - vs_candidates[1].similarity

            # 3. Build synthetic single-turn GenerationRequest
            request = GenerationRequest(
                messages=[ChatMessage(role="user", content=case.query)],
                provider="groq",
                model="llama-3.3-70b-versatile",
                temperature=0.7,
                max_tokens=1000,
                stream=False,
                bypass_cache=False,
                namespace=case_ns,
            )

            # 4. Evaluate using policy
            winning_cand, rejection_reason = policy.evaluate_candidates(vs_candidates, request)

            lookup_latency_ms = (time.perf_counter() - t_lookup_start) * 1000.0

            # 5. Classify decision quality
            safe_expected = case.expected_answer_label is not None
            accepted_id = None
            accepted_label = None
            prompt_tokens = 0
            comp_tokens = 0
            total_tokens = 0
            orig_latency = 0.0

            if winning_cand is not None:
                orig_c = cand_map.get(winning_cand.fingerprint)
                if orig_c is not None:
                    accepted_id = orig_c.id
                    accepted_label = orig_c.answer_label
                    prompt_tokens = orig_c.prompt_tokens
                    comp_tokens = orig_c.completion_tokens
                    total_tokens = orig_c.total_tokens
                    orig_latency = orig_c.original_latency_ms

            if safe_expected:
                if accepted_label == case.expected_answer_label:
                    classification = "TP"
                    decision = "safe_reuse"
                elif winning_cand is not None:
                    classification = "FP"
                    decision = "incorrect_reuse"
                else:
                    classification = "FN"
                    decision = "incorrect_rejection"
            else:
                if winning_cand is not None:
                    classification = "FP"
                    decision = "incorrect_reuse"
                else:
                    classification = "TN"
                    decision = "safe_rejection"

            return EvaluationOutcome(
                case_id=case.id,
                category=case.category,
                split=case.split,
                expected_answer_label=case.expected_answer_label,
                safe_reuse_expected=safe_expected,
                accepted_candidate_id=accepted_id,
                accepted_answer_label=accepted_label,
                top_similarity=top_sim,
                margin=margin,
                decision=decision,
                classification=classification,
                rejection_reason=rejection_reason,
                prompt_tokens=prompt_tokens,
                completion_tokens=comp_tokens,
                total_tokens=total_tokens,
                original_latency_ms=orig_latency,
                indexing_latency_ms=indexing_latency_ms,
                lookup_latency_ms=lookup_latency_ms,
            )
        finally:
            # Strictly namespace-scoped cleanup in finally block
            try:
                await vector_store.clear(case_ns)
            except Exception as exc:
                logger.warning("Failed to clear vector store namespace '%s': %s", case_ns, exc)
            try:
                await cache.clear(case_ns)
            except Exception as exc:
                logger.warning("Failed to clear cache namespace '%s': %s", case_ns, exc)

    else:
        # Standard in-memory cosine ranking flow
        # Stage A: Candidate indexing (document pre-embedding)
        t_idx_start = time.perf_counter()
        pre_embedded_candidates: List[Tuple[EmbeddingResult, CandidateData]] = []
        for cand in case.candidates:
            cand_embed = await provider.embed(cand.query_text, task=EmbeddingTask.DOCUMENT)
            pre_embedded_candidates.append((cand_embed, cand))
        indexing_latency_ms = (time.perf_counter() - t_idx_start) * 1000.0

        # Stage B: Semantic cache lookup
        t_lookup_start = time.perf_counter()
        # 1. Embed query
        query_embed = await provider.embed(case.query, task=EmbeddingTask.QUERY)

        # 2. Compute similarity against candidate embeddings
        ranked_candidates: List[Tuple[float, VectorSearchResult, CandidateData]] = []
        for cand_embed, cand in pre_embedded_candidates:
            dist, sim = calculate_cosine_similarity(query_embed.vector, cand_embed.vector)

            meta = dict(cand.metadata)
            meta.setdefault("llm_provider", "groq")
            meta.setdefault("llm_model", "llama-3.3-70b-versatile")
            meta.setdefault("temperature", 0.7)
            meta.setdefault("max_tokens", 1000)

            vs_res = VectorSearchResult(
                cache_namespace="default",
                fingerprint=f"fp_{cand.id}",
                query_text=cand.query_text,
                model=policy.configured_model,
                distance=dist,
                similarity=sim,
                cached_response=cand.cached_response,
                prompt_tokens=cand.prompt_tokens,
                completion_tokens=cand.completion_tokens,
                total_tokens=cand.total_tokens,
                original_latency_ms=cand.original_latency_ms,
                metadata=meta,
            )
            ranked_candidates.append((sim, vs_res, cand))

        # Sort descending by similarity
        ranked_candidates.sort(key=lambda x: x[0], reverse=True)
        vs_candidates = [x[1] for x in ranked_candidates]

        top_sim = ranked_candidates[0][0] if ranked_candidates else 0.0
        margin = None
        if len(ranked_candidates) > 1:
            margin = ranked_candidates[0][0] - ranked_candidates[1][0]

        # 3. Build synthetic single-turn GenerationRequest
        request = GenerationRequest(
            messages=[ChatMessage(role="user", content=case.query)],
            provider="groq",
            model="llama-3.3-70b-versatile",
            temperature=0.7,
            max_tokens=1000,
            stream=False,
            bypass_cache=False,
            namespace="default",
        )

        # 4. Evaluate using policy
        winning_cand, rejection_reason = policy.evaluate_candidates(vs_candidates, request)

        lookup_latency_ms = (time.perf_counter() - t_lookup_start) * 1000.0

        # 5. Classify decision quality
        safe_expected = case.expected_answer_label is not None
        accepted_id = None
        accepted_label = None
        prompt_tokens = 0
        comp_tokens = 0
        total_tokens = 0
        orig_latency = 0.0

        if winning_cand is not None:
            for _, vs_c, orig_c in ranked_candidates:
                if vs_c.fingerprint == winning_cand.fingerprint:
                    accepted_id = orig_c.id
                    accepted_label = orig_c.answer_label
                    prompt_tokens = orig_c.prompt_tokens
                    comp_tokens = orig_c.completion_tokens
                    total_tokens = orig_c.total_tokens
                    orig_latency = orig_c.original_latency_ms
                    break

        if safe_expected:
            if accepted_label == case.expected_answer_label:
                classification = "TP"
                decision = "safe_reuse"
            elif winning_cand is not None:
                classification = "FP"
                decision = "incorrect_reuse"
            else:
                classification = "FN"
                decision = "incorrect_rejection"
        else:
            if winning_cand is not None:
                classification = "FP"
                decision = "incorrect_reuse"
            else:
                classification = "TN"
                decision = "safe_rejection"

        return EvaluationOutcome(
            case_id=case.id,
            category=case.category,
            split=case.split,
            expected_answer_label=case.expected_answer_label,
            safe_reuse_expected=safe_expected,
            accepted_candidate_id=accepted_id,
            accepted_answer_label=accepted_label,
            top_similarity=top_sim,
            margin=margin,
            decision=decision,
            classification=classification,
            rejection_reason=rejection_reason,
            prompt_tokens=prompt_tokens,
            completion_tokens=comp_tokens,
            total_tokens=total_tokens,
            original_latency_ms=orig_latency,
            indexing_latency_ms=indexing_latency_ms,
            lookup_latency_ms=lookup_latency_ms,
        )


async def evaluate_dataset(
    cases: List[EvaluationCase],
    policy: SemanticCachePolicy,
    provider: EmbeddingProvider,
    split: str = "development",
    cache: Optional[BaseCache] = None,
    vector_store: Optional[BaseVectorStore] = None,
    dataset_hash: str = "eval",
    run_id: Optional[str] = None,
) -> Tuple[EvaluationMetrics, List[EvaluationOutcome]]:
    """Evaluates all cases matching the specified split partition."""
    filtered_cases = [c for c in cases if split == "all" or c.split == split]
    if run_id is None:
        run_id = f"eval_{int(time.time()*1000)}"

    outcomes: List[EvaluationOutcome] = []
    tp = fp = fn = tn = 0
    safe_tokens_saved = 0
    safe_latency_saved_ms = 0.0
    hazardous_tokens_involved = 0

    category_counts: Dict[str, Dict[str, int]] = {}

    for case in filtered_cases:
        outcome = await evaluate_single_case(
            case,
            policy,
            provider,
            cache=cache,
            vector_store=vector_store,
            dataset_hash=dataset_hash,
            run_id=run_id,
        )
        outcomes.append(outcome)

        cat = case.category
        if cat not in category_counts:
            category_counts[cat] = {"total": 0, "tp": 0, "fp": 0, "fn": 0, "tn": 0}
        category_counts[cat]["total"] += 1
        category_counts[cat][outcome.classification.lower()] += 1

        if outcome.classification == "TP":
            tp += 1
            safe_tokens_saved += outcome.total_tokens
            lat_saved = max(0.0, outcome.original_latency_ms - outcome.lookup_latency_ms)
            safe_latency_saved_ms += lat_saved
        elif outcome.classification == "FP":
            fp += 1
            hazardous_tokens_involved += outcome.total_tokens
        elif outcome.classification == "FN":
            fn += 1
        elif outcome.classification == "TN":
            tn += 1

    prec, rec, f1, fpr, rej_rate, acc = compute_metrics_from_counts(tp, fp, fn, tn)

    # Build category breakdown
    cat_metrics: Dict[str, Dict[str, Any]] = {}
    for cat, counts in category_counts.items():
        c_tp, c_fp, c_fn, c_tn = counts["tp"], counts["fp"], counts["fn"], counts["tn"]
        c_p, c_r, c_f1, c_fpr, c_rej, c_acc = compute_metrics_from_counts(c_tp, c_fp, c_fn, c_tn)
        cat_metrics[cat] = {
            "total": counts["total"],
            "tp": c_tp,
            "fp": c_fp,
            "fn": c_fn,
            "tn": c_tn,
            "precision": round(c_p, 4),
            "recall": round(c_r, 4),
            "f1_score": round(c_f1, 4),
            "false_positive_rate": round(c_fpr, 4),
            "rejection_rate": round(c_rej, 4),
            "accuracy": round(c_acc, 4),
        }

    metrics = EvaluationMetrics(
        total_cases=len(filtered_cases),
        tp=tp,
        fp=fp,
        fn=fn,
        tn=tn,
        precision=round(prec, 4),
        recall=round(rec, 4),
        f1_score=round(f1, 4),
        false_positive_rate=round(fpr, 4),
        rejection_rate=round(rej_rate, 4),
        accuracy=round(acc, 4),
        category_breakdown=cat_metrics,
        safe_reuse_count=tp,
        incorrect_reuse_count=fp,
        safe_provider_calls_avoided=tp,
        hazardous_provider_calls_avoided=fp,
        total_accepted_hits=tp + fp,
        safe_tokens_saved=safe_tokens_saved,
        safe_latency_saved_ms=round(safe_latency_saved_ms, 2),
        hazardous_tokens_involved=hazardous_tokens_involved,
    )

    return metrics, outcomes


# =============================================================================
# 4. Threshold Sweeps (Strictly Development Partition)
# =============================================================================

async def run_threshold_sweep(
    cases: List[EvaluationCase],
    provider: EmbeddingProvider,
    thresholds: List[float],
    margins: List[float],
    split: str = "development",
    cache: Optional[BaseCache] = None,
    vector_store: Optional[BaseVectorStore] = None,
    dataset_hash: str = "eval",
) -> List[Dict[str, Any]]:
    """
    Executes parameter grid sweep strictly across the development split.
    Rejects sweeps requested on held-out test data to prevent contamination.
    """
    if split != "development":
        logger.warning(
            "Parameter sweep was requested on split='%s'. Forcing split='development' "
            "to prevent held-out test set contamination.",
            split,
        )
        split = "development"

    dev_cases = [c for c in cases if c.split == "development"]
    logger.info(
        "Executing development grid sweep on %d cases: %d thresholds x %d margins",
        len(dev_cases),
        len(thresholds),
        len(margins),
    )

    results = []
    for th in thresholds:
        for mg in margins:
            policy = SemanticCachePolicy(min_similarity=th, min_margin=mg)
            metrics, _ = await evaluate_dataset(
                dev_cases,
                policy,
                provider,
                split="development",
                cache=cache,
                vector_store=vector_store,
                dataset_hash=dataset_hash,
            )
            results.append({
                "threshold": round(th, 4),
                "margin": round(mg, 4),
                "tp": metrics.tp,
                "fp": metrics.fp,
                "fn": metrics.fn,
                "tn": metrics.tn,
                "precision": metrics.precision,
                "recall": metrics.recall,
                "f1_score": metrics.f1_score,
                "false_positive_rate": metrics.false_positive_rate,
                "rejection_rate": metrics.rejection_rate,
                "accuracy": metrics.accuracy,
            })

    # Sort candidates by lowest FPR first, then highest F1 score
    results.sort(key=lambda r: (-r["false_positive_rate"], r["f1_score"]), reverse=True)
    return results


# =============================================================================
# 5. Runtime Latency Benchmarking (Never Fabricated)
# =============================================================================

async def run_benchmark(
    cases: List[EvaluationCase],
    policy: SemanticCachePolicy,
    provider: EmbeddingProvider,
    split: str = "development",
    iterations: int = 5,
    benchmark_mode: str = "component",
    cache: Optional[BaseCache] = None,
    vector_store: Optional[BaseVectorStore] = None,
    dataset_hash: str = "eval",
    db_info: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Measures runtime latency across actual executed operations without live LLM calls.
    Never fabricates similarities or latencies.
    Supports 'component' (synthetic mock), 'real' (Ollama in-memory), and 'pgvector' (Ollama + PostgreSQL/pgvector).
    """
    bench_cases = [c for c in cases if split == "all" or c.split == split]
    logger.info(
        "Running benchmark mode='%s' on %d cases with %d iterations...",
        benchmark_mode,
        len(bench_cases),
        iterations,
    )

    if benchmark_mode == "pgvector":
        if cache is None or vector_store is None:
            raise ValueError("Benchmark mode 'pgvector' requires active cache and vector_store instances.")

        candidate_indexing_ms: List[float] = []
        query_embed_ms: List[float] = []
        postgres_retrieval_ms: List[float] = []
        policy_eval_ms: List[float] = []
        semantic_lookup_ms: List[float] = []

        run_id = f"bench_{int(time.time()*1000)}_{os.getpid()}"

        active_namespaces: List[str] = []
        try:
            # 1. Candidate indexing / setup measurement (one-time setup per case)
            for case in bench_cases:
                case_ns = f"bench_{run_id}_{case.id}"
                active_namespaces.append(case_ns)

                for cand in case.candidates:
                    t_idx0 = time.perf_counter()
                    cand_embed = await provider.embed(cand.query_text, task=EmbeddingTask.DOCUMENT)
                    fp = compute_candidate_fingerprint(dataset_hash, case.id, cand.id)

                    meta = dict(cand.metadata)
                    meta.setdefault("llm_provider", "groq")
                    meta.setdefault("llm_model", "llama-3.3-70b-versatile")
                    meta.setdefault("temperature", 0.7)
                    meta.setdefault("max_tokens", 1000)

                    # Order: exact-cache entry FIRST
                    cache_entry = CacheEntry(
                        response=cand.cached_response,
                        model=meta["llm_model"],
                        usage=UsageMetrics(
                            prompt_tokens=cand.prompt_tokens,
                            completion_tokens=cand.completion_tokens,
                            total_tokens=cand.total_tokens,
                        ),
                        latency_ms=cand.original_latency_ms,
                        metadata=meta,
                        provider=meta["llm_provider"],
                    )
                    await cache.set(fp, cache_entry, namespace=case_ns)

                    # VectorRecord SECOND
                    vec_rec = VectorRecord(
                        cache_namespace=case_ns,
                        fingerprint=fp,
                        query_text=cand.query_text,
                        model=policy.configured_model,
                        vector=cand_embed.vector,
                        metadata=meta,
                    )
                    await vector_store.upsert(vec_rec)
                    candidate_indexing_ms.append((time.perf_counter() - t_idx0) * 1000.0)

            # 2. Repeated query lookup measurement
            for _ in range(iterations):
                for case in bench_cases:
                    case_ns = f"bench_{run_id}_{case.id}"

                    # Measure query embedding latency
                    t0 = time.perf_counter()
                    query_res = await provider.embed(case.query, task=EmbeddingTask.QUERY)
                    t_q = (time.perf_counter() - t0) * 1000.0
                    query_embed_ms.append(t_q)

                    # Measure actual PostgreSQL pgvector retrieval latency
                    t1 = time.perf_counter()
                    vs_candidates = await vector_store.search(
                        query_vector=query_res.vector,
                        top_k=policy.top_k,
                        namespace=case_ns,
                        model=policy.configured_model,
                    )
                    t_search = (time.perf_counter() - t1) * 1000.0
                    postgres_retrieval_ms.append(t_search)

                    # Measure policy evaluation latency
                    case_req = GenerationRequest(
                        messages=[ChatMessage(role="user", content=case.query)],
                        provider="groq",
                        model="llama-3.3-70b-versatile",
                        temperature=0.7,
                        max_tokens=1000,
                        stream=False,
                        bypass_cache=False,
                        namespace=case_ns,
                    )
                    t2 = time.perf_counter()
                    policy.evaluate_candidates(vs_candidates, case_req)
                    t_pol = (time.perf_counter() - t2) * 1000.0
                    policy_eval_ms.append(t_pol)

                    # Semantic lookup latency = query embedding + postgres retrieval + policy evaluation
                    semantic_lookup_ms.append(t_q + t_search + t_pol)
        finally:
            for ns in active_namespaces:
                try:
                    await vector_store.clear(ns)
                except Exception as exc:
                    logger.warning("Failed to clear vector store namespace '%s': %s", ns, exc)
                try:
                    await cache.clear(ns)
                except Exception as exc:
                    logger.warning("Failed to clear cache namespace '%s': %s", ns, exc)

        return {
            "benchmark_mode": "pgvector",
            "benchmark_label": "Real Ollama embeddings with PostgreSQL/pgvector retrieval",
            "disclaimer": (
                "Measured using real OllamaEmbeddingProvider and live PostgreSQL/pgvector. "
                "Captures real HTTP embedding requests, actual pgvector cosine-distance index scan, "
                "exact-cache JOIN, and policy candidate evaluation. Does NOT include LLM generation latency."
            ),
            "iterations": iterations,
            "sample_count": len(semantic_lookup_ms),
            "per_metric_sample_counts": {
                "candidate_indexing": len(candidate_indexing_ms),
                "query_embedding": len(query_embed_ms),
                "postgres_retrieval": len(postgres_retrieval_ms),
                "policy_evaluation": len(policy_eval_ms),
                "semantic_lookup": len(semantic_lookup_ms),
            },
            "candidate_indexing_latency_ms": compute_latency_stats(candidate_indexing_ms),
            "query_embedding_latency_ms": compute_latency_stats(query_embed_ms),
            "postgres_retrieval_latency_ms": compute_latency_stats(postgres_retrieval_ms),
            "policy_evaluation_latency_ms": compute_latency_stats(policy_eval_ms),
            "semantic_lookup_latency_ms": compute_latency_stats(semantic_lookup_ms),
            "total_measured_latency_ms": compute_latency_stats(semantic_lookup_ms),
            "database_info": db_info or {},
        }

    else:
        # Standard in-memory benchmark (component or real)
        # Stage A: Candidate indexing measurement (one-time candidate document embedding)
        candidate_indexing_ms: List[float] = []
        case_candidates_map: Dict[str, List[Tuple[EmbeddingResult, CandidateData]]] = {}
        for case in bench_cases:
            case_cands = []
            for cand in case.candidates:
                t_idx0 = time.perf_counter()
                cand_res = await provider.embed(cand.query_text, task=EmbeddingTask.DOCUMENT)
                t_idx = (time.perf_counter() - t_idx0) * 1000.0
                candidate_indexing_ms.append(t_idx)
                case_cands.append((cand_res, cand))
            case_candidates_map[case.id] = case_cands

        # Stage B: Repeated query lookup measurement
        query_embed_ms = []
        candidate_sim_ms = []
        policy_eval_ms = []
        lookup_ms = []

        for _ in range(iterations):
            for case in bench_cases:
                # 1. Measure Query Embedding Latency
                t0 = time.perf_counter()
                query_res = await provider.embed(case.query, task=EmbeddingTask.QUERY)
                t_q_embed = (time.perf_counter() - t0) * 1000.0
                query_embed_ms.append(t_q_embed)

                # 2. Compute similarity against pre-embedded candidate documents and rank
                t1 = time.perf_counter()
                vs_candidates = []
                for cand_res, cand in case_candidates_map[case.id]:
                    dist, sim = calculate_cosine_similarity(query_res.vector, cand_res.vector)
                    vs_candidates.append(
                        VectorSearchResult(
                            cache_namespace="default",
                            fingerprint=f"fp_{cand.id}",
                            query_text=cand.query_text,
                            model=policy.configured_model,
                            distance=dist,
                            similarity=sim,
                            cached_response=cand.cached_response,
                            metadata={
                                "llm_provider": "groq",
                                "llm_model": "llama-3.3-70b-versatile",
                                "temperature": 0.7,
                                "max_tokens": 1000,
                            },
                        )
                    )
                vs_candidates.sort(key=lambda x: x.similarity, reverse=True)
                t_cand_sim = (time.perf_counter() - t1) * 1000.0
                candidate_sim_ms.append(t_cand_sim)

                # 3. Measure policy evaluation latency
                case_req = GenerationRequest(
                    messages=[ChatMessage(role="user", content=case.query)],
                    provider="groq",
                    model="llama-3.3-70b-versatile",
                )
                t2 = time.perf_counter()
                policy.evaluate_candidates(vs_candidates, case_req)
                t_pol = (time.perf_counter() - t2) * 1000.0
                policy_eval_ms.append(t_pol)

                # Total measured lookup latency (query embed + candidate similarity + policy eval)
                lookup_ms.append(t_q_embed + t_cand_sim + t_pol)

        is_mock = isinstance(provider, DeterministicMockEmbeddingProvider)
        if is_mock:
            mode_label = "Synthetic/mock component timings (offline)"
            disclaimer = (
                "Measured using DeterministicMockEmbeddingProvider. Captures local CPU vector hashing, "
                "dot-product cosine similarity, and in-memory policy rules. Does NOT reflect live production latency "
                "or PostgreSQL pgvector disk index scan time."
            )
        else:
            mode_label = "Real embedding provider in-memory evaluation"
            disclaimer = (
                f"Measured using real {provider.__class__.__name__} ({provider.model_name}). "
                "Captures HTTP API embedding latency, in-memory cosine ranking, and policy rules. "
                "Does NOT measure PostgreSQL pgvector disk index scan time."
            )

        return {
            "benchmark_mode": "offline_component" if is_mock else "real_in_memory",
            "benchmark_label": mode_label,
            "disclaimer": disclaimer,
            "iterations": iterations,
            "sample_count": len(lookup_ms),
            "per_metric_sample_counts": {
                "candidate_indexing": len(candidate_indexing_ms),
                "query_embedding": len(query_embed_ms),
                "candidate_sim_and_ranking": len(candidate_sim_ms),
                "policy_evaluation": len(policy_eval_ms),
                "total_measured_lookup": len(lookup_ms),
            },
            "candidate_indexing_latency_ms": compute_latency_stats(candidate_indexing_ms),
            "query_embedding_latency_ms": compute_latency_stats(query_embed_ms),
            "candidate_sim_and_ranking_latency_ms": compute_latency_stats(candidate_sim_ms),
            "policy_evaluation_latency_ms": compute_latency_stats(policy_eval_ms),
            "total_measured_latency_ms": compute_latency_stats(lookup_ms),
        }


# =============================================================================
# 6. Report Serialization (JSON & CSV)
# =============================================================================

def save_reports(
    metrics: EvaluationMetrics,
    outcomes: List[EvaluationOutcome],
    sweep_results: Optional[List[Dict[str, Any]]],
    benchmark_results: Optional[Dict[str, Any]],
    output_dir: str | Path,
    metadata: Dict[str, Any],
    prefix: str = "semantic_cache_eval",
) -> Tuple[Path, Path]:
    """
    Saves JSON and CSV report files in the target output directory.
    Output data is sanitized: no raw query text, passwords, or vectors.
    """
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    json_path = out_dir / f"{prefix}_{timestamp}.json"
    csv_path = out_dir / f"{prefix}_{timestamp}.csv"

    # Safety status determination
    if metrics.fp > 0:
        safety_status = f"WARNING: {metrics.fp} false positive(s) observed. Not zero-defect. Hazardous reuse occurred."
    else:
        safety_status = "Safe: Zero false positives observed on this evaluation partition."

    # 1. JSON Report
    json_payload = {
        "report_metadata": {
            **metadata,
            "safety_status": safety_status,
            "sample_size_note": f"Evaluated {metrics.total_cases} cases on split '{metadata.get('dataset_split')}'. Synthetic pilot dataset; labels require domain review.",
        },
        "summary_metrics": metrics.to_dict(),
        "sweep": sweep_results or [],
        "benchmark": benchmark_results or {},
        "outcomes": [
            {
                "case_id": o.case_id,
                "category": o.category,
                "split": o.split,
                "safe_reuse_expected": o.safe_reuse_expected,
                "top_similarity": round(o.top_similarity, 4),
                "margin": round(o.margin, 4) if o.margin is not None else None,
                "decision": o.decision,
                "classification": o.classification,
                "rejection_reason": o.rejection_reason,
                "indexing_latency_ms": round(o.indexing_latency_ms, 2),
                "lookup_latency_ms": round(o.lookup_latency_ms, 2),
            }
            for o in outcomes
        ],
    }
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(json_payload, f, indent=2)

    # 2. CSV Report
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "# IntelliCache Evaluation Report",
            f"DatasetHash: {metadata.get('dataset_hash')}",
            f"Split: {metadata.get('dataset_split')}",
            f"Provider: {metadata.get('embedding_provider')}",
            f"Model: {metadata.get('embedding_model')}",
            f"VectorIndex: {metadata.get('vector_index_type', 'none')}",
            f"IndexUsed: {metadata.get('vector_index_used', False)}",
            f"Timestamp: {metadata.get('evaluation_timestamp')}",
        ])
        writer.writerow([
            "case_id", "category", "split", "safe_reuse_expected",
            "top_similarity", "margin", "classification", "decision",
            "indexing_latency_ms", "lookup_latency_ms", "rejection_reason"
        ])
        for o in outcomes:
            writer.writerow([
                o.case_id,
                o.category,
                o.split,
                o.safe_reuse_expected,
                round(o.top_similarity, 4),
                round(o.margin, 4) if o.margin is not None else "",
                o.classification,
                o.decision,
                round(o.indexing_latency_ms, 2),
                round(o.lookup_latency_ms, 2),
                o.rejection_reason or "",
            ])

    return json_path, csv_path


# =============================================================================
# 7. CLI Entry Point
# =============================================================================

async def async_main() -> None:
    parser = argparse.ArgumentParser(
        description="Offline evaluation & benchmarking harness for IntelliCache semantic caching pipeline."
    )
    parser.add_argument(
        "--dataset",
        type=str,
        default="evaluation/dataset.json",
        help="Path to evaluation dataset JSON.",
    )
    parser.add_argument(
        "--split",
        type=str,
        default="development",
        choices=["development", "test", "all"],
        help="Partition split to evaluate.",
    )
    parser.add_argument(
        "--provider",
        type=str,
        default="mock",
        choices=["mock", "ollama"],
        help="Embedding provider adapter to use.",
    )
    parser.add_argument(
        "--allow-mock-fallback",
        action="store_true",
        help="Explicitly permit fallback to DeterministicMockEmbeddingProvider if requested real provider fails.",
    )
    parser.add_argument(
        "--model",
        type=str,
        default="nomic-embed-text",
        help="Embedding model identifier.",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.82,
        help="Minimum cosine similarity threshold.",
    )
    parser.add_argument(
        "--margin",
        type=float,
        default=0.03,
        help="Minimum ambiguity margin.",
    )
    parser.add_argument(
        "--sweep",
        action="store_true",
        help="Execute a grid sweep of similarity thresholds and margins on the development split.",
    )
    parser.add_argument(
        "--benchmark",
        action="store_true",
        help="Benchmark embedding and policy candidate evaluation latency.",
    )
    parser.add_argument(
        "--benchmark-mode",
        type=str,
        default="auto",
        choices=["auto", "component", "real", "pgvector"],
        help="Benchmark execution mode: auto, component (offline mock), real (live Ollama in-memory), pgvector (Ollama + PostgreSQL/pgvector).",
    )
    parser.add_argument(
        "--test-database-url",
        type=str,
        default=None,
        help="Explicit PostgreSQL test database URL for pgvector mode (must contain 'test' in db name; defaults to TEST_DATABASE_URL env var).",
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=5,
        help="Number of iterations for benchmark measurements.",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default="evaluation/reports",
        help="Directory to save JSON and CSV reports.",
    )

    args = parser.parse_args()

    dataset_path = Path(args.dataset)
    if not dataset_path.is_absolute():
        dataset_path = GATEWAY_ROOT / dataset_path

    out_dir = Path(args.output_dir)
    if not out_dir.is_absolute():
        out_dir = GATEWAY_ROOT / out_dir

    logger.info("Loading and validating evaluation dataset from: %s", dataset_path)
    cases = load_dataset(dataset_path)
    dataset_hash = get_dataset_hash(dataset_path)
    logger.info("Dataset validated successfully: %d cases loaded (SHA256: %s...)", len(cases), dataset_hash[:12])

    # 1. Determine effective benchmark mode
    if args.benchmark_mode == "auto":
        effective_benchmark_mode = "component" if args.provider == "mock" else "real"
    else:
        effective_benchmark_mode = args.benchmark_mode

    # 2. Strict safety checks for pgvector mode
    db_pool = None
    pg_cache = None
    pg_vector_store = None
    db_info: Dict[str, Any] = {}

    if effective_benchmark_mode == "pgvector":
        if args.provider != "ollama":
            logger.error(
                "Benchmark mode 'pgvector' strictly requires '--provider ollama'. "
                "Synthetic mock embeddings cannot be used for genuine pgvector evaluation."
            )
            sys.exit(1)

        raw_db_url = args.test_database_url or os.environ.get("TEST_DATABASE_URL")
        try:
            db_url = validate_test_database_url(raw_db_url)
        except ValueError as exc:
            logger.error("Database configuration error: %s", exc)
            sys.exit(1)

        redacted_url = redact_database_url(db_url)
        logger.info("Connecting to test PostgreSQL instance: %s", redacted_url)

        try:
            db_pool = await asyncpg.create_pool(dsn=db_url, min_size=1, max_size=5)
            async with db_pool.acquire() as conn:
                pg_version = await conn.fetchval("SHOW server_version")
                has_pgvector = await check_pgvector_available(conn)
                if not has_pgvector:
                    logger.error(
                        "pgvector extension 'vector' is not available on PostgreSQL at %s. "
                        "Please install pgvector on the PostgreSQL server.",
                        redacted_url,
                    )
                    await db_pool.close()
                    sys.exit(1)

                pgv_row = await conn.fetchrow(
                    "SELECT default_version, installed_version FROM pg_available_extensions WHERE name = 'vector'"
                )
                pgv_version = (pgv_row["installed_version"] or pgv_row["default_version"]) if pgv_row else "unknown"

            # Run exact-cache migrations
            await run_migrations(db_pool)

            db_info = {
                "postgres_version": str(pg_version),
                "pgvector_version": str(pgv_version),
                "database_url": redacted_url,
            }
        except Exception as exc:
            logger.error("Failed to connect or initialize PostgreSQL at %s: %s", redacted_url, exc)
            if db_pool:
                await db_pool.close()
            sys.exit(1)

    provider: EmbeddingProvider
    actual_provider_name: str
    mock_used: bool
    mock_fallback_occurred: bool

    # Provider Initialization (No Silent Fallback)
    if args.provider == "ollama":
        ollama_url = os.environ.get("EMBEDDING_BASE_URL", "http://127.0.0.1:11434")
        logger.info("Connecting to requested Ollama embedding provider at %s...", ollama_url)
        temp_provider = OllamaEmbeddingProvider(model=args.model, base_url=ollama_url)
        try:
            await temp_provider.embed("ping", task=EmbeddingTask.QUERY)
            logger.info("Connected to live Ollama embedding provider successfully.")
            provider = temp_provider
            actual_provider_name = "OllamaEmbeddingProvider"
            mock_used = False
            mock_fallback_occurred = False
        except Exception as exc:
            await temp_provider.close()
            if effective_benchmark_mode == "pgvector":
                logger.error(
                    "Ollama embedding provider at %s is unavailable for pgvector mode: %s\n"
                    "Failing clearly without silent fallback. Mock fallback is not permitted in pgvector mode.",
                    ollama_url,
                    exc,
                )
                if db_pool:
                    await db_pool.close()
                sys.exit(1)
            elif not args.allow_mock_fallback:
                logger.error(
                    "Requested embedding provider 'ollama' is unavailable at %s: %s\n"
                    "Failing clearly without silent fallback. To explicitly permit offline mock fallback, re-run with --allow-mock-fallback.",
                    ollama_url,
                    exc,
                )
                sys.exit(1)
            else:
                logger.warning(
                    "Ollama unavailable at %s (%s). Explicit mock fallback enabled via --allow-mock-fallback.",
                    ollama_url,
                    exc,
                )
                provider = DeterministicMockEmbeddingProvider(model=args.model)
                actual_provider_name = "DeterministicMockEmbeddingProvider (fallback)"
                mock_used = True
                mock_fallback_occurred = True
    else:
        provider = DeterministicMockEmbeddingProvider(model=args.model)
        actual_provider_name = "DeterministicMockEmbeddingProvider"
        mock_used = True
        mock_fallback_occurred = False

    try:
        if effective_benchmark_mode == "pgvector":
            try:
                await run_vector_migrations(db_pool, dimensions=provider.dimensions)
                pg_cache = PostgresCache(pool=db_pool, default_namespace="default")
                pg_vector_store = PostgresVectorStore(
                    pool=db_pool,
                    default_namespace="default",
                    default_model=args.model,
                    dimensions=provider.dimensions,
                )
                idx_info = await pg_vector_store.get_index_info()
                dummy_vec = [0.0] * provider.dimensions
                plan_info = await pg_vector_store.explain_search(dummy_vec, analyze=False)
                db_info.update({
                    "vector_index_type": idx_info.get("vector_index_type", "none"),
                    "vector_index_available": idx_info.get("vector_index_available", False),
                    "vector_index_used": plan_info.get("vector_index_used", False),
                    "retrieval_method": plan_info.get("retrieval_method", "Unknown"),
                    "execution_plan_summary": plan_info.get("execution_plan_summary", []),
                })
            except Exception as exc:
                logger.error("Vector migration or initialization failed: %s", exc)
                sys.exit(1)

        policy = SemanticCachePolicy(
            min_similarity=args.threshold,
            min_margin=args.margin,
            configured_model=args.model,
        )

        sweep_results = None
        selected_config = None
        if args.sweep:
            thresholds = [0.70, 0.75, 0.80, 0.82, 0.85, 0.88, 0.90, 0.95]
            margins = [0.01, 0.02, 0.03, 0.05, 0.08]
            sweep_results = await run_threshold_sweep(
                cases,
                provider,
                thresholds,
                margins,
                split="development",
                cache=pg_cache,
                vector_store=pg_vector_store,
                dataset_hash=dataset_hash,
            )
            print("\n=== TOP 5 CONFIGURATIONS FROM DEV SWEEP (SPLIT = DEVELOPMENT) ===")
            print(f"{'Threshold':<10} {'Margin':<8} {'Precision':<10} {'Recall':<8} {'F1':<8} {'FPR':<8} {'Rejection':<10}")
            print("-" * 66)
            for r in sweep_results[:5]:
                print(
                    f"{r['threshold']:<10.2f} {r['margin']:<8.2f} {r['precision']:<10.4f} "
                    f"{r['recall']:<8.4f} {r['f1_score']:<8.4f} {r['false_positive_rate']:<8.4f} {r['rejection_rate']:<10.4f}"
                )
            selected_config = sweep_results[0]

            # Cross-split comparison
            dev_cases = [c for c in cases if c.split == "development"]
            test_cases = [c for c in cases if c.split == "test"]
            base_pol = SemanticCachePolicy(min_similarity=0.82, min_margin=0.03, configured_model=args.model)
            sel_pol = SemanticCachePolicy(min_similarity=selected_config["threshold"], min_margin=selected_config["margin"], configured_model=args.model)

            m_b_dev, _ = await evaluate_dataset(
                dev_cases, base_pol, provider, split="development",
                cache=pg_cache, vector_store=pg_vector_store, dataset_hash=dataset_hash,
            )
            m_s_dev, _ = await evaluate_dataset(
                dev_cases, sel_pol, provider, split="development",
                cache=pg_cache, vector_store=pg_vector_store, dataset_hash=dataset_hash,
            )
            m_b_test, _ = await evaluate_dataset(
                test_cases, base_pol, provider, split="test",
                cache=pg_cache, vector_store=pg_vector_store, dataset_hash=dataset_hash,
            )
            m_s_test, _ = await evaluate_dataset(
                test_cases, sel_pol, provider, split="test",
                cache=pg_cache, vector_store=pg_vector_store, dataset_hash=dataset_hash,
            )

            sel_th = selected_config["threshold"]
            sel_mg = selected_config["margin"]
            sel_label_dev = f"Selected (th={sel_th:.2f}, mg={sel_mg:.2f})"
            sel_label_test = f"Selected Locked (th={sel_th:.2f}, mg={sel_mg:.2f})"

            print("\n=== THRESHOLD SELECTION & HELD-OUT VALIDATION SUMMARY ===")
            print(f"{'Configuration':<34} {'Split':<12} {'Precision':<10} {'Recall':<8} {'F1':<8} {'FPR':<8} {'Rejection':<10}")
            print("-" * 92)
            print(f"{'Baseline (th=0.82, mg=0.03)':<34} {'dev':<12} {m_b_dev.precision:<10.4f} {m_b_dev.recall:<8.4f} {m_b_dev.f1_score:<8.4f} {m_b_dev.false_positive_rate:<8.4f} {m_b_dev.rejection_rate:<10.4f}")
            print(f"{sel_label_dev:<34} {'dev':<12} {m_s_dev.precision:<10.4f} {m_s_dev.recall:<8.4f} {m_s_dev.f1_score:<8.4f} {m_s_dev.false_positive_rate:<8.4f} {m_s_dev.rejection_rate:<10.4f}")
            print(f"{'Baseline (th=0.82, mg=0.03)':<34} {'test (held)':<12} {m_b_test.precision:<10.4f} {m_b_test.recall:<8.4f} {m_b_test.f1_score:<8.4f} {m_b_test.false_positive_rate:<8.4f} {m_b_test.rejection_rate:<10.4f}")
            print(f"{sel_label_test:<34} {'test (held)':<12} {m_s_test.precision:<10.4f} {m_s_test.recall:<8.4f} {m_s_test.f1_score:<8.4f} {m_s_test.false_positive_rate:<8.4f} {m_s_test.rejection_rate:<10.4f}")
            print("-" * 92)
            print("METHODOLOGY NOTE: If the development-selected configuration degrades on held-out test data,")
            print("DO NOT tune hyper-parameters against test results. Perform error analysis, add representative")
            print("development cases, and re-validate on a fresh held-out partition.\n")

        benchmark_results = None
        if args.benchmark:
            benchmark_results = await run_benchmark(
                cases,
                policy,
                provider,
                split=args.split,
                iterations=args.iterations,
                benchmark_mode=effective_benchmark_mode,
                cache=pg_cache,
                vector_store=pg_vector_store,
                dataset_hash=dataset_hash,
                db_info=db_info,
            )

            if effective_benchmark_mode == "pgvector":
                print("\n=== POSTGRESQL / PGVECTOR BENCHMARK METRICS (NO LLM CALLS) ===")
                print(f"Mode:                        {benchmark_results['benchmark_label']}")
                print(f"PostgreSQL Version:          {db_info.get('postgres_version', 'unknown')}")
                print(f"pgvector Version:            {db_info.get('pgvector_version', 'unknown')}")
                print(f"Vector Index Type:           {db_info.get('vector_index_type', 'none')}")
                print(f"Vector Index Available:      {db_info.get('vector_index_available', False)}")
                print(f"Vector Index Used:           {db_info.get('vector_index_used', False)}")
                print(f"Retrieval Method:            {db_info.get('retrieval_method', 'Unknown')}")
                print(f"Database URL:                {db_info.get('database_url', 'redacted')}")
                print(f"Iterations:                  {benchmark_results['iterations']}, Samples: {benchmark_results['sample_count']}")
                if "per_metric_sample_counts" in benchmark_results:
                    print(f"Per-Metric Sample Counts:    {benchmark_results['per_metric_sample_counts']}")
                idx = benchmark_results["candidate_indexing_latency_ms"]
                q_emb = benchmark_results["query_embedding_latency_ms"]
                pg_ret = benchmark_results["postgres_retrieval_latency_ms"]
                pol = benchmark_results["policy_evaluation_latency_ms"]
                tot = benchmark_results["semantic_lookup_latency_ms"]
                print(f"Candidate Indexing Latency:  min={idx['min']}ms, mean={idx['mean']}ms, p50={idx['p50']}ms, p95={idx['p95']}ms, p99={idx['p99']}ms")
                print(f"Query Embedding Latency:     min={q_emb['min']}ms, mean={q_emb['mean']}ms, p50={q_emb['p50']}ms, p95={q_emb['p95']}ms, p99={q_emb['p99']}ms")
                print(f"PostgreSQL Retrieval Latency:min={pg_ret['min']}ms, mean={pg_ret['mean']}ms, p50={pg_ret['p50']}ms, p95={pg_ret['p95']}ms, p99={pg_ret['p99']}ms")
                print(f"Policy Candidate Evaluation: min={pol['min']}ms, mean={pol['mean']}ms, p50={pol['p50']}ms, p95={pol['p95']}ms, p99={pol['p99']}ms")
                print(f"Semantic Lookup Latency:     min={tot['min']}ms, mean={tot['mean']}ms, p50={tot['p50']}ms, p95={tot['p95']}ms, p99={tot['p99']}ms")
            else:
                print("\n=== RUNTIME BENCHMARK METRICS (NO LLM CALLS) ===")
                print(f"Mode:                        {benchmark_results['benchmark_label']}")
                print(f"Disclaimer:                  {benchmark_results['disclaimer']}")
                print(f"Iterations:                  {benchmark_results['iterations']}, Samples: {benchmark_results['sample_count']}")
                if "per_metric_sample_counts" in benchmark_results:
                    print(f"Per-Metric Sample Counts:    {benchmark_results['per_metric_sample_counts']}")
                q_emb = benchmark_results["query_embedding_latency_ms"]
                c_sim = benchmark_results["candidate_sim_and_ranking_latency_ms"]
                pol = benchmark_results["policy_evaluation_latency_ms"]
                tot = benchmark_results["total_measured_latency_ms"]
                print(f"Query Embedding Latency:     min={q_emb['min']}ms, mean={q_emb['mean']}ms, p50={q_emb['p50']}ms, p95={q_emb['p95']}ms, p99={q_emb['p99']}ms")
                print(f"Candidate Sim & Ranking:     min={c_sim['min']}ms, mean={c_sim['mean']}ms, p50={c_sim['p50']}ms, p95={c_sim['p95']}ms, p99={c_sim['p99']}ms")
                print(f"Policy Candidate Evaluation: min={pol['min']}ms, mean={pol['mean']}ms, p50={pol['p50']}ms, p95={pol['p95']}ms, p99={pol['p99']}ms")
                print(f"Total Measured Lookup:       min={tot['min']}ms, mean={tot['mean']}ms, p50={tot['p50']}ms, p95={tot['p95']}ms, p99={tot['p99']}ms")

        # Evaluate target split
        metrics, outcomes = await evaluate_dataset(
            cases,
            policy,
            provider,
            split=args.split,
            cache=pg_cache,
            vector_store=pg_vector_store,
            dataset_hash=dataset_hash,
        )

        print(f"\n=== EVALUATION REPORT: SPLIT = '{args.split.upper()}' ===")
        print(f"Dataset SHA-256:        {dataset_hash[:16]}... ({len(cases)} total cases)")
        print(f"Cases Evaluated:        {metrics.total_cases}")
        print(f"Embedding Provider:     {actual_provider_name} ({provider.model_name}, dim={provider.dimensions})")
        print(f"Benchmark Mode:         {effective_benchmark_mode}")
        if db_info:
            print(f"PostgreSQL Version:     {db_info.get('postgres_version')}")
            print(f"pgvector Version:       {db_info.get('pgvector_version')}")
            print(f"Vector Index Type:      {db_info.get('vector_index_type', 'none')}")
            print(f"Retrieval Method:       {db_info.get('retrieval_method', 'Unknown')}")
            print(f"Database URL:           {db_info.get('database_url')}")
        print(f"Mock Embeddings Used:   {mock_used}")
        print(f"Mock Fallback Occurred: {mock_fallback_occurred}")
        print(f"Threshold:              {args.threshold:.2f}, Margin: {args.margin:.2f}")
        print(f"Confusion Matrix:       TP={metrics.tp}, FP={metrics.fp}, FN={metrics.fn}, TN={metrics.tn}")
        print(f"Precision:              {metrics.precision:.4f}")
        print(f"Recall:                 {metrics.recall:.4f}")
        print(f"F1 Score:               {metrics.f1_score:.4f}")
        print(f"False-Positive Rate:    {metrics.false_positive_rate:.4f}")
        print(f"Rejection Rate:         {metrics.rejection_rate:.4f}")
        print(f"Accuracy:               {metrics.accuracy:.4f}")
        print(f"Safe Reuse (TP):        {metrics.safe_reuse_count}")
        print(f"Incorrect Reuse (FP):   {metrics.incorrect_reuse_count}")
        print(f"Safe Calls Avoided:     {metrics.safe_provider_calls_avoided}")
        print(f"Hazardous Calls:        {metrics.hazardous_provider_calls_avoided}")
        print(f"Safe Tokens Saved:      {metrics.safe_tokens_saved} tokens")
        print(f"Safe Latency Saved:     {metrics.safe_latency_saved_ms:.2f} ms")
        if metrics.fp > 0:
            print(f"Hazardous Tokens Srvd:  {metrics.hazardous_tokens_involved} tokens (served inappropriately on false positives)")
            print(f"Zero-Defect Status:     FALSE (False positives detected - not zero defect)")
        else:
            print(f"Zero-Defect Status:     TRUE (No false positives on this partition)")

        print("\n--- Category Breakdown ---")
        print(f"{'Category':<24} {'Total':<6} {'TP':<4} {'FP':<4} {'FN':<4} {'TN':<4} {'Precision':<10} {'Recall':<8} {'FPR':<8}")
        print("-" * 76)
        for cat, cm in metrics.category_breakdown.items():
            print(
                f"{cat:<24} {cm['total']:<6} {cm['tp']:<4} {cm['fp']:<4} {cm['fn']:<4} {cm['tn']:<4} "
                f"{cm['precision']:<10.4f} {cm['recall']:<8.4f} {cm['false_positive_rate']:<8.4f}"
            )

        report_meta = {
            "dataset_hash": dataset_hash,
            "dataset_path": str(dataset_path),
            "dataset_split": args.split,
            "case_count": metrics.total_cases,
            "evaluation_timestamp": datetime.now(timezone.utc).isoformat(),
            "embedding_provider": actual_provider_name,
            "embedding_model": provider.model_name,
            "embedding_dimensions": provider.dimensions,
            "mock_embeddings_used": mock_used,
            "mock_fallback_occurred": mock_fallback_occurred,
            "similarity_threshold": args.threshold,
            "ambiguity_margin": args.margin,
            "benchmark_mode": effective_benchmark_mode,
            "vector_index_type": db_info.get("vector_index_type", "none"),
            "vector_index_available": db_info.get("vector_index_available", False),
            "vector_index_used": db_info.get("vector_index_used", False),
            "retrieval_method": db_info.get("retrieval_method", "In-memory exact cosine similarity"),
            "database_info": db_info,
        }

        json_path, csv_path = save_reports(
            metrics, outcomes, sweep_results, benchmark_results, out_dir, report_meta
        )
        print(f"\nSaved JSON report: {json_path}")
        print(f"Saved CSV report:  {csv_path}")

    finally:
        if db_pool is not None:
            await db_pool.close()
            logger.info("Closed test PostgreSQL connection pool.")
        await provider.close()


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
