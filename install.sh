#!/usr/bin/env bash
#
# Softnix PrivateClaw — one-line installer (host-native app, Docker-based tools).
#
#   The app (API + web UI) runs natively on this host as a service.
#   Postgres and the agent's tool-sandbox run as Docker containers.
#
# Supported hosts: macOS, Ubuntu/Debian, Rocky/RHEL/Fedora (x86_64 & arm64).
#
# Usage (from inside the cloned repo):
#     ./install.sh                 # interactive install / upgrade (idempotent)
#     ./install.sh --yes           # non-interactive (reads config from env)
#     ./install.sh --no-service    # set up but don't install a system service
#     ./install.sh --web-only      # just rebuild the web frontend (used by `claw update`)
#
# Config via env (all optional): CLAW_LLM__API_KEY, CLAW_LLM__MODEL, CLAW_PORT,
#     CLAW_PG_PORT, CLAW_DATA_DIR.
#
set -euo pipefail

# ─────────────────────────── pretty output ───────────────────────────
if [[ -t 1 ]]; then
  B=$'\033[1m'; DIM=$'\033[2m'; R=$'\033[0m'
  GRN=$'\033[32m'; YEL=$'\033[33m'; RED=$'\033[31m'; CYN=$'\033[36m'
else
  B=""; DIM=""; R=""; GRN=""; YEL=""; RED=""; CYN=""
fi
step()  { printf "\n${B}${CYN}==>${R} ${B}%s${R}\n" "$*"; }
ok()    { printf "  ${GRN}✓${R} %s\n" "$*"; }
info()  { printf "  ${DIM}%s${R}\n" "$*"; }
warn()  { printf "  ${YEL}!${R} %s\n" "$*"; }
die()   { printf "\n${RED}✗ %s${R}\n" "$*" >&2; exit 1; }
have()  { command -v "$1" >/dev/null 2>&1; }

# ─────────────────────────── flags & config ──────────────────────────
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ASSUME_YES=0
INSTALL_SERVICE=1
WEB_ONLY=0
APP_PORT="${CLAW_PORT:-8700}"
PG_PORT="${CLAW_PG_PORT:-5442}"
DATA_DIR="${CLAW_DATA_DIR:-$PROJECT_DIR/data}"
PG_CONTAINER="claw-postgres"
PG_VOLUME="claw-pgdata"
PG_PASSWORD="${POSTGRES_PASSWORD:-claw}"
SANDBOX_IMAGE="claw-sandbox:latest"

while [[ $# -gt 0 ]]; do
  case "$1" in
    -y|--yes) ASSUME_YES=1 ;;
    --no-service) INSTALL_SERVICE=0 ;;
    --web-only) WEB_ONLY=1 ;;
    --port) APP_PORT="$2"; shift ;;
    --pg-port) PG_PORT="$2"; shift ;;
    --data-dir) DATA_DIR="$2"; shift ;;
    -h|--help) awk 'NR==1&&/^#!/{next} /^#/{sub(/^# ?/,"");print;next} {exit}' "$0"; exit 0 ;;
    *) die "unknown option: $1 (try --help)" ;;
  esac
  shift
done

cd "$PROJECT_DIR"
[[ -f pyproject.toml && -d claw ]] || die "run this from the PrivateClaw repo root (pyproject.toml + claw/ not found)"

# ─────────────────────────── OS detection ────────────────────────────
OS=""; DISTRO=""; ARCH="$(uname -m)"
case "$(uname -s)" in
  Darwin) OS="macos" ;;
  Linux)
    OS="linux"
    if [[ -r /etc/os-release ]]; then . /etc/os-release; DISTRO="${ID:-}"; fi ;;
  *) die "unsupported OS: $(uname -s) (macOS and Linux only)" ;;
esac

# ─────────────────────────── helpers ─────────────────────────────────
confirm() {  # confirm "question"  -> 0 yes / 1 no; auto-yes with --yes
  [[ $ASSUME_YES -eq 1 ]] && return 0
  [[ -t 0 ]] || return 0
  local ans; read -r -p "  ${1} [Y/n] " ans; [[ -z "$ans" || "$ans" =~ ^[Yy] ]]
}

gen_secret() {
  if have openssl; then openssl rand -hex 32
  elif have python3; then python3 -c 'import secrets;print(secrets.token_hex(32))'
  else head -c32 /dev/urandom | od -An -tx1 | tr -d ' \n'; fi
}

