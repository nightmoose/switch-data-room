"""Raw CSV validation with DataContractGate, plus quarantine files."""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from contractgate.contract import CompiledContract

from data_room.drop import COLUMNS


@dataclass
class FailureFacts:
    """Numbers and names a remediation proposal is allowed to cite."""

    drop_path: str
    contract_name: str
    total: int
    passed: int
    failed: int
    orders: int
    refunds: int
    by_field: dict[str, int]
    columns: list[str]
    ranges: dict[str, tuple[float | None, float | None]]
    required_fields: list[str]
    primary_keys: list[str]
    known_files: list[str]
    examples: list[dict[str, str]] = field(default_factory=list)

    @property
    def allowed_numbers(self) -> set[int]:
        numbers = {self.total, self.passed, self.failed, self.orders, self.refunds}
        numbers.update(self.by_field.values())
        for low, high in self.ranges.values():
            if low is not None:
                numbers.add(int(low))
            if high is not None:
                numbers.add(int(high))
        return numbers

    def as_dict(self) -> dict[str, Any]:
        return {
            "drop": self.drop_path,
            "contract": self.contract_name,
            "total_row_count": self.total,
            "passed_row_count": self.passed,
            "failed_row_count": self.failed,
            "order_rows": self.orders,
            "refund_rows": self.refunds,
            "by_field": self.by_field,
            "columns": self.columns,
            "ranges": {
                name: [_bound(low), _bound(high)]
                for name, (low, high) in self.ranges.items()
            },
            "required_fields": self.required_fields,
            "primary_keys": self.primary_keys,
            "examples": self.examples,
        }


@dataclass
class BatchReport:
    facts: FailureFacts
    accepted: list[dict[str, Any]]
    rejected: list[dict[str, Any]]
    quarantine_dir: Path | None = None


def read_csv_rows(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = []
        for raw in reader:
            rows.append(_coerce(raw))
        return rows


def validate_rows(
    rows: list[dict[str, Any]],
    compiled: CompiledContract,
    *,
    drop_path: str,
    input_total: int | None = None,
    refunds: int = 0,
) -> BatchReport:
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    by_field: dict[str, int] = {}
    examples: list[dict[str, str]] = []

    for index, row in enumerate(rows, start=1):
        result = compiled.validate(row)
        if result.passed:
            accepted.append(row)
            continue
        fields = sorted({violation.field.split("[", 1)[0] for violation in result.violations})
        for name in fields:
            by_field[name] = by_field.get(name, 0) + 1
        messages = "; ".join(violation.message for violation in result.violations)
        rejected.append(
            {
                "row_number": index,
                **{column: row.get(column, "") for column in COLUMNS},
                "violation_fields": ",".join(fields),
                "violation_messages": messages,
            }
        )
        if len(examples) < 5 and not any(item["fields"] == ",".join(fields) for item in examples):
            examples.append(
                {
                    "row_number": str(index),
                    "fields": ",".join(fields),
                    "message": messages,
                }
            )

    contract = compiled.contract
    ranges: dict[str, tuple[float | None, float | None]] = {}
    required: list[str] = []
    for entity in contract.entities:
        if entity.required:
            required.append(entity.name)
        if entity.min is not None or entity.max is not None:
            ranges[entity.name] = (entity.min, entity.max)
    primary = [
        entry.field
        for entry in contract.glossary
        if entry.constraints and "primary key" in entry.constraints.lower()
    ]
    facts = FailureFacts(
        drop_path=drop_path,
        contract_name=(
            contract.name
            if str(contract.name).endswith(".yaml")
            else f"{contract.name}.yaml"
        ),
        total=len(rows) if input_total is None else input_total,
        passed=len(accepted),
        failed=len(rejected),
        orders=len(accepted),
        refunds=refunds,
        by_field=by_field,
        columns=[entity.name for entity in contract.entities],
        ranges=ranges,
        required_fields=required,
        primary_keys=primary,
        known_files=[
            drop_path,
            Path(drop_path).name,
            "orders_contract.yaml",
            "remediation_output_contract.yaml",
            "incident_log.md",
            "data_room/normalize.py",
        ],
        examples=examples,
    )
    return BatchReport(facts=facts, accepted=accepted, rejected=rejected)


def quarantine(report: BatchReport, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    rejected_path = directory / "rejected.csv"
    fieldnames = ["row_number", *COLUMNS, "violation_fields", "violation_messages"]
    with rejected_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(report.rejected)
    (directory / "report.json").write_text(
        json.dumps(report.facts.as_dict(), indent=2) + "\n",
        encoding="utf-8",
    )
    report.quarantine_dir = directory
    report.facts.known_files.append(str(directory / "rejected.csv"))
    report.facts.known_files.append(str(directory / "report.json"))
    return directory


def _coerce(raw: dict[str, Any]) -> dict[str, Any]:
    row = dict(raw)
    quantity = row.get("quantity")
    if isinstance(quantity, str):
        text = quantity.strip()
        if text.startswith("-") and text[1:].isdigit() or text.isdigit():
            row["quantity"] = int(text)
    return row


def _bound(value: float | None) -> int | None:
    if value is None:
        return None
    return int(value)
