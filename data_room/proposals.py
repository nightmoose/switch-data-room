"""Scripted fixer proposals so the room can show BLOCK then PASS without a model."""

from __future__ import annotations

import json
from typing import Any

from data_room.validate import FailureFacts


def bad_proposal(facts: FailureFacts) -> dict[str, Any]:
    """Schema-valid proposal that miscounts rows and widens quantity."""
    wrong = _unused_count(facts)
    low, high = facts.ranges["quantity"]
    widened = int(high) + 500
    span = f"{int(low)}..{int(high)}"
    return {
        "summary": (
            f"{wrong} rows failed currency checks. "
            f"Widen quantity to 1..{widened} so refunds pass."
        ),
        "failed_row_count": wrong,
        "total_row_count": facts.total,
        "fields": ["currency", "quantity"],
        "proposed_changes": [
            {
                "target": "contract",
                "field": "quantity",
                "action": "widen_range",
                "detail": f"change quantity range from {span} to 1..{widened}",
            }
        ],
        "human_override_required": False,
        "diff": (
            "--- a/contracts/orders_contract.yaml\n"
            "+++ b/contracts/orders_contract.yaml\n"
            "@@\n"
            f"-      max: {int(high)}\n"
            f"+      max: {widened}\n"
        ),
    }


def good_proposal(facts: FailureFacts) -> dict[str, Any]:
    """Proposal whose counts, columns, and ranges match the failure facts."""
    low, high = facts.ranges["quantity"]
    span = f"{int(low)}..{int(high)}"
    field_bits = ", ".join(f"{count} {name}" for name, count in facts.by_field.items())
    return {
        "summary": (
            f"{facts.failed} rows failed ({field_bits}) of {facts.total}. "
            "Uppercase currency codes. Route negative quantities to refunds. "
            f"Keep quantity {span}."
        ),
        "failed_row_count": facts.failed,
        "total_row_count": facts.total,
        "fields": list(facts.by_field),
        "proposed_changes": [
            {
                "target": "normalizer",
                "field": "currency",
                "action": "uppercase",
                "detail": "map lowercase ISO codes onto the contract enum",
            },
            {
                "target": "routing",
                "field": "quantity",
                "action": "route_negative_to_refunds",
                "detail": (
                    f"rows with quantity below {int(low)} go to refunds; "
                    f"orders stay {span}"
                ),
            },
        ],
        "human_override_required": False,
        "diff": (
            "--- a/data_room/normalize.py\n"
            "+++ b/data_room/normalize.py\n"
            "@@\n"
            "+updated['currency'] = CURRENCY.get(raw.lower(), raw)\n"
            "+if quantity < 1:\n"
            "+    return 'refund', updated\n"
        ),
    }


def proposal_message(proposal: dict[str, Any]) -> str:
    body = json.dumps(proposal, indent=2)
    return f"@gatekeeper remediation proposal\n\n```json\n{body}\n```\n"


def _unused_count(facts: FailureFacts) -> int:
    candidate = facts.failed + 11
    allowed = facts.allowed_numbers
    while candidate in allowed:
        candidate += 11
    return candidate