port_busy() {  # is TCP port $1 already listening?
  if have lsof; then lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1
  elif have ss;  then ss -ltn 2>/dev/null | grep -q ":$1 "
  else return 1; fi
}

# ─────────────────────────── web build (shared) ──────────────────────
build_web() {
  step "Building web frontend"
  if have node && have npm; then
    info "using host Node ($(node -v))"
    ( cd web && { [[ -f package-lock.json ]] && npm ci || npm install; } && npm run build )
  elif have docker; then
    info "no host Node — building in a throwaway Docker container (node:22-alpine)"
    docker build --target web -t claw-web-builder "$PROJECT_DIR"
    local cid; cid="$(docker create claw-web-builder)"
    rm -rf "$PROJECT_DIR/web/dist"
    docker cp "$cid:/web/dist" "$PROJECT_DIR/web/dist"
    docker rm -f "$cid" >/dev/null
  else
    die "need either Node.js or Docker to build the web frontend"
  fi
  [[ -f "$PROJECT_DIR/web/dist/index.html" ]] || die "web build produced no dist/index.html"
  ok "web/dist ready"
}

# `claw update --web-only` shortcut: rebuild and exit.
if [[ $WEB_ONLY -eq 1 ]]; then build_web; exit 0; fi

# ═══════════════════════════ preflight ═══════════════════════════════
printf "\n${B}Softnix PrivateClaw installer${R}  ${DIM}(%s / %s)${R}\n" "$OS${DISTRO:+/$DISTRO}" "$ARCH"

step "Checking prerequisites"
have curl || die "curl is required. Install it and re-run."
ok "curl"

# ── Docker (required: Postgres + sandbox + optional web build) ────────
install_docker_linux() {
  warn "Docker not found."
  if ! confirm "Install Docker Engine now (get.docker.com, needs sudo)?"; then
    die "Docker is required. Install it and re-run: https://docs.docker.com/engine/install/"
  fi
  curl -fsSL https://get.docker.com | sh
  sudo systemctl enable --now docker 2>/dev/null || true
  if [[ $EUID -ne 0 ]]; then
    sudo usermod -aG docker "$USER" 2>/dev/null || true
    warn "Added $USER to the 'docker' group — log out/in (or run 'newgrp docker') for it to take effect."
  fi
}
if ! have docker; then
  if [[ "$OS" == "linux" ]]; then
    install_docker_linux
  else
    if have brew; then
      warn "Docker not found. Installing Docker Desktop via Homebrew…"
      brew install --cask docker || true
    fi
    die "Install Docker Desktop and launch it, then re-run:  https://www.docker.com/products/docker-desktop/"
  fi
fi
if ! docker info >/dev/null 2>&1; then
  [[ "$OS" == "macos" ]] && die "Docker is installed but the daemon isn't running. Start Docker Desktop and re-run."
  die "Docker is installed but not accessible. Try 'newgrp docker' (or re-login) and re-run, or start it: sudo systemctl start docker"
fi
ok "docker ($(docker version --format '{{.Server.Version}}' 2>/dev/null || echo running))"

# ── uv (installs Python 3.12 + app deps; no system Python needed) ─────
if ! have uv; then
  step "Installing uv (Python package manager)"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  # uv installs to ~/.local/bin (or ~/.cargo/bin on older installers)
  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
  have uv || die "uv installed but not on PATH. Open a new shell and re-run, or add ~/.local/bin to PATH."
fi
ok "uv ($(uv --version 2>/dev/null | awk '{print $2}'))"

# ═══════════════════════════ install steps ═══════════════════════════
# 1) Python runtime + app dependencies (host .venv)
step "Installing Python 3.12 + app dependencies (uv sync)"
uv python install 3.12 >/dev/null 2>&1 || true
uv sync --frozen --no-dev
ok "app dependencies installed into .venv"

# 2) Web frontend
build_web

# 3) Sandbox image (the agent's tools run inside this)
step "Building the tool-sandbox image ($SANDBOX_IMAGE)"
if docker image inspect "$SANDBOX_IMAGE" >/dev/null 2>&1 && [[ $ASSUME_YES -eq 0 ]] && ! confirm "Sandbox image exists — rebuild it?"; then
  ok "reusing existing $SANDBOX_IMAGE"
else
  docker build -f docker/sandbox.Dockerfile -t "$SANDBOX_IMAGE" "$PROJECT_DIR"
  ok "$SANDBOX_IMAGE built"
