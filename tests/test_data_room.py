"""Offline checks. None of these open a socket or call a Switch server."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import yaml
from aischemagate import Contract as OutputContract
from contractgate import Contract as DataContract
from contractgate.models import ViolationKind

from data_room.contracts_io import load_data_contract, load_output_contract
from data_room.demo import run_demo
from data_room.drop import generate_orders, write_drop
from data_room.grounding import evaluate_proposal
from data_room.paths import ORDERS_CONTRACT, REMEDIATION_CONTRACT, ROOT
from data_room.proposals import bad_proposal, good_proposal
from data_room.session import Incoming, RoomSession
from data_room.validate import read_csv_rows, validate_rows
from switch_gate_agent import (
    REQUIRED_OPS,
    AgentConfig,
    SwitchClient,
    SwitchError,
    filter_op_args,
    frame_kind,
    pong_frame,
    registration_profile,
    websocket_url,
)

ORDERS = """
order_id,sku,quantity,currency,status
ORD-000001,SKU-AAAB,2,USD,paid
""".strip()


def _facts_for(rows, tmp_path: Path):
    path = tmp_path / "drops" / "orders.csv"
    write_drop(path, rows)
    compiled = load_data_contract()
    report = validate_rows(read_csv_rows(path), compiled, drop_path="drops/orders.csv")
    return report


def test_orders_contract_accepts_a_clean_row():
    compiled = DataContract.from_yaml(ORDERS_CONTRACT.read_text(encoding="utf-8")).compile()
    result = compiled.validate(
        {
            "order_id": "ORD-000001",
            "sku": "SKU-AAAB",
            "quantity": 2,
            "currency": "USD",
            "status": "paid",
        }
    )
    assert result.passed, result.violations


def test_orders_contract_rejects_lowercase_currency_and_negative_quantity():
    compiled = load_data_contract()
    currency = compiled.validate(
        {
            "order_id": "ORD-000010",
            "sku": "SKU-AAAB",
            "quantity": 2,
            "currency": "usd",
            "status": "paid",
        }
    )
    quantity = compiled.validate(
        {
            "order_id": "ORD-000011",
            "sku": "SKU-AAAB",
            "quantity": -2,
            "currency": "USD",
            "status": "paid",
        }
    )
    missing = compiled.validate(
        {
            "sku": "SKU-AAAB",
            "quantity": 2,
            "currency": "USD",
            "status": "paid",
        }
    )
    assert [item.kind for item in currency.violations] == [ViolationKind.ENUM_VIOLATION]
    assert [item.kind for item in quantity.violations] == [ViolationKind.RANGE_VIOLATION]
    assert missing.violations[0].kind == ViolationKind.MISSING_REQUIRED_FIELD
    assert missing.violations[0].field == "order_id"


def test_dirty_drop_quarantines_63_of_240(tmp_path: Path):
    rows = generate_orders()
    report = _facts_for(rows, tmp_path)
    facts = report.facts
    assert facts.total == 240
    assert facts.passed == 177
    assert facts.failed == 63
    assert facts.by_field == {"currency": 59, "quantity": 4}
    assert facts.primary_keys == ["order_id"]
    assert facts.ranges["quantity"] == (1, 500)


def test_output_contract_blocks_a_wrong_type_and_passes_a_grounded_plan(tmp_path: Path):
    report = _facts_for(generate_orders(), tmp_path)
    compiled = OutputContract.from_yaml(REMEDIATION_CONTRACT.read_text(encoding="utf-8")).compile()
    broken = dict(good_proposal(report.facts))
    broken["failed_row_count"] = "many"
    schema = evaluate_proposal(broken, compiled, report.facts)
    assert schema.schema_ok is False
    assert schema.passed is False
    assert "Schema invalid" in schema.render(report.facts)

    bad = evaluate_proposal(bad_proposal(report.facts), compiled, report.facts)
    rendered = bad.render(report.facts)
    assert bad.schema_ok is True
    assert bad.passed is False
    assert rendered.startswith("[AISchemaGate: BLOCKED]")
    assert "1..1000" in rendered
    assert "orders_contract.yaml (1..500)" in rendered

    good = evaluate_proposal(good_proposal(report.facts), compiled, report.facts)
    assert good.passed is True
    passed = good.render(report.facts)
    assert passed.startswith("[AISchemaGate: PASS]")
    assert "63 failed rows, 2 fields: currency, quantity" in passed


def test_grounding_rules_for_columns_overrides_and_files(tmp_path: Path):
    report = _facts_for(generate_orders(), tmp_path)
    compiled = load_output_contract()
    facts = report.facts

    unknown = good_proposal(facts)
    unknown["fields"] = ["currency", "tax_rate"]
    unknown_decision = evaluate_proposal(unknown, compiled, facts)
    assert unknown_decision.passed is False
    assert any("tax_rate" in message for message in unknown_decision.messages)

    weakened = good_proposal(facts)
    weakened["proposed_changes"].append(
        {
            "target": "contract",
            "field": "order_id",
            "action": "drop_required",
            "detail": "required: false",
        }
    )
    blocked = evaluate_proposal(weakened, compiled, facts)
    assert blocked.passed is False
    assert any("primary key" in message for message in blocked.messages)

    weakened["human_override_required"] = True
    allowed = evaluate_proposal(weakened, compiled, facts)
    assert allowed.passed is True

    missing_file = good_proposal(facts)
    missing_file["summary"] = missing_file["summary"] + " See ghost_feed.csv."
    missing = evaluate_proposal(missing_file, compiled, facts)
    assert missing.passed is False
    assert any("ghost_feed.csv" in message for message in missing.messages)


def test_recheck_waits_for_pass_and_approval(tmp_path: Path):
    root = tmp_path
    path = write_drop(root / "drops" / "orders_2026-11-05.csv", generate_orders(total=10, currency_failures=2, quantity_failures=1))
    session = RoomSession(root=root)
    relative = "drops/orders_2026-11-05.csv"
    early = session.handle(Incoming(room="incidents", body=f"@gatekeeper recheck {relative}"))
    assert "waiting" in early[0].body
    session.handle(Incoming(room="ingest", body=f"@gatekeeper check {relative}"))
    assert session.last_facts is not None
    assert session.last_facts.failed == 3
    blocked = session.handle(
        Incoming(room="incidents", body=_fence(bad_proposal(session.last_facts)))
    )
    assert "[AISchemaGate: BLOCKED]" in blocked[0].body
    passed = session.handle(
        Incoming(room="incidents", body=_fence(good_proposal(session.last_facts)))
    )
    assert "[AISchemaGate: PASS]" in passed[0].body
    session.handle(Incoming(room="incidents", body="@fixer approve patch"))
    done = session.handle(Incoming(room="incidents", body=f"@gatekeeper recheck {relative}"))
    assert any("rows valid" in item.body for item in done)
    assert (root / "quarantine" / path.stem / "refunds.csv").is_file()
    assert (root / "accepted" / f"{path.stem}.csv").is_file()


def test_chat_path_cannot_escape_the_workspace(tmp_path: Path):
    session = RoomSession(root=tmp_path)
    escaped = session.handle(Incoming(room="ingest", body="@gatekeeper check /etc/passwd"))
    assert "escapes" in escaped[0].body


def test_offline_demo_shows_block_then_pass(tmp_path: Path):
    drop = write_drop(tmp_path / "drops" / "orders_2026-11-05.csv", generate_orders())
    transcript = run_demo(drop, root=tmp_path)
    assert transcript.index("[AISchemaGate: BLOCKED]") < transcript.index("[AISchemaGate: PASS]")
    assert "63 failed" in transcript
    assert "240 / 240 rows valid (236 orders, 4 routed refunds, 0 quarantined)." in transcript
    assert "escalates_to" in transcript
    assert "@fixer" in transcript


def test_room_template_links_roles_and_embeds_contracts():
    document = yaml.safe_load((ROOT / "switch-rooms.template.yaml").read_text(encoding="utf-8"))
    assert document["version"] == 2
    resources = document["resources"]
    ingest = resources["ingest"]
    incidents = resources["incidents"]
    assert ingest["name"] == "data-ingest"
    assert incidents["name"] == "data-incidents"
    assert {role["name"] for role in ingest["roles"]} == {"gatekeeper"}
    assert {role["name"] for role in incidents["roles"]} == {"gatekeeper", "fixer"}
    link = resources["reliability"]["links"][0]
    assert link == {"from": "ingest", "to": "incidents", "label": "escalates_to"}
    ingest_docs = {item["name"]: item["content"] for item in ingest["docs"]}
    incident_docs = {item["name"]: item["content"] for item in incidents["docs"]}
    orders = ORDERS_CONTRACT.read_text(encoding="utf-8").strip()
    remediation = REMEDIATION_CONTRACT.read_text(encoding="utf-8").strip()
    assert ingest_docs["orders_contract.yaml"].strip() == orders
    assert incident_docs["orders_contract.yaml"].strip() == orders
    assert incident_docs["remediation_output_contract.yaml"].strip() == remediation
    assert "incident_log.md" in incident_docs
    DataContract.from_yaml(ingest_docs["orders_contract.yaml"]).compile()
    OutputContract.from_yaml(incident_docs["remediation_output_contract.yaml"]).compile()
    fixer = next(role for role in incidents["roles"] if role["name"] == "fixer")
    assert "approve patch" in fixer["instructions"]
    assert "human_override_required" in fixer["instructions"]


def test_registration_profile_and_operation_filter(tmp_path: Path):
    profile = registration_profile()
    assert profile["connection_model"] == "always_on"
    assert profile["message_exchange"] is True
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content.decode())
        seen["connection"] = request.headers.get("x-switch-connection-id")
        if request.url.path == "/agents":
            return httpx.Response(200, json={"id": "agent-1", "api_key": "sak_test"})
        if request.url.path.endswith("/ops/post_message"):
            return httpx.Response(200, json={"result": {"event_id": "e1"}})
        if request.url.path.endswith("/ops"):
            return httpx.Response(
                200,
                json={
                    "operations": {
                        name: {"description": name, "input_schema": {"type": "object", "properties": {}}}
                        for name in REQUIRED_OPS
                    }
                },
            )
        return httpx.Response(404, json={"detail": "no"})

    config = _config(tmp_path)
    client = SwitchClient(
        config,
        http=httpx.Client(transport=httpx.MockTransport(handler), base_url="http://switch.test"),
    )
    client.ensure_registered()
    assert seen["auth"] == "Bearer reg-key"
    body = seen["body"]
    assert isinstance(body, dict)
    assert body["integration_profile"] == profile
    assert "overwrite" not in body
    assert client.agent_id == "agent-1"
    stored = json.loads((tmp_path / ".switch" / "gatekeeper.json").read_text(encoding="utf-8"))
    assert stored["api_key"] == "sak_test"
    assert oct((tmp_path / ".switch" / "gatekeeper.json").stat().st_mode & 0o777) == "0o600"

    client.ops = {
        "post_message": {
            "input_schema": {
                "type": "object",
                "properties": {"body": {"type": "string"}},
                "required": ["body"],
            }
        }
    }
    result = client.call_op("post_message", {"body": "hello", "thread_id": "t", "extra": 1})
    assert result == {"event_id": "e1"}
    assert seen["body"] == {"body": "hello"}
    assert seen["connection"] == "conn-1"
    assert seen["auth"] == "Bearer sak_test"

    filtered = filter_op_args(
        "post_message",
        {"body": "x", "thread_id": None, "nope": 1},
        client.ops,
    )
    assert filtered == {"body": "x"}


def test_duplicate_registration_is_not_overwritten(tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"detail": "exists"})

    client = SwitchClient(
        _config(tmp_path),
        http=httpx.Client(transport=httpx.MockTransport(handler), base_url="http://switch.test"),
    )
    with pytest.raises(SwitchError) as caught:
        client.ensure_registered()
    assert caught.value.status == 409


def test_websocket_url_and_frames():
    url = websocket_url(
        "https://switch.example",
        "agent-1",
        "conn-1",
        ["room-a", "room-b"],
        start_from="head",
    )
    assert url.startswith("wss://switch.example/agents/agent-1/connection/ws?")
    assert "connection_id=conn-1" in url
    assert "scope=all" in url
    assert "filter=all" in url
    assert "rooms=room-a%2Croom-b" in url or "rooms=room-a,room-b" in url
    assert pong_frame(4813) == {"type": "pong", "cursor": 4813}
    frame = {
        "event": "message",
        "id": 4,
        "data": {"type": "message", "room_id": "r", "payload": {"addressed": True, "body": "hi"}},
    }
    assert frame_kind(frame) == "message"
    assert frame_kind({"type": "ping"}) == "ping"


def _config(tmp_path: Path) -> AgentConfig:
    return AgentConfig(
        bridge_url="http://switch.test",
        registration_key="reg-key",
        agent_api_key="",
        agent_id="",
        agent_name="data-gatekeeper",
        connection_id="conn-1",
        ingest_room="data-ingest",
        incidents_room="data-incidents",
        ingest_room_id="",
        incidents_room_id="",
        root=tmp_path,
        state_path=tmp_path / ".switch" / "gatekeeper.json",
    )


def _fence(payload: dict) -> str:
    return "```json\n" + json.dumps(payload) + "\n```\n"
