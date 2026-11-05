#!/usr/bin/env bash
# Pull Switch's published Compose artifact and run the bundled Mattermost stack.
#
# Pins ghcr.io/sandbox-quantum/standalone-compose. 0.31.0 is the newest
# non-rc tag on that repo as of 2026-11-05. 0.32.0-rc.1 exists and is not
# the default. Override with SWITCH_VERSION=...
#
# Secrets are written to .switch/standalone/.env (mode 600). That directory
# is gitignored. The script never prints secret values.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STACK_DIR="${SWITCH_STACK_DIR:-"$ROOT/.switch/standalone"}"
VERSION="${SWITCH_VERSION:-0.31.0}"
REGISTRY="${SWITCH_REGISTRY:-ghcr.io}"
NAMESPACE="${SWITCH_IMAGE_NAMESPACE:-sandbox-quantum}"
ARTIFACT="${REGISTRY}/${NAMESPACE}/standalone-compose:${VERSION}"
COMPOSE_FILE="${STACK_DIR}/standalone-docker-compose.yml"
ENV_FILE="${STACK_DIR}/.env"
PROJECT="${COMPOSE_PROJECT_NAME:-switch-data-room}"

log() { printf '%s\n' "$*" >&2; }

need_oras() {
  if command -v oras >/dev/null 2>&1; then
    return
  fi
  if [[ -x "${HOME}/.local/bin/oras" ]]; then
    export PATH="${HOME}/.local/bin:${PATH}"
    return
  fi
  log "oras is not on PATH. Install it from https://oras.land or place the binary at ~/.local/bin/oras."
  exit 1
}

docker_cmd() {
  if docker info >/dev/null 2>&1; then
    docker "$@"
  elif sudo docker info >/dev/null 2>&1; then
    sudo docker "$@"
  else
    log "Docker is installed but the daemon is not responding."
    log "Start it with: sudo dockerd"
    exit 1
  fi
}

ensure_env() {
  if [[ -f "${ENV_FILE}" ]]; then
    # Keep an existing file, but force the pinned version so a stale
    # SWITCH_VERSION=latest cannot float the images.
    if grep -q '^SWITCH_VERSION=' "${ENV_FILE}"; then
      sed -i "s/^SWITCH_VERSION=.*/SWITCH_VERSION=${VERSION}/" "${ENV_FILE}"
    else
      printf '\nSWITCH_VERSION=%s\n' "${VERSION}" >> "${ENV_FILE}"
    fi
    return
  fi
  local hex token db_owner db_app gateway_pw mm_admin mm_user secret
  hex() { openssl rand -hex 32; }
  token="$(hex)"
  db_owner="$(hex)"
  db_app="$(hex)"
  gateway_pw="$(hex)"
  mm_admin="$(hex)"
  mm_user="$(hex)"
  secret="$(hex)"
  umask 077
  cat > "${ENV_FILE}" <<EOF
SWITCH_REGISTRY=${REGISTRY}
SWITCH_IMAGE_NAMESPACE=${NAMESPACE}
SWITCH_VERSION=${VERSION}
GATEWAY_HOST_PORT=3000
API_HOST_PORT=8000
MATTERMOST_HOST_PORT=8065
POSTGRES_HOST_PORT=5432
SWITCH_BIND_ADDR=127.0.0.1
DB_HOST=localhost
DB_PORT=5432
DB_USER=switch_app
DB_PASSWORD=${db_app}
DB_OWNER_USER=postgres
DB_OWNER_PASSWORD=${db_owner}
DB_NAME=switch
ID_SERVER_NAME=localhost
AGENT_REGISTRATION_TOKEN=${token}
SECRET_KEYS=1:${secret}
JWT_SECRET_KEY=${secret}
GATEWAY_ADMIN_EMAIL=admin@switch.local
GATEWAY_ADMIN_PASSWORD=${gateway_pw}
FRONTEND_BASE_URL=http://127.0.0.1:3000
MATTERMOST_ADMIN_USER=admin
MATTERMOST_ADMIN_PASSWORD=${mm_admin}
MATTERMOST_TEAM_NAME=switch
MATTERMOST_USER=user
MATTERMOST_USER_PASSWORD=${mm_user}
OUTBOUND_ALLOWED_PRIVATE_HOSTS=localhost
TELEMETRY_ENVIRONMENT=local
FEATURE_FLAGS_ENABLED=
EOF
  chmod 600 "${ENV_FILE}"
  log "Wrote ${ENV_FILE} (mode 600). It holds the bootstrap registration token and admin passwords."
}

repair_bridge_forward() {
  # Docker 29 on a host that still has iptables-legacy can drop packets
  # between containers on the same bridge (ARP works, TCP times out).
  # A hairpin ACCEPT is what current Docker installs itself. -C makes this
  # a no-op when the rule is already present.
  local iface cmd
  command -v ip >/dev/null 2>&1 || return 0
  for iface in $(ip -o link show | awk -F': ' '/^.*: br-/{print $2}'); do
    for cmd in iptables iptables-legacy; do
      command -v "$cmd" >/dev/null 2>&1 || continue
      if sudo -n "$cmd" -C DOCKER-FORWARD -i "$iface" -o "$iface" -j ACCEPT >/dev/null 2>&1; then
        continue
      fi
      sudo -n "$cmd" -I DOCKER-FORWARD 1 -i "$iface" -o "$iface" -j ACCEPT >/dev/null 2>&1 || true
    done
  done
}

pull() {
  need_oras
  mkdir -p "${STACK_DIR}"
  log "Pulling ${ARTIFACT}"
  (
    cd "${STACK_DIR}"
    oras pull "${ARTIFACT}"
  )
  if [[ ! -f "${COMPOSE_FILE}" ]]; then
    log "oras pull finished but ${COMPOSE_FILE} is missing."
    ls -la "${STACK_DIR}" >&2 || true
    exit 1
  fi
  printf '%s\n' "${VERSION}" > "${STACK_DIR}/VERSION"
  ensure_env
  log "Compose file: ${COMPOSE_FILE}"
}

compose() {
  if [[ ! -f "${COMPOSE_FILE}" || ! -f "${ENV_FILE}" ]]; then
    pull
  fi
  docker_cmd compose \
    --project-name "${PROJECT}" \
    --project-directory "${STACK_DIR}" \
    -f "${COMPOSE_FILE}" \
    --profile collab \
    --profile gateway \
    "$@"
}

cmd="${1:-pull}"
case "${cmd}" in
  pull) pull ;;
  up)
    pull
    # The bridge appears as compose creates the network. Keep repairing
    # while `up` waits on init-db, or that container cannot reach Postgres.
    repair_bridge_forward
    (
      for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20 21 22 23 24 25 26 27 28 29 30; do
        repair_bridge_forward
        sleep 1
      done
    ) &
    repair_pid=$!
    compose up -d
    repair_bridge_forward
    kill "$repair_pid" >/dev/null 2>&1 || true
    wait "$repair_pid" 2>/dev/null || true
    ;;
  down)
    if [[ -f "${COMPOSE_FILE}" ]]; then
      compose down
    else
      log "No compose file at ${COMPOSE_FILE}; nothing to stop."
    fi
    ;;
  ps)
    compose ps
    ;;
  *)
    log "Usage: $0 pull|up|down|ps"
    exit 2
    ;;
esac
