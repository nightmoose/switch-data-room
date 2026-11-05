"""Drive one live Switch room loop against a local stack.

Requires `make switch-up` (Switch 0.31.0 by default) and a reachable
agent bridge at SWITCH_BRIDGE_URL (default http://127.0.0.1:8000).

The script registers data-gatekeeper and data-fixer, applies
switch-rooms.template.yaml as far as the agent API allows, starts the
gatekeeper process, and has the fixer post the scripted BLOCK then PASS
sequence. Secrets stay in .switch/. demo_run/ receives redacted logs.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import websockets
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from data_room.drop import DEFAULT_DROP, generate_orders, write_drop  # noqa: E402
from data_room.proposals import bad_proposal, good_proposal, proposal_message  # noqa: E402
from switch_gate_agent import (  # noqa: E402
    AgentConfig,
    SwitchClient,
    SwitchError,
    _websocket_headers,
    frame_kind,
    message_payload,
    pong_frame,
    websocket_url,
)

BRIDGE = os.environ.get("SWITCH_BRIDGE_URL", "http://127.0.0.1:8000").rstrip("/")
STACK_ENV = ROOT / ".switch" / "standalone" / ".env"
DEMO_RUN = ROOT / "demo_run"
GATE_NAME = "data-gatekeeper"
FIXER_NAME = "data-fixer"


def scrub(text: str) -> str:
    text = re.sub(r"Bearer\s+\S+", "Bearer [redacted]", text)
    text = re.sub(
        r"(?i)(api_key|token|password|secret|authorization)(['\"]?\s*[:=]\s*['\"]?)\S+",
        r"\1\2[redacted]",
        text,
    )
    return text


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(scrub(text), encoding="utf-8")


def load_stack_token() -> str:
    if not STACK_ENV.is_file():
        raise SystemExit(f"Missing {STACK_ENV}. Run make switch-up first.")
    token = ""
    for line in STACK_ENV.read_text(encoding="utf-8").splitlines():
        if line.startswith("AGENT_REGISTRATION_TOKEN="):
            token = line.split("=", 1)[1].strip()
    if not token:
        raise SystemExit("AGENT_REGISTRATION_TOKEN is empty in the stack env.")
    return token


def client_for(name: str, token: str, state_name: str) -> SwitchClient:
    config = AgentConfig(
        bridge_url=BRIDGE,
        registration_key=token,
        agent_api_key="",
        agent_id="",
        agent_name=name,
        connection_id=str(uuid.uuid4()),
        ingest_room="data-ingest",
        incidents_room="data-incidents",
        ingest_room_id="",
        incidents_room_id="",
        root=ROOT,
        state_path=ROOT / ".switch" / state_name,
    )
    stored = {}
    if config.state_path.is_file():
        stored = json.loads(config.state_path.read_text(encoding="utf-8"))
        config.agent_id = str(stored.get("agent_id") or "")
        config.agent_api_key = str(stored.get("api_key") or "")
        if stored.get("connection_id"):
            config.connection_id = str(stored["connection_id"])
    http = httpx.Client(base_url=BRIDGE, timeout=60.0)
    client = SwitchClient(config, http=http)
    client.agent_id = config.agent_id
    client.api_key = config.agent_api_key
    return client


def record_call(log: list[dict[str, Any]], name: str, fn) -> Any:
    try:
        result = fn()
    except SwitchError as exc:
        log.append({"op": name, "status": exc.status, "body": scrub(exc.body)[:2000]})
        raise
    log.append({"op": name, "status": 200, "result": _shrink(result)})
    return result


def _shrink(value: Any) -> Any:
    if isinstance(value, dict):
        shrunk = {}
        for key, item in value.items():
            if key in {"instructions", "content", "yaml"} and isinstance(item, str) and len(item) > 180:
                shrunk[key] = item[:180] + "…"
            else:
                shrunk[key] = _shrink(item)
        return shrunk
    if isinstance(value, list):
        return [_shrink(item) for item in value[:30]]
    if isinstance(value, str) and len(value) > 500:
        return value[:500] + "…"
    return value


def wait_for_bridge() -> str:
    deadline = time.time() + 180
    last = ""
    while time.time() < deadline:
        try:
            response = httpx.get(f"{BRIDGE}/health", timeout=5.0)
            last = f"GET /health {response.status_code} {response.text[:300]}"
            if response.status_code == 200:
                return last
        except httpx.HTTPError as exc:
            last = f"GET /health error {exc}"
        time.sleep(3)
    raise SystemExit(f"Agent bridge did not become healthy.\n{last}")


def template_text() -> str:
    return (ROOT / "switch-rooms.template.yaml").read_text(encoding="utf-8")


def template_without_group(text: str) -> str:
    document = yaml.safe_load(text)
    resources = document.get("resources") or {}
    resources.pop("reliability", None)
    for resource in resources.values():
        if isinstance(resource, dict):
            resource.pop("group", None)
    return yaml.safe_dump(document, sort_keys=False)


def role_specs() -> list[dict[str, Any]]:
    document = yaml.safe_load(template_text())
    incidents = document["resources"]["incidents"]
    return [
        {
            "name": role["name"],
            "instructions": role.get("instructions") or "",
            "exclusive": bool(role.get("exclusive", False)),
        }
        for role in incidents["roles"]
    ]


def provision(gate: SwitchClient, log: list[dict[str, Any]]) -> dict[str, str]:
    listed = record_call(log, "list_rooms", lambda: gate.call_op("list_rooms", {}, bound=False))
    found = {}
    if isinstance(listed, list):
        for room in listed:
            if isinstance(room, dict) and room.get("name") and room.get("room_id"):
                found[str(room["name"])] = str(room["room_id"])
    if "data-ingest" in found and "data-incidents" in found:
        log.append({"op": "reuse_rooms", "rooms": found})
        return {"ingest": found["data-ingest"], "incidents": found["data-incidents"]}

    inputs = {"gatekeeper_agent": GATE_NAME, "fixer_agent": FIXER_NAME}
    for label, document in (
        ("create_room_from_yaml", template_text()),
        ("create_room_from_yaml_no_group", template_without_group(template_text())),
    ):
        if "create_room_from_yaml" not in gate.ops:
            log.append({"op": label, "skipped": "operation not in the live catalog"})
            break
        try:
            record_call(
                log,
                label,
                lambda document=document: gate.call_op(
                    "create_room_from_yaml",
                    {"yaml": document, "inputs": inputs},
                    bound=False,
                ),
            )
            listed = gate.call_op("list_rooms", {}, bound=False)
            found = {}
            if isinstance(listed, list):
                for room in listed:
                    if isinstance(room, dict) and room.get("name") and room.get("room_id"):
                        found[str(room["name"])] = str(room["room_id"])
            if "data-ingest" in found and "data-incidents" in found:
                return {"ingest": found["data-ingest"], "incidents": found["data-incidents"]}
        except SwitchError:
            continue
    return create_rooms_directly(gate, log)


def create_rooms_directly(gate: SwitchClient, log: list[dict[str, Any]]) -> dict[str, str]:
    roles = role_specs()
    common = {
        "agent_names": [GATE_NAME, FIXER_NAME],
        "user_names": ["user"],
        "channel_type": "channel_public",
        "roles": roles,
    }
    try:
        incidents = record_call(
            log,
            "create_room incidents",
            lambda: gate.call_op(
                "create_room",
                {
                    **common,
                    "name": "data-incidents",
                    "description": "Broken drops, fixer proposals, and human approval.",
                    "instructions": "Escalation room for the data reliability workflow.",
                },
                bound=False,
            ),
        )
    except SwitchError:
        incidents = record_call(
            log,
            "create_room incidents without chat users",
            lambda: gate.call_op(
                "create_room",
                {
                    **{key: value for key, value in common.items() if key != "user_names"},
                    "name": "data-incidents",
                    "description": "Broken drops, fixer proposals, and human approval.",
                    "instructions": "Escalation room for the data reliability workflow.",
                },
                bound=False,
            ),
        )
    incidents_id = str(incidents.get("id") or incidents.get("room_id"))
    ingest = record_call(
        log,
        "create_room ingest",
        lambda: gate.call_op(
            "create_room",
            {
                **{key: value for key, value in common.items() if key != "user_names"},
                "name": "data-ingest",
                "description": "Partner drops land here.",
                "instructions": "Clean drops stay here. Broken drops escalate.",
                "linked_rooms": [{"target_room_id": incidents_id, "label": "escalates_to"}],
            },
            bound=False,
        ),
    )
    return {"ingest": str(ingest.get("id") or ingest.get("room_id")), "incidents": incidents_id}


async def open_until_ready(client: SwitchClient, connection_id: str):
    headers = _websocket_headers(websockets, {"Authorization": f"Bearer {client.api_key}"})
    url = websocket_url(BRIDGE, client.agent_id, connection_id)
    socket = await websockets.connect(url, **headers)
    deadline = time.time() + 20
    while time.time() < deadline:
        raw = await asyncio.wait_for(socket.recv(), timeout=20)
        frame = json.loads(raw)
        kind = frame_kind(frame)
        if kind == "ping":
            await socket.send(json.dumps(pong_frame(0)))
            continue
        if kind == "connection_state":
            client.config.connection_id = connection_id
            return socket
        if kind == "refused":
            await socket.close()
            raise SwitchError(int(frame.get("status") or 400), str(frame.get("detail") or "refused"))
    await socket.close()
    raise SwitchError(408, "no connection_state frame")


async def seed_documents(gate: SwitchClient, rooms: dict[str, str], log: list[dict[str, Any]]) -> None:
    connection_id = str(uuid.uuid4())
    socket = await open_until_ready(gate, connection_id)
    try:
        contracts = {
            "orders_contract.yaml": (ROOT / "contracts" / "orders_contract.yaml").read_text(encoding="utf-8"),
            "remediation_output_contract.yaml": (
                ROOT / "contracts" / "remediation_output_contract.yaml"
            ).read_text(encoding="utf-8"),
        }
        incident = "# Incident log\n\nNo resolved incidents yet.\n"
        wanted = {
            rooms["ingest"]: contracts,
            rooms["incidents"]: {**contracts, "incident_log.md": incident},
        }
        for room_id, documents in wanted.items():
            context = gate.call_op("connect_to_room", {"room_id": room_id})
            existing = set()
            if isinstance(context, dict):
                for document in context.get("documents") or []:
                    if isinstance(document, dict) and document.get("name"):
                        existing.add(document["name"])
            for name, content in documents.items():
                if name in existing:
                    continue
                record_call(
                    log,
                    f"create_room_document {name}",
                    lambda name=name, content=content: gate.call_op(
                        "create_room_document",
                        {
                            "name": name,
                            "description": name,
                            "instructions": "Room copy of the repo contract.",
                            "content": content,
                        },
                    ),
                )
    finally:
        await socket.close()
        gate.config.connection_id = json.loads(
            (ROOT / ".switch" / "gatekeeper.json").read_text(encoding="utf-8")
        ).get("connection_id", gate.config.connection_id)


async def run_fixer(fixer: SwitchClient, rooms: dict[str, str], transcript: list[str]) -> None:
    drop = DEFAULT_DROP
    if not drop.is_file():
        write_drop(drop, generate_orders())
    relative = drop.relative_to(ROOT).as_posix()
    socket = await open_until_ready(fixer, fixer.config.connection_id)
    seen: list[str] = []

    def remember(body: str, sender: str) -> None:
        line = f"{sender}: {body.strip()}"
        seen.append(line)
        transcript.append(line)

    async def pump_once() -> None:
        raw = await asyncio.wait_for(socket.recv(), timeout=25)
        frame = json.loads(raw)
        kind = frame_kind(frame)
        if isinstance(frame.get("id"), int):
            cursor = int(frame["id"])
        else:
            cursor = 0
        if kind == "ping":
            await socket.send(json.dumps(pong_frame(cursor)))
            return
        if kind != "message":
            return
        payload = message_payload(frame)
        sender = str(payload.get("sender_name") or "")
        body = str(payload.get("body") or "")
        if sender.lower() in {FIXER_NAME, "fixer"}:
            return
        if body.strip():
            remember(body, sender or "room")

    async def wait_for(snippet: str) -> None:
        deadline = time.time() + 90
        while time.time() < deadline:
            if any(snippet in item for item in seen):
                return
            try:
                await pump_once()
            except asyncio.TimeoutError:
                continue
        raise TimeoutError(f"timed out waiting for {snippet!r}. saw:\n" + "\n".join(seen[-8:]))

    try:
        for room_id in (rooms["ingest"], rooms["incidents"]):
            fixer.call_op("connect_to_room", {"room_id": room_id})
        try:
            fixer.call_op("assume_role", {"role": "fixer"})
        except SwitchError as exc:
            transcript.append(f"(assume_role fixer: HTTP {exc.status})")
        fixer.call_op("connect_to_room", {"room_id": rooms["ingest"]})
        fixer.call_op(
            "send_targeted_message",
            {
                "body": f"check {relative}",
                "target_roles": ["gatekeeper"],
            },
        )
        transcript.append(f"@fixer -> @gatekeeper: check {relative}")
        await wait_for("DataContractGate FAIL")
        report_path = ROOT / "quarantine" / drop.stem / "report.json"
        facts_deadline = time.time() + 20
        while not report_path.is_file() and time.time() < facts_deadline:
            await asyncio.sleep(0.5)
        facts = json.loads(report_path.read_text(encoding="utf-8"))
        from data_room.validate import FailureFacts

        failure = FailureFacts(
            drop_path=facts["drop"],
            contract_name=facts["contract"],
            total=facts["total_row_count"],
            passed=facts["passed_row_count"],
            failed=facts["failed_row_count"],
            orders=facts.get("order_rows") or facts["passed_row_count"],
            refunds=facts.get("refund_rows") or 0,
            by_field=facts["by_field"],
            columns=facts["columns"],
            ranges={name: (pair[0], pair[1]) for name, pair in facts["ranges"].items()},
            required_fields=facts["required_fields"],
            primary_keys=facts["primary_keys"],
            known_files=[
                facts["drop"],
                "orders_contract.yaml",
                "remediation_output_contract.yaml",
                "incident_log.md",
                "data_room/normalize.py",
            ],
        )
        fixer.call_op("connect_to_room", {"room_id": rooms["incidents"]})
        fixer.call_op(
            "send_targeted_message",
            {"body": proposal_message(bad_proposal(failure)), "target_roles": ["gatekeeper"]},
        )
        transcript.append("@fixer posted the ungrounded proposal")
        await wait_for("[AISchemaGate: BLOCKED]")
        fixer.call_op(
            "send_targeted_message",
            {"body": proposal_message(good_proposal(failure)), "target_roles": ["gatekeeper"]},
        )
        transcript.append("@fixer posted the repaired proposal")
        await wait_for("[AISchemaGate: PASS]")
        fixer.call_op("post_message", {"body": "@fixer approve patch"})
        transcript.append("engineer: @fixer approve patch")
        await asyncio.sleep(1)
        fixer.call_op(
            "send_targeted_message",
            {"body": f"recheck {relative}", "target_roles": ["gatekeeper"]},
        )
        transcript.append(f"@fixer -> @gatekeeper: recheck {relative}")
        await wait_for("240 / 240")
    finally:
        await socket.close()


def mattermost_posts() -> str:
    password = ""
    user = "user"
    for line in STACK_ENV.read_text(encoding="utf-8").splitlines():
        if line.startswith("MATTERMOST_USER_PASSWORD="):
            password = line.split("=", 1)[1].strip()
        elif line.startswith("MATTERMOST_USER="):
            user = line.split("=", 1)[1].strip() or user
    try:
        response = httpx.post(
            "http://127.0.0.1:8065/api/v4/users/login",
            json={"login_id": user, "password": password},
            timeout=10.0,
        )
    except httpx.HTTPError as exc:
        return f"Mattermost login request failed: {exc}"
    if response.status_code != 200:
        return f"Mattermost login HTTP {response.status_code} (body omitted)"
    token = response.headers.get("Token", "")
    headers = {"Authorization": f"Bearer {token}"}
    team = httpx.get(
        "http://127.0.0.1:8065/api/v4/teams/name/switch",
        headers=headers,
        timeout=10.0,
    )
    if team.status_code != 200:
        return f"Mattermost team lookup HTTP {team.status_code}"
    team_id = team.json().get("id")
    channels = httpx.get(
        f"http://127.0.0.1:8065/api/v4/users/me/teams/{team_id}/channels",
        headers=headers,
        timeout=10.0,
    )
    if channels.status_code != 200:
        return f"Mattermost channel list HTTP {channels.status_code}"
    kept = []
    for channel in channels.json():
        name = str(channel.get("name") or channel.get("display_name") or "")
        if "data-ingest" not in name and "data-incidents" not in name and "ingest" not in name:
            continue
        posts = httpx.get(
            f"http://127.0.0.1:8065/api/v4/channels/{channel['id']}/posts",
            headers=headers,
            timeout=10.0,
        )
        messages = []
        if posts.status_code == 200:
            order = posts.json().get("order") or []
            post_map = posts.json().get("posts") or {}
            newest = list(reversed(order[:30]))
            for post_id in newest:
                post = post_map.get(post_id) or {}
                messages.append(str(post.get("message") or "")[:500])
        kept.append({"channel": name, "messages": messages})
    return json.dumps(kept, indent=2)


def compose_ps() -> str:
    script = ROOT / "scripts" / "pull-switch-compose.sh"
    completed = subprocess.run(
        ["bash", str(script), "ps"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    return scrub(completed.stdout + "\n" + completed.stderr)


def main() -> None:
    DEMO_RUN.mkdir(parents=True, exist_ok=True)
    health = wait_for_bridge()
    write_text(DEMO_RUN / "health.txt", health + "\n")
    write_text(DEMO_RUN / "compose-ps.txt", compose_ps())
    token = load_stack_token()
    log: list[dict[str, Any]] = []
    gate = client_for(GATE_NAME, token, "gatekeeper.json")
    fixer = client_for(FIXER_NAME, token, "fixer.json")
    record_call(log, "register gatekeeper", gate.ensure_registered)
    record_call(log, "register fixer", fixer.ensure_registered)
    gate.discover()
    fixer.discover()
    for agent, label in ((gate, "gatekeeper"), (fixer, "fixer")):
        try:
            record_call(
                log,
                f"addressing {label}",
                lambda agent=agent: agent.call_op(
                    "update_agent_detail",
                    {"agent_id": agent.agent_id, "addressing": "anyone"},
                    bound=False,
                ),
            )
        except SwitchError:
            pass
    rooms = provision(gate, log)
    asyncio.run(seed_documents(gate, rooms, log))
    write_text(DEMO_RUN / "provision.json", json.dumps({"rooms": rooms, "log": log}, indent=2) + "\n")

    env = os.environ.copy()
    env["SWITCH_BRIDGE_URL"] = BRIDGE
    env["SWITCH_AGENT_NAME"] = GATE_NAME
    env.pop("SWITCH_AGENT_API_KEY", None)
    env.pop("SWITCH_AGENT_ID", None)
    gate_log = open(DEMO_RUN / "gatekeeper.log", "w", encoding="utf-8")
    process = subprocess.Popen(
        [sys.executable, str(ROOT / "switch_gate_agent.py")],
        cwd=ROOT,
        env=env,
        stdout=gate_log,
        stderr=subprocess.STDOUT,
    )
    transcript: list[str] = []
    try:
        deadline = time.time() + 60
        while time.time() < deadline:
            gate_log.flush()
            text = (DEMO_RUN / "gatekeeper.log").read_text(encoding="utf-8")
            if "Connected as @gatekeeper" in text:
                break
            if process.poll() is not None:
                break
            time.sleep(1)
        asyncio.run(run_fixer(fixer, rooms, transcript))
        write_text(DEMO_RUN / "transcript.md", "\n\n".join(transcript) + "\n")
        write_text(DEMO_RUN / "mattermost-posts.json", mattermost_posts() + "\n")
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
        gate_log.close()
        raw = (DEMO_RUN / "gatekeeper.log").read_text(encoding="utf-8")
        write_text(DEMO_RUN / "gatekeeper.log", raw)
    print(f"Live loop transcript: {DEMO_RUN / 'transcript.md'}")


if __name__ == "__main__":
    main()
