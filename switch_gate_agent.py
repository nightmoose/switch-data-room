"""Switch Agent Protocol client for the Data Reliability Room.

The process registers once (``POST /agents``, ``connection_model: always_on``),
opens ``/agents/{id}/connection/ws``, and calls operations discovered from
``GET /agents/{id}/ops``. Room commands are decided by ``data_room.session``;
this module only speaks the wire protocol.

Environment: see ``.env.example``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlencode

import httpx

from data_room.paths import (
    INCIDENT_LOG_DOC,
    ORDERS_DOC,
    REMEDIATION_DOC,
    workspace_root,
)
from data_room.session import Incoming, Outbound, RoomSession

CLIENT_NAME = "switch-data-room"
CLIENT_VERSION = "0.1.0"
REQUIRED_OPS = (
    "connect_to_room",
    "assume_role",
    "load_internal_documents",
    "post_message",
    "send_targeted_message",
    "update_room_document",
)
DOCUMENTED_FIELDS = {
    "list_rooms": {"include_archived"},
    "connect_to_room": {"room_id", "include_general_instructions"},
    "assume_role": {"role"},
    "release_role": set(),
    "load_internal_documents": {"ids"},
    "post_message": {"body", "thread_id"},
    "send_targeted_message": {"body", "target_names", "target_roles", "thread_id"},
    "update_room_document": {
        "document_id",
        "name",
        "description",
        "instructions",
        "content",
    },
    "create_room_document": {"name", "description", "instructions", "content"},
    "list_linked_rooms": set(),
    "update_agent_detail": {
        "agent_id",
        "name",
        "description",
        "display_name",
        "addressing",
    },
}


class SwitchError(RuntimeError):
    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self.body = body
        super().__init__(f"Switch HTTP {status}: {body[:500]}")


class TakenOver(RuntimeError):
    """Another client opened this connection id. Do not reconnect."""


@dataclass
class AgentConfig:
    bridge_url: str
    registration_key: str
    agent_api_key: str
    agent_id: str
    agent_name: str
    connection_id: str
    ingest_room: str
    incidents_room: str
    ingest_room_id: str
    incidents_room_id: str
    root: Path
    state_path: Path
    overwrite: bool = False

    @classmethod
    def from_env(cls) -> "AgentConfig":
        root = workspace_root()
        _load_dotenv(root / ".env")
        state_path = root / ".switch" / "gatekeeper.json"
        stored = _read_state(state_path)
        connection_id = os.environ.get("SWITCH_CONNECTION_ID", "").strip()
        if not connection_id:
            connection_id = str(stored.get("connection_id") or uuid.uuid4())
        return cls(
            bridge_url=os.environ.get("SWITCH_BRIDGE_URL", "http://127.0.0.1:8000").strip(),
            registration_key=os.environ.get("SWITCH_REGISTRATION_KEY", "").strip(),
            agent_api_key=os.environ.get("SWITCH_AGENT_API_KEY", "").strip()
            or str(stored.get("api_key") or ""),
            agent_id=os.environ.get("SWITCH_AGENT_ID", "").strip()
            or str(stored.get("agent_id") or ""),
            agent_name=os.environ.get("SWITCH_AGENT_NAME", "data-gatekeeper").strip(),
            connection_id=connection_id,
            ingest_room=os.environ.get("SWITCH_INGEST_ROOM", "data-ingest").strip(),
            incidents_room=os.environ.get("SWITCH_INCIDENTS_ROOM", "data-incidents").strip(),
            ingest_room_id=os.environ.get("SWITCH_INGEST_ROOM_ID", "").strip(),
            incidents_room_id=os.environ.get("SWITCH_INCIDENTS_ROOM_ID", "").strip(),
            root=root,
            state_path=state_path,
            overwrite=os.environ.get("SWITCH_AGENT_OVERWRITE", "").strip().lower()
            in {"1", "true", "yes"},
        )


class SwitchClient:
    """HTTP half of the agent protocol. The WebSocket loop lives in ``run_agent``."""

    def __init__(self, config: AgentConfig, http: httpx.Client | None = None) -> None:
        self.config = config
        self.http = http or httpx.Client(
            base_url=config.bridge_url.rstrip("/"),
            timeout=30.0,
        )
        self.ops: dict[str, Any] = {}
        self.agent_id = config.agent_id
        self.api_key = config.agent_api_key

    def prepare(self) -> None:
        self.probe()
        self.ensure_registered()
        self.discover()
        missing = [name for name in REQUIRED_OPS if name not in self.ops]
        if missing:
            raise SwitchError(404, "agent bridge is missing operations: " + ", ".join(missing))
        self.open_addressing()

    def probe(self) -> None:
        response = self.http.get("/health")
        if response.status_code == 401:
            raise SwitchError(
                401,
                "GET /health returned 401. SWITCH_BRIDGE_URL should be the agent bridge, not a protected UI path.",
            )
        if response.status_code >= 400:
            raise SwitchError(response.status_code, response.text)

    def ensure_registered(self) -> None:
        if self.agent_id and self.api_key:
            return
        if not self.config.registration_key:
            raise SwitchError(
                401,
                "Set SWITCH_REGISTRATION_KEY to register, or SWITCH_AGENT_ID and SWITCH_AGENT_API_KEY to reuse an agent.",
            )
        body: dict[str, Any] = {
            "name": self.config.agent_name,
            "description": (
                "Validates partner data drops with DataContractGate and "
                "fixer proposals with AISchemaGate."
            ),
            "connector_type": CLIENT_NAME,
            "display_name": _display_name(self.config.agent_name),
            "integration_profile": registration_profile(),
        }
        if self.config.overwrite:
            body["overwrite"] = True
        response = self.http.post(
            "/agents",
            headers={"Authorization": f"Bearer {self.config.registration_key}"},
            json=body,
        )
        if response.status_code == 409:
            raise SwitchError(
                409,
                "An agent with this name already exists. Set SWITCH_AGENT_API_KEY from the first registration. "
                "overwrite replaces the API key; leave it off unless you mean to.",
            )
        if response.status_code >= 400:
            raise SwitchError(response.status_code, response.text)
        payload = response.json()
        self.agent_id = str(payload["id"])
        self.api_key = str(payload["api_key"])
        _write_state(
            self.config.state_path,
            {
                "agent_id": self.agent_id,
                "api_key": self.api_key,
                "connection_id": self.config.connection_id,
            },
        )
        print(
            f"Registered {self.config.agent_name} id={self.agent_id}. "
            f"API key stored in {self.config.state_path} (mode 600).",
            file=sys.stderr,
        )

    def discover(self) -> dict[str, Any]:
        response = self.http.get(
            f"/agents/{self.agent_id}/ops",
            headers=self._auth(),
        )
        if response.status_code >= 400:
            raise SwitchError(response.status_code, response.text)
        payload = response.json()
        self.ops = dict(payload.get("operations") or {})
        return self.ops

    def open_addressing(self) -> None:
        if "update_agent_detail" not in self.ops and self.ops:
            print(
                "update_agent_detail is not in the live operation catalog. "
                "Set addressing to anyone in the agent settings so the room can mention @gatekeeper.",
                file=sys.stderr,
            )
            return
        try:
            self.call_op(
                "update_agent_detail",
                {"agent_id": self.agent_id, "addressing": "anyone"},
                bound=False,
            )
        except SwitchError as exc:
            if exc.status in {400, 403, 404}:
                print(
                    "Could not set addressing to anyone on this agent. "
                    "The released server may reserve that for the owner. "
                    "In Switch Console, open the agent and set who can address it to anyone.",
                    file=sys.stderr,
                )
                return
            raise

    def call_op(self, name: str, args: Mapping[str, Any], *, bound: bool = True) -> Any:
        response = self.http.post(
            f"/agents/{self.agent_id}/ops/{name}",
            headers=self._auth(bound=bound),
            json=filter_op_args(name, args, self.ops),
        )
        if response.status_code >= 400:
            raise SwitchError(response.status_code, response.text)
        if not response.content:
            return None
        payload = response.json()
        if isinstance(payload, dict) and set(payload) == {"result"}:
            return payload["result"]
        return payload

    def _auth(self, *, bound: bool = False) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self.api_key}"}
        if bound and self.config.connection_id:
            headers["X-Switch-Connection-Id"] = self.config.connection_id
        return headers


def filter_op_args(
    name: str,
    args: Mapping[str, Any],
    ops: Mapping[str, Any],
) -> dict[str, Any]:
    """Keep only arguments the live operation schema (or the documented signature) accepts."""
    schema = (ops.get(name) or {}).get("input_schema") if ops else None
    if isinstance(schema, dict) and "properties" in schema:
        allowed = set(schema.get("properties") or {})
    elif name in DOCUMENTED_FIELDS:
        allowed = DOCUMENTED_FIELDS[name]
    else:
        allowed = set(args)
    return {key: value for key, value in args.items() if key in allowed and value is not None}


def websocket_url(
    bridge_url: str,
    agent_id: str,
    connection_id: str,
    room_ids: list[str] | None = None,
    *,
    start_from: str | int | None = None,
    expected_generation: str | int | None = None,
) -> str:
    base = bridge_url.rstrip("/")
    if base.startswith("https://"):
        ws_base = "wss://" + base[len("https://") :]
    elif base.startswith("http://"):
        ws_base = "ws://" + base[len("http://") :]
    else:
        ws_base = base
    query = [
        ("connection_id", connection_id),
        ("scope", "all"),
        ("filter", "all"),
        ("client", CLIENT_NAME),
        ("client_version", CLIENT_VERSION),
    ]
    if room_ids:
        query.append(("rooms", ",".join(room_ids)))
    if start_from is not None:
        query.append(("start_from", str(start_from)))
    if expected_generation is not None:
        query.append(("expected_generation", str(expected_generation)))
    return f"{ws_base}/agents/{agent_id}/connection/ws?{urlencode(query, safe=',')}"


def pong_frame(cursor: int) -> dict[str, int | str]:
    return {"type": "pong", "cursor": cursor}


def frame_kind(frame: Mapping[str, Any]) -> str:
    event = frame.get("event")
    if event in {"message", "command", "room_join"}:
        return str(event)
    kind = frame.get("type") or event or "unknown"
    return str(kind)


def message_payload(frame: Mapping[str, Any]) -> dict[str, Any]:
    data = frame.get("data")
    if isinstance(data, dict):
        payload = data.get("payload")
        if isinstance(payload, dict):
            return payload
    payload = frame.get("payload")
    return payload if isinstance(payload, dict) else {}


def _display_name(agent_name: str) -> str:
    lowered = agent_name.lower()
    if "fixer" in lowered and "gatekeeper" not in lowered:
        return "Fixer"
    if "gatekeeper" in lowered:
        return "Gatekeeper"
    return agent_name


def registration_profile() -> dict[str, Any]:
    """The integration profile this client sends. Exposed for tests."""
    return {
        "connection_model": "always_on",
        "message_exchange": True,
        "pre_invocation_mediation": [],
        "post_invocation_mediation": [],
        "event_reporting": [],
    }


class RoomRunner:
    """Binds a RoomSession to Switch operations."""

    def __init__(self, client: SwitchClient, session: RoomSession) -> None:
        self.client = client
        self.session = session
        self.room_ids: dict[str, str] = {}
        self.context: dict[str, dict[str, Any]] = {}
        self.active_room: str | None = None
        self.cursor = 0
        self.generation: int | None = None

    def remember_state(self, frame: Mapping[str, Any]) -> None:
        payload = frame.get("data") if isinstance(frame.get("data"), dict) else frame
        assert isinstance(payload, dict)
        if "cursor" in payload and payload["cursor"] is not None:
            self.cursor = int(payload["cursor"])
        if payload.get("generation") is not None:
            self.generation = int(payload["generation"])

    def bind_rooms(self) -> None:
        config = self.client.config
        found = self._rooms_by_name()
        ingest = config.ingest_room_id or found.get(_clean_name(config.ingest_room), "")
        incidents = config.incidents_room_id or found.get(_clean_name(config.incidents_room), "")
        if not ingest or not incidents:
            raise SwitchError(
                404,
                "Could not resolve #data-ingest and #data-incidents. "
                "Import switch-rooms.template.yaml, invite this agent, then set "
                "SWITCH_INGEST_ROOM_ID and SWITCH_INCIDENTS_ROOM_ID if the names differ.",
            )
        self.room_ids = {"ingest": ingest, "incidents": incidents}
        self.occupy("ingest")
        if "list_linked_rooms" in self.client.ops:
            links = self.client.call_op("list_linked_rooms", {})
            labels = []
            if isinstance(links, list):
                labels = [str(item.get("label", "")) for item in links if isinstance(item, dict)]
            if "escalates_to" not in labels:
                print(
                    "No escalates_to link on the ingest room yet. "
                    "The template's room group creates it.",
                    file=sys.stderr,
                )
        self.occupy("incidents")
        self._load_room_contracts()
        print(
            f"Connected as @gatekeeper in {', '.join(sorted(self.room_ids))}",
            file=sys.stderr,
        )

    def occupy(self, room_key: str) -> None:
        room_id = self.room_ids[room_key]
        if self.active_room == room_id:
            return
        if self.active_room is not None and "release_role" in self.client.ops:
            try:
                self.client.call_op("release_role", {})
            except SwitchError:
                pass
        context = self.client.call_op("connect_to_room", {"room_id": room_id})
        if isinstance(context, dict):
            self.context[room_id] = context
        try:
            self.client.call_op("assume_role", {"role": "gatekeeper"})
        except SwitchError as exc:
            print(f"assume_role gatekeeper in {room_key} failed: {exc}", file=sys.stderr)
        self.active_room = room_id

    def handle_frame(self, frame: Mapping[str, Any]) -> None:
        if "id" in frame and isinstance(frame["id"], int):
            self.cursor = max(self.cursor, frame["id"])
        if frame_kind(frame) != "message":
            return
        data = frame.get("data") if isinstance(frame.get("data"), dict) else frame
        assert isinstance(data, dict)
        payload = message_payload(frame)
        sender = str(payload.get("sender_name") or "")
        body = str(payload.get("body") or "")
        if _from_us(sender, body, self.client.config.agent_name):
            return
        room_id = str(data.get("room_id") or "")
        room_key = next((key for key, value in self.room_ids.items() if value == room_id), "")
        if not room_key:
            return
        self._refresh_contracts()
        incoming = Incoming(
            room=room_key,
            body=body,
            message_id=_optional_str(payload.get("message_id")),
            thread_id=_optional_str(payload.get("thread_id")),
            sender_name=sender or None,
            addressed=bool(payload.get("addressed")),
        )
        for outbound in self.session.handle(incoming):
            self.dispatch(outbound)

    def dispatch(self, outbound: Outbound) -> None:
        self.occupy(outbound.room)
        thread = outbound.thread_id
        if outbound.op == "post_message":
            self._post(outbound.body, thread)
        elif outbound.op == "send_targeted_message":
            args: dict[str, Any] = {
                "body": outbound.body,
                "target_roles": outbound.target_roles,
            }
            if thread:
                args["thread_id"] = thread
            try:
                self.client.call_op("send_targeted_message", args)
            except SwitchError:
                if thread:
                    args.pop("thread_id", None)
                    self.client.call_op("send_targeted_message", args)
                else:
                    raise
        elif outbound.op == "update_room_document":
            self._write_document(
                outbound.document_name or INCIDENT_LOG_DOC,
                outbound.document_content or "",
            )

    def _post(self, body: str, thread: str | None) -> None:
        args: dict[str, Any] = {"body": body}
        if thread:
            args["thread_id"] = thread
        try:
            self.client.call_op("post_message", args)
        except SwitchError:
            if not thread:
                raise
            self.client.call_op("post_message", {"body": body})

    def _write_document(self, name: str, content: str) -> None:
        room_id = self.active_room or ""
        documents = (self.context.get(room_id) or {}).get("documents") or []
        document_id = _document_id(documents, name)
        if document_id and "update_room_document" in self.client.ops:
            try:
                self.client.call_op(
                    "update_room_document",
                    {"document_id": document_id, "content": content},
                )
                return
            except SwitchError as exc:
                print(f"update_room_document {name} failed ({exc.status}); trying create.", file=sys.stderr)
        if "create_room_document" not in self.client.ops:
            return
        created = self.client.call_op(
            "create_room_document",
            {
                "name": name,
                "description": "Resolution notes for data incidents.",
                "instructions": "Read this after a clean recheck.",
                "content": content,
            },
        )
        if isinstance(created, dict) and created.get("document_id"):
            documents.append({"id": created["document_id"], "name": name})

    def _refresh_contracts(self) -> None:
        orders = self._load_named(ORDERS_DOC)
        remediation = self._load_named(REMEDIATION_DOC)
        if orders:
            self.session.orders_yaml = orders
        if remediation:
            self.session.remediation_yaml = remediation

    def _load_room_contracts(self) -> None:
        self._refresh_contracts()

    def _load_named(self, name: str) -> str | None:
        if "load_internal_documents" not in self.client.ops:
            return None
        for room_id in (self.room_ids.get("incidents"), self.room_ids.get("ingest")):
            if not room_id:
                continue
            documents = (self.context.get(room_id) or {}).get("documents") or []
            document_id = _document_id(documents, name)
            if not document_id:
                continue
            # load_internal_documents reads the connected room.
            key = next(key for key, value in self.room_ids.items() if value == room_id)
            self.occupy(key)
            loaded = self.client.call_op("load_internal_documents", {"ids": [document_id]})
            content = _document_content(loaded)
            if content:
                return content
        return None

    def _rooms_by_name(self) -> dict[str, str]:
        if "list_rooms" not in self.client.ops:
            return {}
        listed = self.client.call_op("list_rooms", {})
        found: dict[str, str] = {}
        if not isinstance(listed, list):
            return found
        for room in listed:
            if not isinstance(room, dict):
                continue
            name = _clean_name(str(room.get("name") or ""))
            room_id = str(room.get("room_id") or room.get("id") or "")
            if name and room_id:
                found[name] = room_id
        return found


def _document_id(documents: Any, name: str) -> str | None:
    if not isinstance(documents, list):
        return None
    for document in documents:
        if not isinstance(document, dict):
            continue
        label = document.get("name") or document.get("filename") or document.get("title")
        if label == name:
            identifier = document.get("id") or document.get("document_id")
            if identifier:
                return str(identifier)
    return None


def _document_content(loaded: Any) -> str | None:
    if isinstance(loaded, list) and loaded:
        first = loaded[0]
        if isinstance(first, dict) and isinstance(first.get("content"), str):
            return first["content"]
    if isinstance(loaded, dict) and isinstance(loaded.get("content"), str):
        return loaded["content"]
    return None


def _from_us(sender: str, body: str, agent_name: str) -> bool:
    if sender and sender.lower() in {agent_name.lower(), "gatekeeper"}:
        return True
    stripped = body.lstrip()
    return stripped.startswith(("DataContractGate", "[AISchemaGate", "Failure facts", "Approval recorded", "Recheck is waiting", "Nothing to approve", "No DataContractGate"))


def _clean_name(name: str) -> str:
    return name.lstrip("#").strip()


def _optional_str(value: Any) -> str | None:
    if value is None or value == "":
        return None
    return str(value)


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _read_state(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _write_state(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)


async def run_agent(config: AgentConfig) -> None:
    import websockets

    client = SwitchClient(config)
    client.prepare()
    runner = RoomRunner(client, RoomSession(root=config.root))
    header_kwargs = _websocket_headers(websockets, {"Authorization": f"Bearer {client.api_key}"})
    while True:
        url = websocket_url(
            config.bridge_url,
            client.agent_id,
            config.connection_id,
            [room_id for room_id in runner.room_ids.values()],
            start_from=runner.cursor if runner.cursor else "head",
            expected_generation=runner.generation,
        )
        try:
            async with websockets.connect(url, **header_kwargs) as socket:
                async for raw in socket:
                    frame = json.loads(raw)
                    kind = frame_kind(frame)
                    if isinstance(frame.get("id"), int):
                        runner.cursor = max(runner.cursor, int(frame["id"]))
                    if kind == "ping":
                        await socket.send(json.dumps(pong_frame(runner.cursor)))
                        continue
                    if kind == "connection_state":
                        runner.remember_state(frame)
                        if not runner.room_ids:
                            runner.bind_rooms()
                        continue
                    if kind == "evicted":
                        code = str(frame.get("code") or "")
                        if code == "taken_over":
                            raise TakenOver(str(frame.get("reason") or "taken_over"))
                        break
                    if kind == "gap":
                        resumed = frame.get("resumed_at")
                        if isinstance(resumed, int):
                            runner.cursor = resumed
                        continue
                    if kind == "refused":
                        raise SwitchError(
                            int(frame.get("status") or 400),
                            str(frame.get("detail") or "refused"),
                        )
                    if kind == "message":
                        runner.handle_frame(frame)
        except TakenOver:
            print("Connection was taken over. Stop. Not reconnecting.", file=sys.stderr)
            raise SystemExit(1) from None
        except KeyboardInterrupt:
            raise
        except Exception as exc:  # noqa: BLE001 - reconnect unless the operator stops us
            print(f"Switch connection dropped ({exc}). Reconnecting.", file=sys.stderr)
            await asyncio.sleep(2)


def _websocket_headers(websockets: Any, headers: dict[str, str]) -> dict[str, Any]:
    import inspect

    parameters = inspect.signature(websockets.connect).parameters
    if "additional_headers" in parameters:
        return {"additional_headers": headers}
    return {"extra_headers": headers}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run the Switch data-room gatekeeper.")
    parser.add_argument(
        "command",
        nargs="?",
        default="run",
        choices=("run", "register"),
        help="register once, or connect and stay up (default)",
    )
    args = parser.parse_args(argv)
    config = AgentConfig.from_env()
    if args.command == "register":
        SwitchClient(config).prepare()
        return
    asyncio.run(run_agent(config))


if __name__ == "__main__":
    main()
