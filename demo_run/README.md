# Live loop, 2026-11-05

Switch **0.31.0** (`ghcr.io/sandbox-quantum/standalone-compose:0.31.0`) with the `collab` and `gateway` profiles. No secrets are in this directory. Passwords and API keys stayed in `.switch/`.

| File | What it is |
| --- | --- |
| `health.txt` | `GET /health` → 200 `{"status":"ok"}` |
| `compose-ps.txt` | switch-core, gateway, Mattermost, Postgres up |
| `provision.json` | Room ids, the template-import refusal, and the `create_room` fallback |
| `template-validate.json` | Gateway validate: `ok: true`, no warnings |
| `template-saved.json` | Saved template "Data reliability room" (id only, no document body) |
| `transcript.md` | WebSocket transcript: FAIL, BLOCKED, PASS, approve, `240 / 240` |
| `mattermost-posts.json` | The same lines as Mattermost channel posts |
| `gatekeeper.log` | Process log. Addressing self-update returned 403. The owner opened it afterwards. |

`#data-ingest` is room `4fdcb29e-e247-45d0-a4cf-196533de06e4`. `#data-incidents` is room `eda222f3-870d-464c-9754-09a1fc424c84`. Mattermost mirrored the check into `#data-ingest` and the BLOCK / PASS / approve / recheck into the incidents channel.
