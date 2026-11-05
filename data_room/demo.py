"""Offline room transcript: dirty drop, BLOCK, PASS, approval, recheck."""

from __future__ import annotations

from pathlib import Path

from data_room.drop import generate_orders, write_drop
from data_room.paths import DEFAULT_DROP, workspace_root
from data_room.proposals import bad_proposal, good_proposal, proposal_message
from data_room.session import Incoming, Outbound, RoomSession

_ROOM_LABEL = {"ingest": "#data-ingest", "incidents": "#data-incidents"}


def run_demo(drop_path: Path | None = None, *, root: Path | None = None) -> str:
    root = root or workspace_root()
    path = drop_path or DEFAULT_DROP
    if drop_path is None or not path.exists():
        path = write_drop(path, generate_orders())
    session = RoomSession(root=root)
    lines = [
        "Data Reliability Room — offline transcript",
        "Switch rooms: #data-ingest escalates_to #data-incidents",
        "Roles: @gatekeeper (this process), @fixer (scripted proposals)",
        "",
    ]

    def play(room: str, speaker: str, body: str) -> None:
        lines.append(f"{_ROOM_LABEL[room]}  {speaker}")
        lines.append(body.rstrip())
        lines.append("")
        for outbound in session.handle(Incoming(room=room, body=body, message_id="m1")):
            lines.append(_render(outbound))
            lines.append("")

    relative = path.resolve().relative_to(root.resolve()).as_posix()
    play("ingest", "engineer", f"@gatekeeper check {relative}")
    assert session.last_facts is not None
    play("incidents", "@fixer", proposal_message(bad_proposal(session.last_facts)))
    play("incidents", "@fixer", proposal_message(good_proposal(session.last_facts)))
    play("incidents", "engineer", "@fixer approve patch")
    play("incidents", "@fixer", f"@gatekeeper recheck {relative}")
    return "\n".join(lines).rstrip() + "\n"


def _render(outbound: Outbound) -> str:
    label = _ROOM_LABEL.get(outbound.room, outbound.room)
    if outbound.op == "send_targeted_message":
        targets = " ".join(f"@{role}" for role in outbound.target_roles)
        return f"{label}  @gatekeeper -> {targets}\n{outbound.body.rstrip()}"
    if outbound.op == "update_room_document":
        return (
            f"{label}  @gatekeeper update_room_document {outbound.document_name}\n"
            f"{(outbound.document_content or '').rstrip()}"
        )
    return f"{label}  @gatekeeper\n{outbound.body.rstrip()}"


def main() -> None:
    print(run_demo(), end="")


if __name__ == "__main__":
    main()
