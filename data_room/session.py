"""In-room decisions shared by the live agent and the offline demo."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from data_room.contracts_io import load_data_contract, load_output_contract
from data_room.grounding import evaluate_proposal
from data_room.messages import (
    format_facts_for_fixer,
    format_incident_log,
    format_raw_check,
    format_recheck,
)
from data_room.normalize import normalize_batch, write_split
from data_room.paths import INCIDENT_LOG_DOC, resolve_inside, workspace_root
from data_room.validate import FailureFacts, read_csv_rows, quarantine, validate_rows

_COMMAND = re.compile(r"\b(recheck|check)\s+(\S+)", re.IGNORECASE)
_APPROVE = re.compile(r"\bapprove\s+patch\b", re.IGNORECASE)
_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL | re.IGNORECASE)


@dataclass
class Incoming:
    room: str
    body: str
    message_id: str | None = None
    thread_id: str | None = None
    sender_name: str | None = None
    addressed: bool = False


@dataclass
class Outbound:
    room: str
    op: str
    body: str = ""
    target_roles: list[str] = field(default_factory=list)
    document_name: str | None = None
    document_content: str | None = None
    thread_id: str | None = None


class RoomSession:
    """Mode A validates a drop. Mode B validates a fixer proposal."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or workspace_root()
        self.approved = False
        self.gate_passed = False
        self.last_drop: Path | None = None
        self.last_facts: FailureFacts | None = None
        self.orders_yaml: str | None = None
        self.remediation_yaml: str | None = None

    def handle(self, incoming: Incoming) -> list[Outbound]:
        thread = incoming.thread_id or incoming.message_id
        proposal = _proposal_from_body(incoming.body)
        if proposal is not None:
            if proposal.get("_unreadable"):
                return [
                    _post(
                        "incidents",
                        "[AISchemaGate: BLOCKED] Remediation JSON could not be parsed. "
                        "Repair proposal and resubmit.",
                        thread,
                    )
                ]
            return self._review_proposal(proposal, thread)
        if _APPROVE.search(incoming.body):
            return self._approve(incoming.room, thread)
        command = _COMMAND.search(incoming.body)
        if command and (incoming.addressed or "gatekeeper" in incoming.body.lower()):
            kind, raw_path = command.group(1).lower(), command.group(2)
            return self._run_check(incoming.room, raw_path, recheck=kind == "recheck", thread=thread)
        return []

    def _run_check(
        self,
        room: str,
        raw_path: str,
        *,
        recheck: bool,
        thread: str | None,
    ) -> list[Outbound]:
        if recheck and not (self.gate_passed and self.approved):
            return [
                _post(
                    room,
                    "Recheck is waiting on [AISchemaGate: PASS] and a human `approve patch`.",
                    thread,
                )
            ]
        try:
            path = resolve_inside(self.root, raw_path)
        except ValueError as exc:
            return [_post(room, f"DataContractGate refused the path: {exc}", thread)]
        if not path.is_file():
            return [_post(room, f"DataContractGate could not read {raw_path}", thread)]

        compiled = load_data_contract(self.orders_yaml)
        raw_rows = read_csv_rows(path)
        relative = _display_path(self.root, path)
        if not recheck:
            report = validate_rows(raw_rows, compiled, drop_path=relative)
            quarantine_dir = self.root / "quarantine" / path.stem
            quarantine(report, quarantine_dir)
            self.last_drop = path
            self.last_facts = report.facts
            self.gate_passed = False
            self.approved = False
            return self._escalate(room, report, thread)

        orders, refunds = normalize_batch(raw_rows)
        report = validate_rows(
            orders,
            compiled,
            drop_path=relative,
            input_total=len(raw_rows),
            refunds=len(refunds),
        )
        quarantine_dir = self.root / "quarantine" / path.stem
        quarantine(report, quarantine_dir)
        write_split(
            orders,
            refunds,
            orders_path=self.root / "accepted" / f"{path.stem}.csv",
            refunds_path=quarantine_dir / "refunds.csv",
        )
        self.last_drop = path
        self.last_facts = report.facts
        notice = _post("ingest", format_recheck(report), thread)
        log = Outbound(
            room="incidents",
            op="update_room_document",
            document_name=INCIDENT_LOG_DOC,
            document_content=format_incident_log(report.facts, clean=report.facts.failed == 0),
            thread_id=thread,
        )
        incidents_note = _post("incidents", format_recheck(report), thread)
        return [notice, incidents_note, log]

    def _escalate(self, room: str, report, thread: str | None) -> list[Outbound]:
        summary = format_raw_check(report)
        if report.facts.failed == 0:
            return [_post(room, summary, thread)]
        handoff = format_facts_for_fixer(report.facts)
        posts = [_post("ingest", summary, thread)]
        posts.append(_post("incidents", summary, thread))
        posts.append(
            Outbound(
                room="incidents",
                op="send_targeted_message",
                body=handoff,
                target_roles=["fixer"],
                thread_id=thread,
            )
        )
        return posts

    def _review_proposal(self, proposal: dict[str, Any], thread: str | None) -> list[Outbound]:
        if self.last_facts is None:
            return [
                _post(
                    "incidents",
                    "No DataContractGate failure facts yet. Run `check <file>` first.",
                    thread,
                )
            ]
        compiled = load_output_contract(self.remediation_yaml)
        decision = evaluate_proposal(proposal, compiled, self.last_facts)
        self.gate_passed = decision.passed
        if not decision.passed:
            self.approved = False
        return [_post("incidents", decision.render(self.last_facts), thread)]

    def _approve(self, room: str, thread: str | None) -> list[Outbound]:
        if not self.gate_passed or self.last_drop is None:
            self.approved = False
            return [
                _post(
                    room,
                    "Nothing to approve. AISchemaGate has not passed a remediation yet.",
                    thread,
                )
            ]
        self.approved = True
        display = _display_path(self.root, self.last_drop)
        return [
            _post(
                "incidents",
                "Approval recorded. @fixer apply the normalizer, then "
                f"`@gatekeeper recheck {display}`.",
                thread,
            )
        ]


def _proposal_from_body(body: str) -> dict[str, Any] | None:
    match = _FENCE.search(body)
    raw = match.group(1) if match else None
    if raw is None and body.strip().startswith("{"):
        raw = body.strip()
    if raw is None:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {"_unreadable": True}
    if not isinstance(parsed, dict):
        return {"_unreadable": True}
    return parsed


def _post(room: str, body: str, thread: str | None) -> Outbound:
    return Outbound(room=room, op="post_message", body=body, thread_id=thread)


def _display_path(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()
