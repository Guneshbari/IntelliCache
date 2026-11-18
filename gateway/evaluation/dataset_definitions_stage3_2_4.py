"""
Aggregated definitions for Stage 3.2.4 Semantic Similarity and Threshold Calibration Dataset.
Combines 210 canonical technical intent groups across 10 domains (21 per domain)
plus 60 deterministic metadata-incompatible pairs (6 per domain).
"""

from typing import Any, Dict, List

try:
    from .dataset_domains.pl import PL_INTENT_GROUPS
    from .dataset_domains.fl import FL_INTENT_GROUPS
    from .dataset_domains.db import DB_INTENT_GROUPS
    from .dataset_domains.ds import DS_INTENT_GROUPS
    from .dataset_domains.net import NET_INTENT_GROUPS
    from .dataset_domains.api import API_INTENT_GROUPS
    from .dataset_domains.dbg import DEBUGGING_INTENT_GROUPS
    from .dataset_domains.sys import SYS_CONFIG_INTENT_GROUPS
    from .dataset_domains.data import DATA_PROCESSING_INTENT_GROUPS
    from .dataset_domains.infra import INFRA_INTENT_GROUPS
    from .dataset_domains.incompatible import INCOMPATIBLE_TEST_PAIRS
except ImportError:
    from gateway.evaluation.dataset_domains.pl import PL_INTENT_GROUPS
    from gateway.evaluation.dataset_domains.fl import FL_INTENT_GROUPS
    from gateway.evaluation.dataset_domains.db import DB_INTENT_GROUPS
    from gateway.evaluation.dataset_domains.ds import DS_INTENT_GROUPS
    from gateway.evaluation.dataset_domains.net import NET_INTENT_GROUPS
    from gateway.evaluation.dataset_domains.api import API_INTENT_GROUPS
    from gateway.evaluation.dataset_domains.dbg import DEBUGGING_INTENT_GROUPS
    from gateway.evaluation.dataset_domains.sys import SYS_CONFIG_INTENT_GROUPS
    from gateway.evaluation.dataset_domains.data import DATA_PROCESSING_INTENT_GROUPS
    from gateway.evaluation.dataset_domains.infra import INFRA_INTENT_GROUPS
    from gateway.evaluation.dataset_domains.incompatible import INCOMPATIBLE_TEST_PAIRS

ALL_DOMAINS_DEFINITIONS: List[Dict[str, Any]] = (
    PL_INTENT_GROUPS
    + FL_INTENT_GROUPS
    + DB_INTENT_GROUPS
    + DS_INTENT_GROUPS
    + NET_INTENT_GROUPS
    + API_INTENT_GROUPS
    + DEBUGGING_INTENT_GROUPS
    + SYS_CONFIG_INTENT_GROUPS
    + DATA_PROCESSING_INTENT_GROUPS
    + INFRA_INTENT_GROUPS
)

__all__ = [
    "ALL_DOMAINS_DEFINITIONS",
    "INCOMPATIBLE_TEST_PAIRS",
]
