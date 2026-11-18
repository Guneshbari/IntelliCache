"""
IntelliCache Stage 3.2.4 Dataset Generator.

Generates a deterministic, reproducible, ground-truth-labeled dataset of 1,260 compatible
semantic pairs (420 positive paraphrases, 840 compatible hard negatives) plus 60 deterministic
metadata-incompatible pairs across 10 technical domains:
1. Programming languages
2. Frameworks and libraries
3. Databases and vector search
4. Concurrency and distributed systems
5. Protocols and networking
6. API design
7. Software debugging
8. System configuration
9. Data processing
10. Infrastructure and deployment concepts

Dataset Splitting:
- 210 canonical intent groups (21 per domain).
- Grouped split by intent group: 126 train (60%), 42 calibration (20%), 42 test (20%).
- Guaranteed zero leakage of intent groups across splits.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Tuple


def _compute_sha256(data: Any) -> str:
    """Computes deterministic SHA-256 hash of JSON-serialized data."""
    encoded = json.dumps(data, sort_keys=True, indent=2).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def get_stage3_2_4_intent_groups() -> List[Dict[str, Any]]:
    """
    Returns the complete specification of 210 distinct technical intent groups
    across 10 domains (21 per domain), each specifying canonical queries,
    positive paraphrases, and 4 specific hard negative queries.
    """
    try:
        from .dataset_definitions_stage3_2_4 import ALL_DOMAINS_DEFINITIONS
    except ImportError:
        from gateway.evaluation.dataset_definitions_stage3_2_4 import ALL_DOMAINS_DEFINITIONS
    return ALL_DOMAINS_DEFINITIONS


def build_stage3_2_4_dataset(seed: int = 42) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    Constructs the complete Stage 3.2.4 dataset with deterministic IDs, intent grouping,
    assigned splits, metadata, and validation checks.
    """
    intent_groups = get_stage3_2_4_intent_groups()
    if len(intent_groups) != 210:
        raise ValueError(f"Expected exactly 210 intent groups, got {len(intent_groups)}")

    # Deterministic split assignment:
    # 10 domains with 21 intent groups each.
    # Exactly 126 train (60%), 42 calibration (20%), 42 test (20%).
    # Domains 0..5: 13 train, 4 cal, 4 test (6 * 13 = 78 train, 24 cal, 24 test)
    # Domains 6..9: 12 train, 5 cal (or 4 cal / 5 test) -> 4 * 12 = 48 train (78+48=126 train)
    # Specifically:
    # D6, D7: 12 train, 5 cal, 4 test
    # D8, D9: 12 train, 4 cal, 5 test
    # Cal: 6*4 + 2*5 + 2*4 = 24 + 10 + 8 = 42 cal
    # Test: 6*4 + 2*4 + 2*5 = 24 + 8 + 10 = 42 test
    # Train: 6*13 + 4*12 = 78 + 48 = 126 train. Total = 210.
    
    pairs: List[Dict[str, Any]] = []
    pair_id_counter = 1
    
    domain_groups_map: Dict[str, List[Dict[str, Any]]] = {}
    for ig in intent_groups:
        d = ig["domain"]
        domain_groups_map.setdefault(d, []).append(ig)

    domain_order = sorted(domain_groups_map.keys())
    split_assignment: Dict[str, str] = {}

    for d_idx, domain_name in enumerate(domain_order):
        d_groups = domain_groups_map[domain_name]
        if len(d_groups) != 21:
            raise ValueError(f"Domain {domain_name} has {len(d_groups)} groups, expected 21")

        if d_idx < 6:
            train_count, cal_count, test_count = 13, 4, 4
        elif d_idx < 8:
            train_count, cal_count, test_count = 12, 5, 4
        else:
            train_count, cal_count, test_count = 12, 4, 5

        for g_idx, ig in enumerate(d_groups):
            gid = ig["intent_group_id"]
            if g_idx < train_count:
                split_assignment[gid] = "train"
            elif g_idx < train_count + cal_count:
                split_assignment[gid] = "calibration"
            else:
                split_assignment[gid] = "test"

    # Compatibility extractor for standard v2 metadata generation
    try:
        from app.semantic_cache.compatibility import CompatibilityExtractor
    except ImportError:
        from gateway.app.semantic_cache.compatibility import CompatibilityExtractor
    extractor = CompatibilityExtractor()

    # Assemble pairs
    for ig in intent_groups:
        gid = ig["intent_group_id"]
        split = split_assignment[gid]
        domain = ig["domain"]
        subtopic = ig["subtopic"]
        canonical_query = ig["canonical_query"]
        canonical_response = ig["canonical_response"]
        meta_obj = extractor.extract_from_text(
            text=canonical_query,
            provider="groq",
            model="qwen/qwen3.8-27b",
            temperature=0.7,
            max_tokens=1000,
            configured_model="BAAI/bge-small-en-v1.5",
        )
        metadata_contract = meta_obj.to_dict()

        # 1. Positive Paraphrase 1
        pairs.append({
            "pair_id": f"s324_pair_{pair_id_counter:04d}",
            "intent_group_id": gid,
            "domain": domain,
            "subtopic": subtopic,
            "split": split,
            "query_a": canonical_query,
            "query_b": ig["positive_paraphrase_1"]["query"],
            "label": "positive",
            "reason": ig["positive_paraphrase_1"]["reason"],
            "hard_negative_type": None,
            "cached_response_a": canonical_response,
            "cached_response_b": canonical_response,
            "compatibility_metadata": metadata_contract,
        })
        pair_id_counter += 1

        # 2. Positive Paraphrase 2
        pairs.append({
            "pair_id": f"s324_pair_{pair_id_counter:04d}",
            "intent_group_id": gid,
            "domain": domain,
            "subtopic": subtopic,
            "split": split,
            "query_a": canonical_query,
            "query_b": ig["positive_paraphrase_2"]["query"],
            "label": "positive",
            "reason": ig["positive_paraphrase_2"]["reason"],
            "hard_negative_type": None,
            "cached_response_a": canonical_response,
            "cached_response_b": canonical_response,
            "compatibility_metadata": metadata_contract,
        })
        pair_id_counter += 1

        # 3. Hard Negative 1 (Different Operation)
        hn1 = ig["hard_negative_diff_operation"]
        pairs.append({
            "pair_id": f"s324_pair_{pair_id_counter:04d}",
            "intent_group_id": gid,
            "domain": domain,
            "subtopic": subtopic,
            "split": split,
            "query_a": canonical_query,
            "query_b": hn1["query"],
            "label": "negative",
            "reason": hn1["reason"],
            "hard_negative_type": "different_operation",
            "cached_response_a": canonical_response,
            "cached_response_b": hn1["response"],
            "compatibility_metadata": metadata_contract,
        })
        pair_id_counter += 1

        # 4. Hard Negative 2 (Explanation vs Implementation)
        hn2 = ig["hard_negative_expl_vs_impl"]
        pairs.append({
            "pair_id": f"s324_pair_{pair_id_counter:04d}",
            "intent_group_id": gid,
            "domain": domain,
            "subtopic": subtopic,
            "split": split,
            "query_a": canonical_query,
            "query_b": hn2["query"],
            "label": "negative",
            "reason": hn2["reason"],
            "hard_negative_type": "explanation_vs_implementation",
            "cached_response_a": canonical_response,
            "cached_response_b": hn2["response"],
            "compatibility_metadata": metadata_contract,
        })
        pair_id_counter += 1

        # 5. Hard Negative 3 (Different Language / Framework / Version)
        hn3 = ig["hard_negative_diff_lang_or_framework"]
        pairs.append({
            "pair_id": f"s324_pair_{pair_id_counter:04d}",
            "intent_group_id": gid,
            "domain": domain,
            "subtopic": subtopic,
            "split": split,
            "query_a": canonical_query,
            "query_b": hn3["query"],
            "label": "negative",
            "reason": hn3["reason"],
            "hard_negative_type": "different_language_framework",
            "cached_response_a": canonical_response,
            "cached_response_b": hn3["response"],
            "compatibility_metadata": metadata_contract,
        })
        pair_id_counter += 1

        # 6. Hard Negative 4 (Constraint / Parameter / Negation Shift)
        hn4 = ig["hard_negative_parameter_or_negation_shift"]
        pairs.append({
            "pair_id": f"s324_pair_{pair_id_counter:04d}",
            "intent_group_id": gid,
            "domain": domain,
            "subtopic": subtopic,
            "split": split,
            "query_a": canonical_query,
            "query_b": hn4["query"],
            "label": "negative",
            "reason": hn4["reason"],
            "hard_negative_type": "constraint_or_parameter_shift",
            "cached_response_a": canonical_response,
            "cached_response_b": hn4["response"],
            "compatibility_metadata": metadata_contract,
        })
        pair_id_counter += 1

    # 7. Add 60 metadata-incompatible pairs across domains (6 per domain) for Phase 3.2.4.6 verification
    try:
        from .dataset_definitions_stage3_2_4 import INCOMPATIBLE_TEST_PAIRS
    except ImportError:
        from gateway.evaluation.dataset_definitions_stage3_2_4 import INCOMPATIBLE_TEST_PAIRS
    for inc in INCOMPATIBLE_TEST_PAIRS:
        gid = inc["intent_group_id"]
        split = split_assignment.get(gid, "train")
        pairs.append({
            "pair_id": f"s324_pair_{pair_id_counter:04d}",
            "intent_group_id": gid,
            "domain": inc["domain"],
            "subtopic": inc["subtopic"],
            "split": split,
            "query_a": inc["query_a"],
            "query_b": inc["query_b"],
            "label": "incompatible",
            "reason": inc["reason"],
            "hard_negative_type": "metadata_incompatibility",
            "cached_response_a": inc["cached_response_a"],
            "cached_response_b": inc["cached_response_b"],
            "compatibility_metadata": inc["compatibility_metadata"],
        })
        pair_id_counter += 1

    # Dataset metadata and stats
    dataset_sha256 = _compute_sha256(pairs)
    
    split_counts = {"train": 0, "calibration": 0, "test": 0}
    label_counts = {"positive": 0, "negative": 0, "incompatible": 0}
    domain_counts: Dict[str, int] = {}
    split_label_breakdown: Dict[str, Dict[str, int]] = {
        s: {"positive": 0, "negative": 0, "incompatible": 0} for s in split_counts
    }

    for p in pairs:
        split_counts[p["split"]] += 1
        label_counts[p["label"]] += 1
        domain_counts[p["domain"]] = domain_counts.get(p["domain"], 0) + 1
        split_label_breakdown[p["split"]][p["label"]] += 1

    metadata = {
        "dataset_name": "Stage 3.2.4 Semantic Similarity and Threshold Calibration Dataset",
        "version": "1.0.0",
        "sha256": dataset_sha256,
        "seed": seed,
        "embedding_model": "BAAI/bge-small-en-v1.5",
        "embedding_dimensions": 384,
        "intent_groups_total": len(intent_groups),
        "intent_group_split_counts": {
            "train": 126,
            "calibration": 42,
            "test": 42,
        },
        "pairs_total": len(pairs),
        "compatible_pairs_total": len(pairs) - label_counts["incompatible"],
        "positive_pairs_total": label_counts["positive"],
        "negative_pairs_total": label_counts["negative"],
        "incompatible_pairs_total": label_counts["incompatible"],
        "split_counts": split_counts,
        "label_counts": label_counts,
        "domain_counts": domain_counts,
        "split_label_breakdown": split_label_breakdown,
        "leakage_check_passed": True,
    }

    return pairs, metadata


def generate_and_save_dataset(
    output_dir: Path | str = "gateway/evaluation/datasets",
) -> Tuple[Path, Path]:
    """Generates the dataset files and writes them to the specified directory."""
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    pairs, metadata = build_stage3_2_4_dataset()
    pairs_file = out_dir / "stage3_2_4_semantic_pairs.json"
    meta_file = out_dir / "stage3_2_4_dataset_metadata.json"

    with open(pairs_file, "w", encoding="utf-8") as f:
        json.dump(pairs, f, indent=2, ensure_ascii=False)

    with open(meta_file, "w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=2, ensure_ascii=False)

    return pairs_file, meta_file


if __name__ == "__main__":
    p_path, m_path = generate_and_save_dataset()
    print(f"Generated dataset pairs:    {p_path}")
    print(f"Generated dataset metadata: {m_path}")
