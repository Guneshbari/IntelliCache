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

    # 42 Distractor candidates from train/calibration splits
    train_cal_pairs = [p for p in pairs if p["split"] in ("train", "calibration") and p["label"] != "incompatible"]
    train_cal_groups = sorted(list(set(p["intent_group_id"] for p in train_cal_pairs)))
    
    # Pick 42 distinct distractor intent groups (spread across domains)
    distractor_targets: List[Dict[str, Any]] = []
    seen_distractor_groups = set()
    for p in train_cal_pairs:
        gid = p["intent_group_id"]
        if gid not in seen_distractor_groups and len(distractor_targets) < 42:
            seen_distractor_groups.add(gid)
            distractor_targets.append({
                "intent_group_id": gid,
                "domain": p["domain"],
                "subtopic": p["subtopic"],
                "canonical_query": p["query_a"],
                "canonical_response": p["cached_response_a"],
                "metadata": p["compatibility_metadata"],
            })

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
    
    # Conflicting responses within margin delta < 0.03
    for i in range(1, 5):
        ambiguity_scenarios.append({
            "scenario_id": f"s325_ambig_conflicting_{i:02d}",
            "scenario_category": "ambiguity_controlled",
            "scenario_type": "ambiguous_competing_candidates",
            "incoming_query": f"Ambiguous test query with competing conflicting candidates {i}",
            "top_candidate": {
                "query": f"Top candidate query {i}",
                "response": f"Response Option Alpha {i}",
                "similarity": 0.940,
                "distance": 0.060,
            },
            "runner_up_candidate": {
                "query": f"Runner up candidate query {i}",
                "response": f"Response Option Beta {i}",  # Conflicting!
                "similarity": 0.925,                     # diff = 0.015 < 0.030
                "distance": 0.075,
            },
            "expected_safe_response": None,
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
            },
            "runner_up_candidate": {
                "query": f"Concordant runner up candidate query {i}",
                "response": common_resp,                  # Identical!
                "similarity": 0.930,                     # diff = 0.010 < 0.030
                "distance": 0.070,
            },
            "expected_safe_response": common_resp,
            "expected_outcome": "concordant_hit",
        })

    # Distinct responses outside margin delta >= 0.03
    for i in range(1, 5):
        ambiguity_scenarios.append({
            "scenario_id": f"s325_ambig_clear_margin_{i:02d}",
            "scenario_category": "ambiguity_controlled",
            "scenario_type": "unambiguous_competing_candidates",
            "incoming_query": f"Ambiguous test query with clear margin {i}",
            "top_candidate": {
                "query": f"Clear top candidate query {i}",
                "response": f"Clear Top Response {i}",
                "similarity": 0.945,
                "distance": 0.055,
            },
            "runner_up_candidate": {
                "query": f"Clear runner up query {i}",
                "response": f"Clear Alternative Response {i}",
                "similarity": 0.890,                     # diff = 0.055 >= 0.030
                "distance": 0.110,
            },
            "expected_safe_response": f"Clear Top Response {i}",
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
    failure_scenarios = [
        {
            "scenario_id": "s325_fail_no_candidate",
            "scenario_category": "infrastructure_fallback",
            "scenario_type": "no_candidate",
            "incoming_query": "Explain how distributed snapshotting works in Chandy-Lamport algorithm.",
            "description": "Empty candidate pool in target namespace.",
            "expected_safe_response": None,
            "expected_outcome": "upstream_fallback",
        },
        {
            "scenario_id": "s325_fail_expired_candidate",
            "scenario_category": "infrastructure_fallback",
            "scenario_type": "expired_candidate",
            "incoming_query": "Explain Raft consensus leader election and term heartbeats.",
            "description": "Candidate vector exists in vector_records, but joined cache_entries record is expired.",
            "expected_safe_response": None,
            "expected_outcome": "upstream_fallback",
        },
        {
            "scenario_id": "s325_fail_namespace_mismatch",
            "scenario_category": "infrastructure_fallback",
            "scenario_type": "namespace_mismatch",
            "incoming_query": "How does PostgreSQL pgvector perform HNSW vector index construction?",
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
            "incoming_query": "What is the difference between TCP Reno and TCP BBR congestion control?",
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