fi
docker run --rm "$SANDBOX_IMAGE" python -c "import reportlab, docx, openpyxl, pptx, pandas" \
  && ok "sandbox document stack verified" || warn "sandbox libraries failed to import — check docker/sandbox.Dockerfile"

# 4) Postgres container
step "Setting up Postgres (Docker container '$PG_CONTAINER')"
mkdir -p "$DATA_DIR/workspaces" "$DATA_DIR/knowledge"
if docker ps -a --format '{{.Names}}' | grep -qx "$PG_CONTAINER"; then
  docker start "$PG_CONTAINER" >/dev/null 2>&1 || true
  ok "reusing existing container '$PG_CONTAINER'"
else
  if port_busy "$PG_PORT"; then
    die "port $PG_PORT is already in use — pass a free one: ./install.sh --pg-port <PORT>"
  fi
  docker run -d --name "$PG_CONTAINER" --restart unless-stopped \
    -e POSTGRES_USER=claw -e POSTGRES_PASSWORD="$PG_PASSWORD" -e POSTGRES_DB=claw \
    -p "127.0.0.1:${PG_PORT}:5432" \
    -v "${PG_VOLUME}:/var/lib/postgresql/data" \
    postgres:16-alpine >/dev/null
  ok "started '$PG_CONTAINER' on 127.0.0.1:$PG_PORT"
fi
printf "  waiting for Postgres"
for _ in $(seq 1 30); do
  if docker exec "$PG_CONTAINER" pg_isready -U claw >/dev/null 2>&1; then printf " ready\n"; PG_UP=1; break; fi
  printf "."; sleep 1
done
[[ "${PG_UP:-0}" == "1" ]] || die "Postgres did not become ready — check: docker logs $PG_CONTAINER"

# 5) .env (generate once; never clobber an existing one)
step "Writing configuration (.env)"
DB_URL="postgresql+asyncpg://claw:${PG_PASSWORD}@localhost:${PG_PORT}/claw"
if [[ -f .env ]]; then
  warn ".env already exists — leaving it untouched"
  info "if you change ports/password, update CLAW_DATABASE_URL / CLAW_PORT by hand"
else
  LLM_KEY="${CLAW_LLM__API_KEY:-}"
  LLM_MODEL="${CLAW_LLM__MODEL:-openrouter/anthropic/claude-sonnet-5}"
  if [[ -z "$LLM_KEY" && $ASSUME_YES -eq 0 && -t 0 ]]; then
    read -r -p "  LLM API key (LiteLLM/OpenRouter, leave blank to fill in later): " LLM_KEY || true
  fi
  cat > .env <<EOF
# Softnix PrivateClaw — generated by install.sh on host-native production.
CLAW_DATABASE_URL=${DB_URL}
CLAW_AUTO_MIGRATE=true
CLAW_SECRET_KEY=$(gen_secret)

# password mode disables the dev token; the FIRST account you register is admin.
CLAW_AUTH_MODE=password
CLAW_OPEN_REGISTRATION=true

# App bind + public URL (put your real https URL here when behind a proxy/tunnel).
CLAW_HOST=0.0.0.0
CLAW_PORT=${APP_PORT}
CLAW_PUBLIC_BASE_URL=http://localhost:${APP_PORT}
CLAW_WEB_BASE_URL=http://localhost:${APP_PORT}

# LLM (any LiteLLM-supported model).
CLAW_LLM__MODEL=${LLM_MODEL}
CLAW_LLM__API_KEY=${LLM_KEY}

# Tool-sandbox (runs on the host Docker daemon). bridge = agent can pip-install
# on demand (every exec is audit-logged); set none to isolate to the image.
CLAW_SANDBOX__ENABLED=true
CLAW_SANDBOX__IMAGE=${SANDBOX_IMAGE}
CLAW_SANDBOX__NETWORK=bridge
CLAW_SANDBOX__TIMEOUT_SECONDS=120

# Persistent data (attachments/agent files + knowledge OKF bundles).
CLAW_WORKSPACES_ROOT=${DATA_DIR}/workspaces
CLAW_KNOWLEDGE_ROOT=${DATA_DIR}/knowledge

# Optional speech-to-text (composer mic). Fill to enable.
QROQ_KEY=
QROQ_URL=https://api.groq.com/openai/v1
QROQ_MODEL=whisper-large-v3
EOF
  chmod 600 .env
  ok ".env written (secret key generated)"
  [[ -z "$LLM_KEY" ]] && warn "no LLM API key set — edit .env (CLAW_LLM__API_KEY) before chatting"
fi

