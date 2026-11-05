# Demo video shot list

Under 2 minutes 30 seconds. Record Mattermost on a local Switch 0.31.0 stack. Discord and Slack can wait. Do not put `.switch/standalone/.env` or agent API keys on screen.

The 2026-11-05 run already posted this sequence. `demo_run/mattermost-posts.json` and `demo_run/transcript.md` are the receipts. Re-record it live with the steps below.

## Before you hit record

```bash
make switch-up
make drop-dirty
```

Mattermost is `http://127.0.0.1:8065`, team `switch`, user `user`. The password is `MATTERMOST_USER_PASSWORD` in `.switch/standalone/.env`.

In a second terminal, from the repo root:

```bash
python3 switch_gate_agent.py
```

Leave it running. It should print `Connected as @gatekeeper in incidents, ingest`. If the rooms are missing, run `make live-loop` once. That creates them, plays the sequence, and exits. You can then record a second pass by typing in Mattermost while `switch_gate_agent.py` stays up.

Gateway admin (only if a human mention is refused): sign in at `http://127.0.0.1:3000` as `admin@switch.local` and set the agent's addressing policy so anyone in the room can mention it. `PUT /gateway/agents/{id}/addressing-policy` with `{"policy": null}` is the same switch. The password is `GATEWAY_ADMIN_PASSWORD` in the generated env file.

## 0:00–0:25 — The room

Show Mattermost with `#data-ingest` and `#data-incidents`.

Say, in one breath: a dirty partner CSV is triaged in the chat the team already has. Switch holds the contract, the two roles, and the link between the rooms. The Python process only decides.

Point at:

- Room documents `orders_contract.yaml` and `remediation_output_contract.yaml` (quantity stays `1..500`, currency stays `USD`/`EUR`/`GBP`).
- Roles `@gatekeeper` (this repo's agent) and `@fixer` (the coding agent, or the scripted fixer for this take).
- The `escalates_to` link from ingest to incidents.

## 0:25–1:00 — The drop fails

In `#data-ingest` type:

```text
@gatekeeper check drops/orders_2026-11-05.csv
```

Show the reply:

```text
DataContractGate FAIL
240 rows · 177 passed · 63 failed
currency 59
quantity 4
```

Open `quarantine/orders_2026-11-05/rejected.csv` for one second. Then show the handoff in `#data-incidents`: the failure facts and the mention of `@fixer`.

## 1:00–1:45 — The bad proposal, then the repair

Have `@fixer` post a fenced JSON remediation that claims 74 failed rows and widens quantity to `1..1000`. The captured wording is in `demo_run/transcript.md`.

Hold on the reply:

```text
[AISchemaGate: BLOCKED] Violation on field 'quantity': range 1..1000 conflicts with room contract orders_contract.yaml (1..500).
```

Then the repaired proposal: 63 failed rows, uppercase currency, negative quantities routed to refunds, quantity left at `1..500`.

Hold on:

```text
[AISchemaGate: PASS] Schema valid (remediation_output_contract.yaml). Grounding verified (63 failed rows, 2 fields: currency, quantity).
```

If you would rather not paste JSON by hand, run `make live-loop` with Mattermost visible. It posts this pair for you. Crop the terminal if it shows a local path. It does not print secrets.

## 1:45–2:15 — A person approves

In `#data-incidents` type:

```text
@fixer approve patch
```

Then:

```text
@gatekeeper recheck drops/orders_2026-11-05.csv
```

Show both rooms receiving:

```text
DataContractGate PASS
240 / 240 rows valid (236 orders, 4 routed refunds, 0 quarantined).
```

Mention that `incident_log.md` in the room is the note the gatekeeper updates after that pass.

## 2:15–2:30 — Someone else can run it

Switch back to the repo. Show `make test` and `make demo` in the README. One sentence: the same rooms, roles, and documents work in Mattermost today and in Slack or Discord after those bridges are connected in Switch Console. The coding agent replaces the scripted fixer by holding `@fixer`.

Stop. Do not show pricing, keys, or the generated `.env`.
