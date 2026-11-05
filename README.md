# switch-data-room

Data Reliability Room for Flip the Switch, Track 01 (best business workflow).

A partner CSV lands with schema drift. `@gatekeeper` checks it with DataContractGate, quarantines the bad rows, and escalates to `@fixer`. The coding agent's remediation JSON is checked with AISchemaGate before anyone is asked to approve it. The approval happens in the same chat.

Switch is the place the work runs. The contracts live in room documents, the two seats are room roles, and `#data-ingest` escalates to `#data-incidents` through a room link. Mattermost (bundled with a local Switch server), Slack, and Discord are surfaces on that same room. The workflow needs those Switch primitives. This process decides; Switch carries the conversation.

Agent protocol: <https://docs.switchagents.ai/switch-rooms/internals/agent-protocol.md>. Quickstart: <https://docs.switchagents.ai/switch-rooms/getting-started/index.md>.

## Rooms

| Room | Role | What it is for |
| --- | --- | --- |
| `#data-ingest` | `@gatekeeper` | A drop arrives. `check <file>` runs DataContractGate. A clean batch stays here. |
| `#data-incidents` | `@gatekeeper` and `@fixer` | Failure facts, the fixer proposal, the AISchemaGate result, human approval, and the recheck. |

`#data-ingest` links to `#data-incidents` with the label `escalates_to`. The link is in `switch-rooms.template.yaml`.

Room documents, stored in the room rather than only in this git tree:

- `orders_contract.yaml` — DataContractGate spec (quantity `1..500`, currency `USD`/`EUR`/`GBP`, `order_id` primary key)
- `remediation_output_contract.yaml` — AISchemaGate spec for the fixer JSON
- `incident_log.md` — updated after a clean recheck

`@gatekeeper` is this repo's always-on agent (`switch_gate_agent.py`). `@fixer` is a coding agent you connect in Switch Console and point at this directory. The fixer role text in the template is its brief: one fenced JSON object, a diff, no weakening of a required field or the primary key unless `human_override_required` is true, and no apply until a person sends `@fixer approve patch`.

## Run the offline demo

These commands do not need a Switch server. CI runs the same tests.

```bash
python3 -m pip install -r requirements.txt
make test
make drop-dirty
make demo
```

`make setup` is the install step. `make drop-dirty` writes `drops/orders_2026-11-05.csv`: 240 rows, 177 valid, 63 invalid (59 lowercase `usd`, 4 negative quantities). `make demo` plays the room on stdout:

1. `@gatekeeper check` fails the raw file and writes `quarantine/orders_2026-11-05/rejected.csv` plus `report.json`.
2. A scripted `@fixer` posts a schema-valid proposal that miscounts the failures and widens quantity to `1..1000`. AISchemaGate answers `BLOCKED`.
3. The repaired proposal keeps `1..500`, uppercases currency, and routes negative quantities to refunds. AISchemaGate answers `PASS`.
4. A person sends `@fixer approve patch`.
5. `@gatekeeper recheck` runs the normalizer and reports `240 / 240 rows valid (236 orders, 4 routed refunds, 0 quarantined)`.

`check` validates the file as it landed. `recheck` runs only after AISchemaGate has passed and the approval was recorded in this process. Paths named in chat have to stay inside the repo root.

Row checks use `contractgate` 0.4.1 (`Contract.from_yaml(...).compile().validate`). Remediation JSON uses `aischemagate` 0.5.0, the same local validator under the AISchemaGate name. Three grounding rules sit on top of that schema check: cited counts must appear in the DataContractGate facts, cited columns and files must exist, and a required field or primary key stays in force unless `human_override_required` is true. The offline path uses those local validators.

## Run it in Switch

Copy `.env.example` to `.env`. The agent reads `.env` without overriding variables you already exported. Do not commit `.env` or `.switch/`.

| Variable | Purpose |
| --- | --- |
| `SWITCH_BRIDGE_URL` | Agent bridge, usually `http://127.0.0.1:8000`. The Gateway UI is a different port. |
| `SWITCH_REGISTRATION_KEY` | Registration key, used once for `POST /agents`. A Gateway API key of type registration, or the deployment bootstrap token. |
| `SWITCH_AGENT_ID`, `SWITCH_AGENT_API_KEY` | Set after registration. The API key is returned once. |
| `SWITCH_AGENT_NAME` | Default `data-gatekeeper`. |
| `SWITCH_INGEST_ROOM`, `SWITCH_INCIDENTS_ROOM` | Room names. Ids are optional overrides. |

```bash
python3 switch_gate_agent.py register
python3 switch_gate_agent.py
```

