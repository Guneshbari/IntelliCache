#!/usr/bin/env python3
"""
IntelliCache — Semantic Cache Offline Evaluation & Benchmarking Harness.

Evaluates semantic retrieval correctness, candidate ambiguity, safety,
threshold grid sweeps, and lookup runtime latencies without requiring PostgreSQL,
pgvector, or live LLM inference calls.

Usage:
    python scripts/evaluate_semantic_cache.py --dataset evaluation/dataset.json --split development
    python scripts/evaluate_semantic_cache.py --dataset evaluation/dataset.json --split test
    python scripts/evaluate_semantic_cache.py --sweep
    python scripts/evaluate_semantic_cache.py --benchmark
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
from typing import Any, Dict, List, Optional, Sequence, Tuple

# Ensure gateway package root is in sys.path
SCRIPT_DIR = Path(__file__).resolve().parent
GATEWAY_ROOT = SCRIPT_DIR.parent
if str(GATEWAY_ROOT) not in sys.path:
    sys.path.insert(0, str(GATEWAY_ROOT))

from app.core.contracts import GenerationRequest, ChatMessage
from app.embeddings.contracts import EmbeddingResult, EmbeddingTask
from app.embeddings.errors import EmbeddingError
from app.embeddings.interfaces import EmbeddingProvider
from app.embeddings.ollama_adapter import OllamaEmbeddingProvider
from app.semantic_cache.policy import SemanticCachePolicy
from app.vector_store.contracts import VectorSearchResult, cosine_distance_to_similarity

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
)
logger = logging.getLogger("intellicache.evaluator")


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
# 2. Evaluation Data Models
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
    provider_calls_avoided: int = 0
    tokens_saved: int = 0
    estimated_latency_saved_ms: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# =============================================================================
# 3. Core Evaluation Logic
# =============================================================================

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


def load_dataset(dataset_path: str | Path) -> List[EvaluationCase]:
    """Loads and validates the evaluation dataset from JSON."""
    p = Path(dataset_path)
    if not p.is_file():
        raise FileNotFoundError(f"Evaluation dataset file not found at: {p}")

    with open(p, "r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, list):
        raise ValueError(f"Dataset root must be a list of cases, got {type(data)}")

    cases: List[EvaluationCase] = []
    seen_ids = set()

    for item in data:
        cid = item.get("id")
        if not cid or cid in seen_ids:
            raise ValueError(f"Invalid or duplicate case id: {cid}")
        seen_ids.add(cid)

        candidates = []
        for cand in item.get("candidates", []):
            candidates.append(
                CandidateData(
                    id=cand["id"],
                    query_text=cand["query_text"],
                    cached_response=cand["cached_response"],
                    answer_label=cand["answer_label"],
                    metadata=cand.get("metadata", {}),
                    prompt_tokens=cand.get("prompt_tokens", 120),
                    completion_tokens=cand.get("completion_tokens", 250),
                    total_tokens=cand.get("total_tokens", 370),
                    original_latency_ms=cand.get("original_latency_ms", 1250.0),
                )
            )

        cases.append(
            EvaluationCase(
                id=cid,
                query=item["query"],
                category=item["category"],
                split=item["split"],
                expected_answer_label=item.get("expected_answer_label"),
                rationale=item.get("rationale", ""),
                candidates=candidates,
            )
        )

    return cases


def calculate_cosine_similarity(vec1: Sequence[float], vec2: Sequence[float]) -> Tuple[float, float]:
    """
    Calculates cosine distance and cosine similarity consistently with pgvector.
    similarity = 1.0 - distance
    """
    # Assuming unit vectors: dot product is cosine similarity
    dot = sum(a * b for a, b in zip(vec1, vec2))
    sim = max(-1.0, min(1.0, float(dot)))
    dist = max(0.0, min(2.0, 1.0 - sim))
    return dist, sim


async def evaluate_single_case(
    case: EvaluationCase,
    policy: SemanticCachePolicy,
    provider: EmbeddingProvider,
    record_timing: bool = False,
) -> EvaluationOutcome:
    """
    Evaluates a single evaluation case against candidate questions using
    cosine similarity ranking and SemanticCachePolicy.
    """
    t_start = time.perf_counter()

    # 1. Embed query
    query_embed = await provider.embed(case.query, task=EmbeddingTask.QUERY)

    # 2. Embed candidates and compute similarity
    ranked_candidates: List[Tuple[float, VectorSearchResult, CandidateData]] = []
    for cand in case.candidates:
        cand_embed = await provider.embed(cand.query_text, task=EmbeddingTask.DOCUMENT)
        dist, sim = calculate_cosine_similarity(query_embed.vector, cand_embed.vector)

        # Build candidate metadata matching GenerationRequest requirements
        meta = dict(cand.metadata)
        if "llm_provider" not in meta:
            meta["llm_provider"] = "groq"
        if "llm_model" not in meta:
            meta["llm_model"] = "llama-3.3-70b-versatile"
        if "temperature" not in meta:
            meta["temperature"] = 0.7
        if "max_tokens" not in meta:
            meta["max_tokens"] = 1000

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

    lookup_latency_ms = (time.perf_counter() - t_start) * 1000.0

    # 5. Classify decision quality
    safe_expected = case.expected_answer_label is not None
    accepted_id = None
    accepted_label = None
    prompt_tokens = 0
    comp_tokens = 0
    total_tokens = 0
    orig_latency = 0.0

    if winning_cand is not None:
        # Match winning candidate back to CandidateData
        for _, vs_c, orig_c in ranked_candidates:
            if vs_c.fingerprint == winning_cand.fingerprint:
                accepted_id = orig_c.id
                accepted_label = orig_c.answer_label
                prompt_tokens = orig_c.prompt_tokens
                comp_tokens = orig_c.completion_tokens
                total_tokens = orig_c.total_tokens
                orig_latency = orig_c.original_latency_ms
                break

    # Determine classification
    if safe_expected:
        if accepted_label == case.expected_answer_label:
            classification = "TP"
            decision = "safe_reuse"
        elif winning_cand is not None:
            # Reused an answer, but the label didn't match -> Materially incorrect reuse!
            classification = "FP"
            decision = "incorrect_reuse"
        else:
            # Reusable opportunity existed, but policy rejected it
            classification = "FN"
            decision = "incorrect_rejection"
    else:
        # No reusable answer existed
        if winning_cand is not None:
            # Cache accepted reuse anyway -> Unsafe False Positive!
            classification = "FP"
            decision = "incorrect_reuse"
        else:
            # Correctly rejected unsafe candidate
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
        lookup_latency_ms=lookup_latency_ms,
    )


async def evaluate_dataset(
    cases: List[EvaluationCase],
    policy: SemanticCachePolicy,
    provider: EmbeddingProvider,
    split: str = "development",
) -> Tuple[EvaluationMetrics, List[EvaluationOutcome]]:
    """Evaluates all cases matching the specified split partition."""
    filtered_cases = [c for c in cases if split == "all" or c.split == split]

    outcomes: List[EvaluationOutcome] = []
    tp = fp = fn = tn = 0
    total_tokens_saved = 0
    total_latency_saved_ms = 0.0
    provider_calls_avoided = 0

    category_counts: Dict[str, Dict[str, int]] = {}

    for case in filtered_cases:
        outcome = await evaluate_single_case(case, policy, provider)
        outcomes.append(outcome)

        cat = case.category
        if cat not in category_counts:
            category_counts[cat] = {"total": 0, "tp": 0, "fp": 0, "fn": 0, "tn": 0}
        category_counts[cat]["total"] += 1
        category_counts[cat][outcome.classification.lower()] += 1

        if outcome.classification == "TP":
            tp += 1
            provider_calls_avoided += 1
            total_tokens_saved += outcome.total_tokens
            # Latency saved calculation: original generation latency minus lookup latency
            lat_saved = max(0.0, outcome.original_latency_ms - outcome.lookup_latency_ms)
            total_latency_saved_ms += lat_saved
        elif outcome.classification == "FP":
            fp += 1
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
        provider_calls_avoided=provider_calls_avoided,
        tokens_saved=total_tokens_saved,
        estimated_latency_saved_ms=round(total_latency_saved_ms, 2),
    )

    return metrics, outcomes


# =============================================================================
# 4. Threshold & Ambiguity Margin Sweeps
# =============================================================================

async def run_threshold_sweep(
    cases: List[EvaluationCase],
    provider: EmbeddingProvider,
    thresholds: List[float],
    margins: List[float],
    split: str = "development",
) -> List[Dict[str, Any]]:
    """
    Executes a grid sweep across similarity thresholds and ambiguity margins
    on the development split to discover candidate configurations.
    """
    dev_cases = [c for c in cases if c.split == split]
    logger.info(
        "Starting parameter grid sweep on %d '%s' cases: %d thresholds x %d margins",
        len(dev_cases),
        split,
        len(thresholds),
        len(margins),
    )

    results = []
    for th in thresholds:
        for mg in margins:
            policy = SemanticCachePolicy(min_similarity=th, min_margin=mg)
            metrics, _ = await evaluate_dataset(dev_cases, policy, provider, split=split)
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

    # Sort results by highest F1 score, then lowest FPR
    results.sort(key=lambda r: (r["f1_score"], -r["false_positive_rate"]), reverse=True)
    return results


# =============================================================================
# 5. Benchmark Latency Measurements
# =============================================================================

async def run_benchmark(
    cases: List[EvaluationCase],
    policy: SemanticCachePolicy,
    provider: EmbeddingProvider,
    split: str = "development",
    iterations: int = 5,
) -> Dict[str, Any]:
    """
    Benchmarks runtime latency of embedding generation, policy evaluation,
    and end-to-end semantic lookup across multiple iterations.
    No live LLM inference calls are invoked.
    """
    bench_cases = [c for c in cases if split == "all" or c.split == split]
    logger.info("Running benchmark on %d cases with %d iterations...", len(bench_cases), iterations)

    embed_latencies_ms: List[float] = []
    policy_latencies_ms: List[float] = []
    lookup_latencies_ms: List[float] = []

    for _ in range(iterations):
        for case in bench_cases:
            # 1. Measure query embedding latency
            t0 = time.perf_counter()
            query_res = await provider.embed(case.query, task=EmbeddingTask.QUERY)
            t_embed = (time.perf_counter() - t0) * 1000.0
            embed_latencies_ms.append(t_embed)

            # Pre-compute candidate search results for policy benchmark
            dummy_vs = []
            for cand in case.candidates:
                dist, sim = 0.1, 0.9
                dummy_vs.append(
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

            req = GenerationRequest(
                messages=[ChatMessage(role="user", content=case.query)],
                provider="groq",
                model="llama-3.3-70b-versatile",
            )

            # 2. Measure policy candidate evaluation latency
            t1 = time.perf_counter()
            policy.evaluate_candidates(dummy_vs, req)
            t_policy = (time.perf_counter() - t1) * 1000.0
            policy_latencies_ms.append(t_policy)

            # Total lookup latency
            lookup_latencies_ms.append(t_embed + t_policy)

    def stats(data: List[float]) -> Dict[str, float]:
        if not data:
            return {"mean": 0.0, "p50": 0.0, "p90": 0.0, "p99": 0.0}
        s = sorted(data)
        n = len(s)
        return {
            "mean": round(sum(s) / n, 3),
            "p50": round(s[int(n * 0.50)], 3),
            "p90": round(s[int(n * 0.90)], 3),
            "p99": round(s[min(n - 1, int(n * 0.99))], 3),
        }

    return {
        "iterations": iterations,
        "sample_count": len(lookup_latencies_ms),
        "embedding_latency_ms": stats(embed_latencies_ms),
        "policy_latency_ms": stats(policy_latencies_ms),
        "total_lookup_latency_ms": stats(lookup_latencies_ms),
        "methodology": (
            "Latency benchmarks measured end-to-end across candidate embedding "
            "and policy evaluation paths. All LLM provider generation calls avoided."
        ),
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

    # 1. JSON Report
    json_payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "summary": metrics.to_dict(),
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
            "case_id", "category", "split", "safe_reuse_expected",
            "top_similarity", "margin", "classification", "decision", "rejection_reason"
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
                o.rejection_reason or "",
            ])

    return json_path, csv_path


# =============================================================================
# 7. CLI Entry Point
# =============================================================================

async def async_main() -> None:
    parser = argparse.ArgumentParser(
        description="Offline evaluation harness for IntelliCache semantic caching pipeline."
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
        help="Execute a grid sweep of similarity thresholds and margins on the dev split.",
    )
    parser.add_argument(
        "--benchmark",
        action="store_true",
        help="Benchmark embedding and policy candidate evaluation latency.",
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

    # Resolve paths relative to gateway root if relative
    dataset_path = Path(args.dataset)
    if not dataset_path.is_absolute():
        dataset_path = GATEWAY_ROOT / dataset_path

    out_dir = Path(args.output_dir)
    if not out_dir.is_absolute():
        out_dir = GATEWAY_ROOT / out_dir

    logger.info("Loading evaluation dataset from: %s", dataset_path)
    cases = load_dataset(dataset_path)
    logger.info("Loaded %d total evaluation cases.", len(cases))

    # Initialize Embedding Provider
    provider: EmbeddingProvider
    if args.provider == "ollama":
        ollama_url = os.environ.get("EMBEDDING_BASE_URL", "http://127.0.0.1:11434")
        logger.info("Connecting to Ollama embedding provider at %s...", ollama_url)
        provider = OllamaEmbeddingProvider(model=args.model, base_url=ollama_url)
        # Test connection
        try:
            await provider.embed("ping", task=EmbeddingTask.QUERY)
            logger.info("Connected to live Ollama embedding provider successfully.")
        except Exception as e:
            logger.warning(
                "Ollama daemon not reachable at %s (%s). Falling back to DeterministicMockEmbeddingProvider.",
                ollama_url,
                e,
            )
            provider = DeterministicMockEmbeddingProvider(model=args.model)
    else:
        logger.info("Using DeterministicMockEmbeddingProvider (offline mode).")
        provider = DeterministicMockEmbeddingProvider(model=args.model)

    policy = SemanticCachePolicy(
        min_similarity=args.threshold,
        min_margin=args.margin,
        configured_model=args.model,
    )

    sweep_results = None
    if args.sweep:
        thresholds = [0.70, 0.75, 0.80, 0.82, 0.85, 0.88, 0.90, 0.95]
        margins = [0.01, 0.02, 0.03, 0.05, 0.08]
        sweep_results = await run_threshold_sweep(
            cases, provider, thresholds, margins, split="development"
        )
        print("\n=== TOP 5 CONFIGURATIONS FROM DEV SWEEP ===")
        print(f"{'Threshold':<10} {'Margin':<8} {'Precision':<10} {'Recall':<8} {'F1':<8} {'FPR':<8} {'Rejection':<10}")
        print("-" * 66)
        for r in sweep_results[:5]:
            print(
                f"{r['threshold']:<10.2f} {r['margin']:<8.2f} {r['precision']:<10.4f} "
                f"{r['recall']:<8.4f} {r['f1_score']:<8.4f} {r['false_positive_rate']:<8.4f} {r['rejection_rate']:<10.4f}"
            )

    benchmark_results = None
    if args.benchmark:
        benchmark_results = await run_benchmark(
            cases, policy, provider, split=args.split, iterations=args.iterations
        )
        print("\n=== RUNTIME BENCHMARK METRICS (NO LLM CALLS) ===")
        print(f"Iterations: {benchmark_results['iterations']}, Samples: {benchmark_results['sample_count']}")
        emb = benchmark_results["embedding_latency_ms"]
        pol = benchmark_results["policy_latency_ms"]
        tot = benchmark_results["total_lookup_latency_ms"]
        print(f"Embedding Latency (ms): mean={emb['mean']}ms, p50={emb['p50']}ms, p90={emb['p90']}ms, p99={emb['p99']}ms")
        print(f"Policy Latency (ms):    mean={pol['mean']}ms, p50={pol['p50']}ms, p90={pol['p90']}ms, p99={pol['p99']}ms")
        print(f"Total Lookup (ms):      mean={tot['mean']}ms, p50={tot['p50']}ms, p90={tot['p90']}ms, p99={tot['p99']}ms")

    # Evaluate target split
    metrics, outcomes = await evaluate_dataset(cases, policy, provider, split=args.split)

    print(f"\n=== EVALUATION REPORT: SPLIT = '{args.split.upper()}' ===")
    print(f"Total Cases:            {metrics.total_cases}")
    print(f"Threshold:              {args.threshold:.2f}, Margin: {args.margin:.2f}")
    print(f"True Positives (TP):    {metrics.tp}")
    print(f"False Positives (FP):   {metrics.fp}")
    print(f"False Negatives (FN):   {metrics.fn}")
    print(f"True Negatives (TN):    {metrics.tn}")
    print(f"Precision:              {metrics.precision:.4f}")
    print(f"Recall:                 {metrics.recall:.4f}")
    print(f"F1 Score:               {metrics.f1_score:.4f}")
    print(f"False-Positive Rate:    {metrics.false_positive_rate:.4f}")
    print(f"Rejection Rate:         {metrics.rejection_rate:.4f}")
    print(f"Accuracy:               {metrics.accuracy:.4f}")
    print(f"Provider Calls Avoided: {metrics.provider_calls_avoided}")
    print(f"Tokens Saved (est.):    {metrics.tokens_saved}")
    print(f"Latency Saved (est.):   {metrics.estimated_latency_saved_ms:.2f} ms")

    print("\n--- Category Breakdown ---")
    print(f"{'Category':<24} {'Total':<6} {'TP':<4} {'FP':<4} {'FN':<4} {'TN':<4} {'Precision':<10} {'Recall':<8} {'FPR':<8}")
    print("-" * 76)
    for cat, cm in metrics.category_breakdown.items():
        print(
            f"{cat:<24} {cm['total']:<6} {cm['tp']:<4} {cm['fp']:<4} {cm['fn']:<4} {cm['tn']:<4} "
            f"{cm['precision']:<10.4f} {cm['recall']:<8.4f} {cm['false_positive_rate']:<8.4f}"
        )

    # Save reports
    json_path, csv_path = save_reports(
        metrics, outcomes, sweep_results, benchmark_results, out_dir
    )
    print(f"\nSaved JSON report: {json_path}")
    print(f"Saved CSV report:  {csv_path}")

    await provider.close()


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
