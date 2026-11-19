"""
IntelliCache Stage 3.2.5 Scenario Dataset Generator.

Converts the Stage 3.2.4 ground-truth dataset and held-out test split into
end-to-end evaluation scenarios for live pipeline execution:
1. Primary Held-Out Test Scenarios (252 scenarios: 84 positive + 168 hard-negative)
2. Controlled Ambiguity Scenarios (12 scenarios: conflicting, concordant, and clear margins)
3. Compatibility Guardrail Scenarios (60 metadata-incompatible scenarios)
4. Infrastructure and Failure Fallback Scenarios (5 scenarios: empty, expired, cross-ns, embed-fail, db-fail)

Produces:
- gateway/evaluation/datasets/stage3_2_5_scenarios.json
- gateway/evaluation/datasets/stage3_2_5_dataset_metadata.json
"""

from __future__ import annotations

from collections import defaultdict
import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Tuple


def build_stage3_2_5_scenarios(
    stage3_2_4_pairs_path: Path | str = "gateway/evaluation/datasets/stage3_2_4_semantic_pairs.json",
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Builds the comprehensive Stage 3.2.5 scenario suite."""
    pairs_file = Path(stage3_2_4_pairs_path)
    with open(pairs_file, "r", encoding="utf-8") as f:
        pairs = json.load(f)

    # 1. Extract Primary Held-Out Test Scenarios
    test_compat_pairs = [p for p in pairs if p["split"] == "test" and p["label"] != "incompatible"]
    if len(test_compat_pairs) != 252:
        raise ValueError(f"Expected exactly 252 held-out compatible test pairs, found {len(test_compat_pairs)}")

    primary_scenarios: List[Dict[str, Any]] = []
    test_group_ids = sorted(list(set(p["intent_group_id"] for p in test_compat_pairs)))

    # Track canonical queries for target pool (one per test intent group)
    canonical_targets: Dict[str, Dict[str, Any]] = {}
    for p in test_compat_pairs:
        gid = p["intent_group_id"]
        if gid not in canonical_targets:
            canonical_targets[gid] = {
                "intent_group_id": gid,
                "domain": p["domain"],
                "subtopic": p["subtopic"],
                "canonical_query": p["query_a"],
                "canonical_response": p["cached_response_a"],
                "metadata": p["compatibility_metadata"],
            }

    # 42 Distractor candidates from train/calibration splits, domain-stratified
    train_cal_pairs = [p for p in pairs if p["split"] in ("train", "calibration") and p["label"] != "incompatible"]
    
    domain_to_groups: Dict[str, Dict[str, Dict[str, Any]]] = defaultdict(dict)
    for p in train_cal_pairs:
        gid = p["intent_group_id"]
        d = p["domain"]
        if gid not in domain_to_groups[d]:
            domain_to_groups[d][gid] = {
                "intent_group_id": gid,
                "domain": d,
                "subtopic": p["subtopic"],
                "canonical_query": p["query_a"],
                "canonical_response": p["cached_response_a"],
                "metadata": p["compatibility_metadata"],
            }

    sorted_domains = sorted(domain_to_groups.keys())
    if len(sorted_domains) != 10:
        raise ValueError(f"Expected 10 domains in train/calibration data, found {len(sorted_domains)}")

    distractor_targets: List[Dict[str, Any]] = []
    for d_idx, d_name in enumerate(sorted_domains):
        quota = 5 if d_idx < 2 else 4
        d_gids = sorted(domain_to_groups[d_name].keys())
        if len(d_gids) < quota:
            raise ValueError(f"Domain '{d_name}' has only {len(d_gids)} groups, required {quota}")
        for gid in d_gids[:quota]:
            distractor_targets.append(domain_to_groups[d_name][gid])

    if len(distractor_targets) != 42:
        raise ValueError(f"Expected exactly 42 distractor targets, got {len(distractor_targets)}")

    # Build primary scenario items
    for idx, p in enumerate(test_compat_pairs, 1):
        is_pos = (p["label"] == "positive")
        scen_id = f"s325_prim_{idx:04d}"
        expected_response = p["cached_response_a"] if is_pos else None
        
        primary_scenarios.append({
            "scenario_id": scen_id,
            "source_pair_id": p["pair_id"],
            "intent_group_id": p["intent_group_id"],
            "domain": p["domain"],
            "subtopic": p["subtopic"],
            "scenario_category": "primary_held_out",
            "scenario_type": "safe_positive" if is_pos else "hard_negative",
            "hard_negative_type": p.get("hard_negative_type"),
            "incoming_query": p["query_b"],
            "target_canonical_query": p["query_a"],
            "expected_safe_response": expected_response,
            "expected_outcome": "cache_hit" if is_pos else "upstream_fallback",
            "metadata": p["compatibility_metadata"],
        })

    # 2. Controlled Ambiguity Scenarios (12 scenarios)
    ambiguity_scenarios: List[Dict[str, Any]] = []
    
    ambig_meta = {
        "schema_version": 2,
        "extractor_version": "compat-extractor-v2",
        "task_kind": {"status": "KNOWN", "value": "conceptual"},
        "dimensions": {
            "language": {"status": "NOT_APPLICABLE", "value": None},
            "language_version": {"status": "NOT_APPLICABLE", "value": None},
            "framework": {"status": "NOT_APPLICABLE", "value": None},
            "framework_version": {"status": "NOT_APPLICABLE", "value": None},
            "runtime": {"status": "NOT_APPLICABLE", "value": None},
            "runtime_version": {"status": "NOT_APPLICABLE", "value": None},
            "execution_model": {"status": "NOT_APPLICABLE", "value": None},
            "output_format": {"status": "NOT_APPLICABLE", "value": None},
            "constraints": {"status": "NOT_APPLICABLE", "value": None},
        },
        "generation_contract": {
            "provider": "groq",
            "model": "qwen/qwen3.8-27b",
            "temperature": 0.7,
            "max_tokens": 1000,
        },
        "embedding_contract": {
            "model": "BAAI/bge-small-en-v1.5",
        },
        "llm_provider": "groq",
        "llm_model": "qwen/qwen3.8-27b",
        "temperature": 0.7,
        "max_tokens": 1000,
    }

    # Conflicting responses within margin delta < 0.03
    # Explicit ground-truth answers for each conflicting pair to rigorously classify
    # whether ambiguity rejection prevented a false positive (top was wrong) or caused
    # an erroneous false negative (top was correct).
    conflicting_fixtures = [
        {
            "id": "s325_ambig_conflicting_01",
            "query": "In Python asyncio, what exception is raised when a task is cancelled?",
            "top_q": "What exception does Python asyncio task cancellation raise?",
            "top_r": "Task cancellation in Python asyncio raises KeyboardInterrupt.",  # WRONG
            "run_q": "What error is raised when an asyncio task is cancelled in Python?",
            "run_r": "Task cancellation in Python asyncio raises asyncio.CancelledError.",  # CORRECT
            "gt_r": "Task cancellation in Python asyncio raises asyncio.CancelledError.",
            "top_is_correct": False,
        },
        {
            "id": "s325_ambig_conflicting_02",
            "query": "In PostgreSQL, what is the default network port number?",
            "top_q": "What port does PostgreSQL default to?",
            "top_r": "The default port for PostgreSQL is 3306.",  # WRONG (MySQL port)
            "run_q": "Default PostgreSQL listening port?",
            "run_r": "The default port for PostgreSQL is 5432.",  # CORRECT
            "gt_r": "The default port for PostgreSQL is 5432.",
            "top_is_correct": False,
        },
        {
            "id": "s325_ambig_conflicting_03",
            "query": "What does dict.get('missing') return in Python if no default is provided?",
            "top_q": "Return value of Python dict.get() for missing key without default?",
            "top_r": "In Python, dict.get() returns None if the key is missing and no default is provided.",  # CORRECT
            "run_q": "What happens when dict.get() key is not found in Python?",
            "run_r": "In Python, dict.get() raises KeyError if the key is missing and no default is provided.",  # WRONG
            "gt_r": "In Python, dict.get() returns None if the key is missing and no default is provided.",
            "top_is_correct": True,
        },
        {
            "id": "s325_ambig_conflicting_04",
            "query": "In HTTP response status codes, what does 404 Not Found indicate?",
            "top_q": "What does HTTP status code 404 mean?",
            "top_r": "HTTP 404 indicates the origin server cannot find the requested resource.",  # CORRECT
            "run_q": "Meaning of 404 status code in HTTP?",
            "run_r": "HTTP 404 indicates unauthorized access requiring user credentials.",  # WRONG
            "gt_r": "HTTP 404 indicates the origin server cannot find the requested resource.",
            "top_is_correct": True,
        },
    ]

    for fix in conflicting_fixtures:
        ambiguity_scenarios.append({
            "scenario_id": fix["id"],
            "scenario_category": "ambiguity_controlled",
            "scenario_type": "ambiguous_competing_candidates",
            "incoming_query": fix["query"],
            "top_candidate": {
                "query": fix["top_q"],
                "response": fix["top_r"],
                "similarity": 0.940,
                "distance": 0.060,
                "metadata": ambig_meta,
            },
            "runner_up_candidate": {
                "query": fix["run_q"],
                "response": fix["run_r"],
                "similarity": 0.925,  # diff = 0.015 < 0.030
                "distance": 0.075,
                "metadata": ambig_meta,
            },
            "ground_truth_response": fix["gt_r"],
            "expected_safe_response": fix["gt_r"] if fix["top_is_correct"] else None,
            "top_candidate_is_correct": fix["top_is_correct"],
            "expected_outcome": "ambiguous_rejection",
        })

    # Concordant responses within margin delta < 0.03
    for i in range(1, 5):
        common_resp = f"Concordant common response answer for test {i}"
        ambiguity_scenarios.append({
            "scenario_id": f"s325_ambig_concordant_{i:02d}",
            "scenario_category": "ambiguity_controlled",
            "scenario_type": "concordant_candidates",
            "incoming_query": f"Ambiguous test query with concordant identical candidates {i}",
            "top_candidate": {
                "query": f"Concordant top candidate query {i}",
                "response": common_resp,
                "similarity": 0.940,
                "distance": 0.060,
                "metadata": ambig_meta,
            },
            "runner_up_candidate": {
                "query": f"Concordant runner up candidate query {i}",
                "response": common_resp,                  # Identical!
                "similarity": 0.930,                     # diff = 0.010 < 0.030
                "distance": 0.070,
                "metadata": ambig_meta,
            },
            "ground_truth_response": common_resp,
            "expected_safe_response": common_resp,
            "top_candidate_is_correct": True,
            "expected_outcome": "concordant_hit",
        })

    # Distinct responses outside margin delta >= 0.03
    for i in range(1, 5):
        clear_resp = f"Clear Top Response {i}"
        ambiguity_scenarios.append({
            "scenario_id": f"s325_ambig_clear_margin_{i:02d}",
            "scenario_category": "ambiguity_controlled",
            "scenario_type": "unambiguous_competing_candidates",
            "incoming_query": f"Ambiguous test query with clear margin {i}",
            "top_candidate": {
                "query": f"Clear top candidate query {i}",
                "response": clear_resp,
                "similarity": 0.945,
                "distance": 0.055,
                "metadata": ambig_meta,
            },
            "runner_up_candidate": {
                "query": f"Clear runner up query {i}",
                "response": f"Clear Alternative Response {i}",
                "similarity": 0.890,                     # diff = 0.055 >= 0.030
                "distance": 0.110,
                "metadata": ambig_meta,
            },
            "ground_truth_response": clear_resp,
            "expected_safe_response": clear_resp,
            "top_candidate_is_correct": True,
            "expected_outcome": "cache_hit",
        })

    # 3. Compatibility Guardrail Scenarios (60 scenarios from Stage 3.2.4)
    incompat_pairs = [p for p in pairs if p["label"] == "incompatible"]
    if len(incompat_pairs) != 60:
        raise ValueError(f"Expected exactly 60 incompatible pairs, found {len(incompat_pairs)}")

    compatibility_scenarios: List[Dict[str, Any]] = []
    for idx, p in enumerate(incompat_pairs, 1):
        compatibility_scenarios.append({
            "scenario_id": f"s325_compat_{idx:04d}",
            "source_pair_id": p["pair_id"],
            "domain": p["domain"],
            "subtopic": p["subtopic"],
            "scenario_category": "compatibility_guardrail",
            "scenario_type": "incompatible_metadata",
            "incompatibility_category": p.get("reason", "unknown_mismatch"),
            "incoming_query": p["query_b"],
            "target_canonical_query": p["query_a"],
            "candidate_response": p["cached_response_a"],
            "candidate_metadata": p["compatibility_metadata"],
            "expected_safe_response": None,
            "expected_outcome": "compatibility_rejection",
        })

    # 4. Infrastructure & Failure Scenarios (5 scenarios)
    # Uses demonstrably eligible queries so requests proceed past eligibility
    # checks and genuinely reach the target infrastructure component being tested.
    failure_scenarios = [
        {
            "scenario_id": "s325_fail_no_candidate",
            "scenario_category": "infrastructure_fallback",
            "scenario_type": "no_candidate",
            "incoming_query": "How do I write a Python function to parse JSON data?",
            "description": "Empty candidate pool in target namespace.",
            "expected_safe_response": None,
            "expected_outcome": "upstream_fallback",
        },
        {
            "scenario_id": "s325_fail_expired_candidate",
            "scenario_category": "infrastructure_fallback",
            "scenario_type": "expired_candidate",
            "incoming_query": "Explain how to implement binary search in Python with code",
            "description": "Candidate vector exists in vector_records, but joined cache_entries record is expired.",
            "expected_safe_response": None,
            "expected_outcome": "upstream_fallback",
        },
        {
            "scenario_id": "s325_fail_namespace_mismatch",
            "scenario_category": "infrastructure_fallback",
            "scenario_type": "namespace_mismatch",
            "incoming_query": "How to query PostgreSQL database table using SQL select in Python?",
            "description": "Candidate exists only in another namespace, ensuring namespace isolation.",
            "expected_safe_response": None,
            "expected_outcome": "upstream_fallback",
        },
        {
            "scenario_id": "s325_fail_embedding_error",
            "scenario_category": "infrastructure_fallback",
            "scenario_type": "embedding_failure",
            "incoming_query": "Write a Python script to compute moving averages with pandas.",
            "description": "Simulated embedding provider failure or timeout causes fail-open upstream generation.",
            "expected_safe_response": None,
            "expected_outcome": "upstream_fallback",
        },
        {
            "scenario_id": "s325_fail_vector_store_error",
            "scenario_category": "infrastructure_fallback",
            "scenario_type": "vector_store_failure",
            "incoming_query": "How do I configure TCP socket keepalive in Python network script?",
            "description": "Simulated vector store failure causes fail-open upstream generation.",
            "expected_safe_response": None,
            "expected_outcome": "upstream_fallback",
        },
    ]

    all_scenarios = {
        "primary_held_out": primary_scenarios,
        "ambiguity_controlled": ambiguity_scenarios,
        "compatibility_guardrail": compatibility_scenarios,
        "infrastructure_fallback": failure_scenarios,
    }

    # Metadata & Manifest
    scenario_counts = {
        "primary_held_out": len(primary_scenarios),
        "primary_positive": sum(1 for s in primary_scenarios if s["scenario_type"] == "safe_positive"),
        "primary_negative": sum(1 for s in primary_scenarios if s["scenario_type"] == "hard_negative"),
        "ambiguity_controlled": len(ambiguity_scenarios),
        "compatibility_guardrail": len(compatibility_scenarios),
        "infrastructure_fallback": len(failure_scenarios),
        "total_scenarios": len(primary_scenarios) + len(ambiguity_scenarios) + len(compatibility_scenarios) + len(failure_scenarios),
    }

    metadata = {
        "scenario_dataset_name": "Stage 3.2.5 End-to-End Semantic Cache Evaluation Scenarios",
        "version": "1.0.0",
        "base_dataset_sha256": "b13d69c9024246136f69b189d3f0801a4e71de1d1f9362761faa943a7a913da5",
        "held_out_intent_groups": len(test_group_ids),
        "target_candidate_pool_size": len(canonical_targets),
        "distractor_candidate_pool_size": len(distractor_targets),
        "total_candidate_pool_size": len(canonical_targets) + len(distractor_targets),
        "scenario_counts": scenario_counts,
        "canonical_targets": list(canonical_targets.values()),
        "distractor_targets": distractor_targets,
    }

    return all_scenarios, metadata


def save_stage3_2_5_scenarios(
    output_dir: Path | str = "gateway/evaluation/datasets",
) -> Tuple[Path, Path]:
    """Generates scenario files and writes them to the specified directory."""
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    scenarios, metadata = build_stage3_2_5_scenarios()
    scen_file = out_dir / "stage3_2_5_scenarios.json"
    meta_file = out_dir / "stage3_2_5_dataset_metadata.json"

    with open(scen_file, "w", encoding="utf-8") as f:
        json.dump(scenarios, f, indent=2, ensure_ascii=False)

    with open(meta_file, "w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=2, ensure_ascii=False)

    return scen_file, meta_file


if __name__ == "__main__":
    s_path, m_path = save_stage3_2_5_scenarios()
    print(f"Generated scenarios: {s_path}")
    print(f"Generated metadata:  {m_path}")
