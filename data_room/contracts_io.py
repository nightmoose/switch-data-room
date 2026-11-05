"""Load DataContractGate and AISchemaGate contracts from YAML."""

from __future__ import annotations

from pathlib import Path

from aischemagate import Contract as OutputContract
from contractgate import Contract as DataContract
from contractgate.contract import CompiledContract

from data_room.paths import ORDERS_CONTRACT, REMEDIATION_CONTRACT


def load_data_contract(source: str | Path | None = None) -> CompiledContract:
    """Compile an orders contract with DataContractGate's local validator."""
    text = _read(source, ORDERS_CONTRACT)
    return DataContract.from_yaml(text).compile()


def load_output_contract(source: str | Path | None = None) -> CompiledContract:
    """Compile a remediation contract with AISchemaGate's local validator."""
    text = _read(source, REMEDIATION_CONTRACT)
    return OutputContract.from_yaml(text).compile()


def _read(source: str | Path | None, fallback: Path) -> str:
    if source is None:
        return fallback.read_text(encoding="utf-8")
    if isinstance(source, Path):
        return source.read_text(encoding="utf-8")
    return source
