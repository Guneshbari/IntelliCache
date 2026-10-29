# IntelliCache Semantic Cache Evaluation Dataset

This directory contains version-controlled synthetic evaluation cases used to measure the correctness, coverage, and safety of the IntelliCache semantic caching pipeline.

## Overview

The dataset provides labeled evaluation pairs designed to benchmark candidate retrieval, cosine similarity thresholds, ambiguity margins, and policy enforcement without invoking live LLMs.

> [!IMPORTANT]
> **Pilot Dataset Notice**: This is an initial synthetic pilot dataset for system benchmarking and design validation. All labels reflect idealized programming semantics and require careful human domain review before production threshold calibration. No personal data, real chat logs, or confidential user histories are included.

## Dataset Structure

The dataset is located at `gateway/evaluation/dataset.json`. Each entry contains:

```json
{
  "id": "pos-dev-001",
  "query": "How do I reverse a string in Python using slicing?",
  "category": "positive_paraphrase",
  "split": "development",
  "expected_answer_label": "py_str_slice_rev",
  "rationale": "Direct paraphrase asking how to reverse Python string with slice notation.",
  "candidates": [
    {
      "id": "cand-pos-001-a",
      "query_text": "What is the slice syntax to reverse a string in Python?",
      "cached_response": "To reverse a string `s` in Python using slicing, use `s[::-1]`.",
      "answer_label": "py_str_slice_rev",
      "metadata": {
        "llm_provider": "groq",
        "llm_model": "llama-3.3-70b-versatile"
      }
    }
  ]
}
```

### Fields

- `id`: Unique identifier for the evaluation case.
- `query`: The incoming single-turn technical query.
- `category`: Taxonomy classification (see below).
- `split`: Dataset partition (`"development"` or `"test"`).
- `expected_answer_label`: The label of the safely reusable answer equivalence class, or `null` if no candidate is safely reusable.
- `rationale`: Explanation of why reuse is safe or hazardous.
- `candidates`: List of one or more candidate cached interactions with simulated query text, cached response, answer equivalence class label, and generation metadata.

## Taxonomy & Category Definitions

1. **`positive_paraphrase`** (34 cases): Queries that express the exact same technical intent, constraints, language, and runtime requirements in different words. Reusing the candidate answer is expected and correct.
2. **Hard Negatives** (56 cases): Queries that appear syntactically similar or share vocabulary with candidates, but differ along critical axes such that reusing the cached answer would produce a hallucinated, incorrect, or broken response:
   - **`different_language`** (10 cases): Same concept implemented in incompatible programming languages (e.g. Python vs JavaScript array filtering, Go vs Rust slices).
   - **`version_difference`** (10 cases): Library or framework version breaking changes (e.g. Next.js App Router vs Pages Router, Pydantic v2 vs v1).
   - **`error_difference`** (10 cases): Error diagnostics that share keywords but have different root causes (e.g. `TypeError: NoneType not subscriptable` vs `IndexError: out of range`).
   - **`constraint_difference`** (10 cases): Algorithmic constraints or operational requirements that differ (e.g. in-place mutation vs allocating a new copy, $O(1)$ memory vs $O(N)$ memory, case-sensitive vs insensitive).
   - **`runtime_difference`** (8 cases): Different runtime execution contexts (e.g. Browser DOM `window.location` vs Node.js HTTP `req.url`, Linux POSIX signals vs Windows tasks).
   - **`output_difference`** (8 cases): Mismatched return types or formats (e.g. dictionary keys vs dictionary values, boolean flag vs element count, JSON string vs file write).
3. **`ambiguous_candidates`** (12 cases): Queries with multiple candidate cached responses that are equally close in vector space but correspond to distinct, conflicting solutions (e.g. sync vs async drivers, competing web frameworks). The policy must detect ambiguity and reject reuse.
4. **`unrelated_technical`** (12 cases): Completely unrelated technical domains (e.g. network routing vs string manipulation) that must score well below similarity thresholds.

## Split Methodology

- **`development` (57 cases, 50%)**: Used for threshold exploration, margin grid sweeps, and error analysis.
- **`test` (57 cases, 50%)**: Strictly held-out test split. Used to validate chosen thresholds and configurations without tuning hyper-parameters against test metrics.

## Decision Classification & Safety Metrics

- **True Positive (TP)**: Safe reuse expected (`expected_answer_label` is non-null), policy accepted candidate, and accepted `answer_label == expected_answer_label`.
- **False Positive (FP)**: Policy accepted a candidate BUT either:
  - No candidate was safely reusable (`expected_answer_label is None`), OR
  - Accepted candidate's `answer_label != expected_answer_label` (materially incorrect or incompatible reuse).
- **False Negative (FN)**: Safe reuse was available, but policy rejected all candidates (conservative miss).
- **True Negative (TN)**: No candidate was safely reusable, and policy correctly rejected all candidates.

### Core Metrics

- **Precision**: $\frac{\text{TP}}{\text{TP} + \text{FP}}$ (proportion of accepted hits that were safe).
- **Recall**: $\frac{\text{TP}}{\text{TP} + \text{FN}}$ (proportion of safe opportunities successfully captured).
- **False-Positive Rate (FPR)**: $\frac{\text{FP}}{\text{FP} + \text{TN}}$ (proportion of unsafe situations where the cache incorrectly reused an answer).
- **Rejection Rate**: $\frac{\text{TN} + \text{FN}}{N}$ (overall frequency of refusing reuse).