# 6) Database migrations
step "Running database migrations"
set -a; # shellcheck disable=SC1091
. ./.env; set +a
uv run alembic upgrade head
ok "schema up to date"

# 7) System service
install_service() {
  if [[ "$OS" == "linux" ]] && have systemctl; then
    step "Installing systemd service (claw.service)"
    local unit="/tmp/claw.service.$$"
    cat > "$unit" <<EOF
[Unit]
Description=Softnix PrivateClaw (host-native)
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=simple
User=${SUDO_USER:-$USER}
WorkingDirectory=${PROJECT_DIR}
ExecStart=${PROJECT_DIR}/scripts/claw-serve.sh
Restart=always
RestartSec=3
TimeoutStopSec=30
Environment=PATH=${HOME}/.local/bin:/usr/local/bin:/usr/bin:/bin

[Install]
WantedBy=multi-user.target
EOF
    sudo install -m 644 "$unit" /etc/systemd/system/claw.service && rm -f "$unit"
    sudo systemctl daemon-reload
    sudo systemctl enable claw >/dev/null 2>&1 || true
    sudo systemctl restart claw
    ok "systemd service 'claw' enabled + started (auto-starts on boot)"
  elif [[ "$OS" == "macos" ]]; then
    step "Installing launchd agent (com.softnix.claw)"
    local plist="$HOME/Library/LaunchAgents/com.softnix.claw.plist"
    mkdir -p "$HOME/Library/LaunchAgents"
    cat > "$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.softnix.claw</string>
  <key>ProgramArguments</key>
  <array><string>${PROJECT_DIR}/scripts/claw-serve.sh</string></array>
  <key>WorkingDirectory</key><string>${PROJECT_DIR}</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>${PROJECT_DIR}/claw.out.log</string>
  <key>StandardErrorPath</key><string>${PROJECT_DIR}/claw.err.log</string>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>${HOME}/.local/bin:/usr/local/bin:/usr/bin:/bin</string></dict>
</dict></plist>
EOF
    launchctl unload "$plist" 2>/dev/null || true
    launchctl load -w "$plist"
    ok "launchd agent loaded + started (auto-starts on login)"
  else
    warn "no systemd/launchd — start manually with: ./scripts/claw start"
    INSTALL_SERVICE=0
  fi
}

chmod +x scripts/claw-serve.sh scripts/claw 2>/dev/null || true
if [[ $INSTALL_SERVICE -eq 1 ]]; then
  install_service
  # Convenience symlink so `claw ...` works from anywhere (best-effort).
  if [[ -w /usr/local/bin ]] || { [[ "$OS" == "linux" ]] && have sudo; }; then
    if [[ -w /usr/local/bin ]]; then ln -sf "$PROJECT_DIR/scripts/claw" /usr/local/bin/claw 2>/dev/null || true
    else sudo ln -sf "$PROJECT_DIR/scripts/claw" /usr/local/bin/claw 2>/dev/null || true; fi
  fi
else
  step "Starting the app (no service)"
  ./scripts/claw start
fi

# 8) Health check
step "Verifying the app responds"
printf "  waiting for http://127.0.0.1:%s/api/ready" "$APP_PORT"
HEALTHY=0
for _ in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:${APP_PORT}/api/ready" >/dev/null 2>&1; then printf " OK\n"; HEALTHY=1; break; fi
  printf "."; sleep 1
done

# ═══════════════════════════ summary ═════════════════════════════════
printf "\n${B}${GRN}✓ Installation complete${R}\n\n"
if [[ $HEALTHY -eq 1 ]]; then
  printf "  ${B}Open:${R}  http://localhost:%s   ${DIM}(register the first account — it becomes admin)${R}\n" "$APP_PORT"
else
  warn "app not responding yet — check logs:  ./scripts/claw logs"
fi
cat <<EOF

  ${B}Manage the service${R}
    claw status        # or ./scripts/claw status
    claw restart
    claw logs
    claw update        # git pull + deps + rebuild web + migrate + restart

  ${B}Notes${R}
    • Config lives in ${PROJECT_DIR}/.env (chmod 600). Set CLAW_LLM__API_KEY if you skipped it.
    • Put this behind your reverse proxy / tunnel for TLS; the app listens on :${APP_PORT}.
    • Data (uploads, agent files, knowledge) persists under ${DATA_DIR}.
    • Postgres runs as container '${PG_CONTAINER}' (volume '${PG_VOLUME}'); back it up with pg_dump.
EOF
