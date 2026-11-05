"""AISchemaGate schema check plus deterministic grounding rules."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from contractgate.contract import CompiledContract

from data_room.validate import FailureFacts

_BARE_NUMBER = re.compile(r"(?<![\w./-])-?\d+(?![\w./-])")
_RANGE = re.compile(r"(-?\d+)\.\.(-?\d+)")
_FILE = re.compile(r"[\w./-]+\.(?:yaml|yml|csv|md|py|json)\b")
_WEAKEN = re.compile(
    r"required\s*[:=]\s*false|drop\s+required|remove\s+(?:the\s+)?primary\s+key|nullable",
    re.IGNORECASE,
)


@dataclass
class GateDecision:
    passed: bool
    schema_ok: bool
    messages: list[str] = field(default_factory=list)

    def render(self, facts: FailureFacts) -> str:
        if self.passed:
            fields = ", ".join(facts.by_field) or "none"
            return (
                "[AISchemaGate: PASS] Schema valid (remediation_output_contract.yaml). "
                f"Grounding verified ({facts.failed} failed rows, "
                f"{len(facts.by_field)} fields: {fields}). "
                'Ready for human sign-off: reply "@fixer approve patch" to apply.'
            )
        return (
            "[AISchemaGate: BLOCKED] "
            + " ".join(self.messages)
            + " Repair proposal and resubmit."
        )


def evaluate_proposal(
    proposal: dict[str, Any],
    compiled: CompiledContract,
    facts: FailureFacts,
) -> GateDecision:
    """Validate fixer JSON with AISchemaGate, then apply the grounding rules."""
    schema = compiled.validate(proposal)
    if not schema.passed:
        messages = [
            "Schema invalid (remediation_output_contract.yaml): "
            + "; ".join(item.message for item in schema.violations)
        ]
        return GateDecision(passed=False, schema_ok=False, messages=messages)

    messages = _ground(proposal, facts)
    return GateDecision(passed=not messages, schema_ok=True, messages=messages)


def _ground(proposal: dict[str, Any], facts: FailureFacts) -> list[str]:
    messages: list[str] = []
    if proposal.get("failed_row_count") != facts.failed:
        messages.append(
            "Violation on field 'failed_row_count': "
            f"{proposal.get('failed_row_count')} is not the DataContractGate failed count {facts.failed}."
        )
    if proposal.get("total_row_count") != facts.total:
        messages.append(
            "Violation on field 'total_row_count': "
            f"{proposal.get('total_row_count')} is not the DataContractGate row count {facts.total}."
        )

    allowed = facts.allowed_numbers
    for number in _cited_numbers(proposal):
        if number not in allowed:
            messages.append(
                "Violation on field 'summary': "
                f"count {number} does not appear in the DataContractGate failure facts."
            )

    columns = set(facts.columns)
    for name in proposal.get("fields") or []:
        if name not in columns:
            messages.append(
                "Violation on field 'fields': "
                f"column {name!r} is not in the contract or the failing dataset."
            )

    for token in _FILE.findall(_blob(proposal)):
        if not _known_file(token, facts):
            messages.append(
                "Violation on field 'summary': "
                f"file {token!r} is not in the drop, the contract, or the room documents."
            )

    override = bool(proposal.get("human_override_required"))
    for change in proposal.get("proposed_changes") or []:
        if not isinstance(change, dict):
            continue
        field_name = str(change.get("field", ""))
        detail = f"{change.get('action', '')} {change.get('detail', '')} {proposal.get('diff', '')}"
        if change.get("target") == "contract" and field_name in facts.ranges:
            low, high = facts.ranges[field_name]
            for proposed_low, proposed_high in _RANGE.findall(detail):
                proposed_low_i = int(proposed_low)
                proposed_high_i = int(proposed_high)
                wider = (low is not None and proposed_low_i < low) or (
                    high is not None and proposed_high_i > high
                )
                same = (low is None or proposed_low_i == int(low)) and (
                    high is None or proposed_high_i == int(high)
                )
                if wider and not same and not override:
                    contract_span = f"{_num(low)}..{_num(high)}"
                    messages.append(
                        f"Violation on field '{field_name}': range "
                        f"{proposed_low_i}..{proposed_high_i} conflicts with room contract "
                        f"{facts.contract_name} ({contract_span})."
                    )
        protected = field_name in facts.primary_keys or field_name in facts.required_fields
        if protected and _WEAKEN.search(detail) and not override:
            messages.append(
                f"Violation on field '{field_name}': weakening a required field or "
                "primary key needs human_override_required true."
            )
    return _prioritize(_unique(messages))


def _cited_numbers(proposal: dict[str, Any]) -> list[int]:
    text = " ".join(
        [
            str(proposal.get("summary", "")),
            " ".join(
                str(change.get("detail", ""))
                for change in proposal.get("proposed_changes") or []
                if isinstance(change, dict)
            ),
        ]
    )
    # Range endpoints are judged against the contract, not as free counts.
    without_ranges = _RANGE.sub(" ", text)
    return [int(match) for match in _BARE_NUMBER.findall(without_ranges)]


def _blob(proposal: dict[str, Any]) -> str:
    return json.dumps(proposal, sort_keys=True)

def _known_file(token: str, facts: FailureFacts) -> bool:
    names = set(facts.known_files)
    names.update(PathName(item) for item in facts.known_files)
    return token in names or token.split("/")[-1] in names


def PathName(value: str) -> str:
    return value.replace("\\", "/").split("/")[-1]


def _num(value: float | None) -> str:
    if value is None:
        return "?"
    return str(int(value))


def _unique(messages: list[str]) -> list[str]:
    seen: list[str] = []
    for message in messages:
        if message not in seen:
            seen.append(message)
    return seen


def _prioritize(messages: list[str]) -> list[str]:
    """Lead with contract-weakening findings so the room sees the unsafe edit first."""
    lead = [
        message
        for message in messages
        if "conflicts with room contract" in message or "weakening a required field" in message
    ]
    rest = [message for message in messages if message not in lead]
    return lead + rest