Registration sends `connection_model: always_on` and `message_exchange: true`, then `GET /agents/{id}/ops` and calls the live operation names. The socket is `/agents/{id}/connection/ws` with a stable `connection_id`. The process answers server pings with `{"type":"pong","cursor":...}`, claims both rooms with `connect_to_room`, and assumes `@gatekeeper`. It loads `orders_contract.yaml` and `remediation_output_contract.yaml` with `load_internal_documents` when those room documents exist, and falls back to `contracts/` otherwise. A clean recheck calls `update_room_document` on `incident_log.md`, and `create_room_document` when the template copy belongs to someone else.

On startup it tries `update_agent_detail` with `addressing: anyone`, which is what the agent protocol docs describe. If the server refuses a self-update, set "who can address this agent" to anyone in Switch Console. Until that is set, a message from someone other than the owner gets a refusal instead of a check.

`make test` never opens this socket.

### Switch Console and bundled Mattermost

The fastest local server is Switch Console, which starts Switch and Mattermost for you. Install it from the [Console install page](https://docs.switchagents.ai/switch-rooms/getting-started/install-switch-console.md). Linux x86_64 and arm64 builds are published (AppImage, deb, rpm) and marked early access. On Wayland, especially inside a VM, the window can fail to draw; the [troubleshooting page](https://docs.switchagents.ai/switch-rooms/resources/troubleshooting.md) says to start it with `switch-console --ozone-platform=x11 --disable-gpu`.

In Console, run a server on this computer and start it (Docker has to be running). When the local server is up, create a registration key, fill in `.env`, and register the gatekeeper. Import `switch-rooms.template.yaml` from the Templates page. Choose the registered gatekeeper and the coding agent that should hold `@fixer`.

Mattermost comes up with that server. With the official Compose profiles it is on port `8065`. Open `#data-ingest` and send:

```text
@gatekeeper check drops/orders_2026-11-05.csv
```

### Docker Compose from the published stack

Switch publishes the Compose file as an OCI artifact, `ghcr.io/sandbox-quantum/standalone-compose:<version>`, and the images `ghcr.io/sandbox-quantum/switch-core`, `gateway`, and `setup` at that same version. Set `SWITCH_VERSION` to that tag; the Compose file requires it. Console release numbers and server release numbers are different lists. Pull the Compose file from the registry when you deploy.

The current steps live in the Switch repository at `deploy/local/standalone-docker-compose.yml` and on the [self-host page](https://docs.switchagents.ai/switch-rooms/deploy/self-host.md).

```bash
oras repo tags ghcr.io/sandbox-quantum/standalone-compose
oras pull ghcr.io/sandbox-quantum/standalone-compose:<version>
```

`oras pull` writes `standalone-docker-compose.yml`. Use Switch's own `.env.example` beside it and set `SWITCH_VERSION`, `AGENT_REGISTRATION_TOKEN`, `SECRET_KEYS` (`1:` plus a secret of at least 32 characters), `GATEWAY_ADMIN_EMAIL`, `GATEWAY_ADMIN_PASSWORD`, `DB_OWNER_PASSWORD`, `DB_PASSWORD`, `MATTERMOST_ADMIN_PASSWORD`, and `MATTERMOST_USER_PASSWORD`.

```bash
docker compose -f standalone-docker-compose.yml --profile collab --profile gateway up -d
```

The default profile is Postgres and switch-core. `collab` adds Mattermost. `gateway` adds the admin UI on port `3000`. The agent API is port `8000`, bound to `127.0.0.1` unless `SWITCH_BIND_ADDR` is changed. Point `SWITCH_BRIDGE_URL` at `http://127.0.0.1:8000` and `SWITCH_REGISTRATION_KEY` at `AGENT_REGISTRATION_TOKEN`.

Bring the stack up with the command above, register the agent, and import the template on the machine where you demo. That live pass is still ahead of this scaffold.

### Connect the coding agent

In Switch Console, connect the coding agent you already run, set its directory to this repo, and select it as `fixer_agent` when you run the template. Console exposes the room operations (`read_context`, `load_internal_documents`, `post_message`, `send_targeted_message`, `update_room_document`) to that session. The `@fixer` instructions tell it how to answer. `make demo` is the stand-in that shows the BLOCK then PASS loop before that session exists.

Discord and Slack use the same rooms after you connect those bridges in Console. Connect the apps there when you want those surfaces.

## Layout

```text
switch_gate_agent.py              Switch agent protocol client and process entry
switch-rooms.template.yaml        #data-ingest, #data-incidents, roles, documents, escalates_to
contracts/                        orders and remediation contracts
data_room/                        validation, grounding, normalizer, offline demo
drops/                            generated CSV (gitignored)
quarantine/<stem>/                rejected.csv, report.json, refunds.csv on recheck
accepted/                         order rows after a passing recheck (gitignored)
```

## License

MIT. See `LICENSE`. `contractgate` and `aischemagate` are installed from PyPI and are not vendored.
