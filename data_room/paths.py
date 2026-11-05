"""Repository paths shared by the demo, the agent, and tests."""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ORDERS_CONTRACT = ROOT / "contracts" / "orders_contract.yaml"
REMEDIATION_CONTRACT = ROOT / "contracts" / "remediation_output_contract.yaml"
ORDERS_DOC = "orders_contract.yaml"
REMEDIATION_DOC = "remediation_output_contract.yaml"
INCIDENT_LOG_DOC = "incident_log.md"
DEFAULT_DROP = ROOT / "drops" / "orders_2026-11-05.csv"


def workspace_root() -> Path:
    raw = os.environ.get("DATA_ROOM_ROOT", "").strip()
    if not raw:
        return ROOT
    return Path(raw).expanduser().resolve()


def resolve_inside(root: Path, raw: str) -> Path:
    """Resolve a chat-supplied path and refuse anything outside ``root``."""
    text = raw.strip().strip("`").rstrip(".,);")
    if not text:
        raise ValueError("missing path")
    candidate = Path(text)
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()
    root_resolved = root.resolve()
    if resolved != root_resolved and root_resolved not in resolved.parents:
        raise ValueError(f"path escapes the data room root: {text}")
    return resolved
