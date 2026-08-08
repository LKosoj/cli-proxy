#!/usr/bin/env bash
set -euo pipefail

# Interactive first-time setup for CLI Proxy Telegram Bridge on Ubuntu/Debian.
# - Installs system deps + Python venv deps
# - Installs CLI tools (codex/claude/gemini/qwen/kimi via npm, grok via xAI installer)
# - Collects required config values from user
# - Creates config.yaml + .env
# - Creates and starts a systemd service

if [[ -z "${BASH_VERSION:-}" ]]; then
  echo "Run this script with bash."
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$SCRIPT_DIR"
CONFIG_EXAMPLE="$REPO_DIR/config_example.yaml"
CONFIG_PATH="$REPO_DIR/config.yaml"
ENV_PATH="$REPO_DIR/.env"
VENV_DIR="$REPO_DIR/.venv"

usage() {
  cat <<'USAGE'
Usage:
  ./setup_bot.sh [options]

Options:
  --non-interactive           Run without prompts (requires required params)
  --bot-token TOKEN           Telegram bot token
  --whitelist IDS             Comma-separated chat IDs (e.g. 123,-100987)
  --admins IDS                Comma-separated admin chat IDs (must be subset of whitelist)
  --user-workdirs SPEC        Optional per-user allowed projects:
                              "CHATID:/abs/p1,/abs/p2;CHATID2:/abs/p3"
  --workdir PATH              defaults.workdir
  --service-name NAME         systemd service name (default: cli-proxy-bot)
  --service-user USER         systemd service user (default: current/sudo user)
  --chown yes|no              Chown repo/workdir to service user (default: prompt in interactive, no in non-interactive)
  -h, --help                  Show this help

Non-interactive mode can also read env vars:
  SETUP_BOT_TOKEN, SETUP_WHITELIST_RAW, SETUP_ADMINLIST_RAW, SETUP_USER_WORKDIRS_RAW, SETUP_WORKDIR,
  SETUP_SERVICE_NAME, SETUP_SERVICE_USER, SETUP_CHOWN,
  OPENAI_API_KEY, OPENAI_MODEL, OPENAI_BIG_MODEL, OPENAI_BASE_URL,
  ANTHROPIC_API_KEY, GOOGLE_API_KEY, QWEN_API_KEY, XAI_API_KEY, KIMI_API_KEY, ZAI_API_KEY, TAVILY_API_KEY, JINA_API_KEY

Required in all modes:
  OPENAI_API_KEY, OPENAI_MODEL, OPENAI_BIG_MODEL
USAGE
}

NON_INTERACTIVE=0
BOT_TOKEN="${SETUP_BOT_TOKEN:-}"
WHITELIST_RAW="${SETUP_WHITELIST_RAW:-}"
ADMINS_RAW="${SETUP_ADMINLIST_RAW:-}"
USER_WORKDIRS_RAW="${SETUP_USER_WORKDIRS_RAW:-}"
WORKDIR="${SETUP_WORKDIR:-}"
SERVICE_NAME="${SETUP_SERVICE_NAME:-}"
SERVICE_USER="${SETUP_SERVICE_USER:-}"
CHOWN_REPLY="${SETUP_CHOWN:-}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --non-interactive) NON_INTERACTIVE=1; shift ;;
    --bot-token) BOT_TOKEN="${2:-}"; shift 2 ;;
    --whitelist) WHITELIST_RAW="${2:-}"; shift 2 ;;
    --admins) ADMINS_RAW="${2:-}"; shift 2 ;;
    --user-workdirs) USER_WORKDIRS_RAW="${2:-}"; shift 2 ;;
    --workdir) WORKDIR="${2:-}"; shift 2 ;;
    --service-name) SERVICE_NAME="${2:-}"; shift 2 ;;
    --service-user) SERVICE_USER="${2:-}"; shift 2 ;;
    --chown) CHOWN_REPLY="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1"; usage; exit 1 ;;
  esac
done

if [[ ! -f "$CONFIG_EXAMPLE" ]]; then
  echo "config_example.yaml not found in $REPO_DIR"
  exit 1
fi

if [[ -r /etc/os-release ]]; then
  # shellcheck disable=SC1091
  source /etc/os-release
  DISTRO_ID="${ID:-unknown}"
  if [[ "$DISTRO_ID" != "ubuntu" && "$DISTRO_ID" != "debian" ]]; then
    echo "Warning: this script is designed for Ubuntu/Debian, detected: $DISTRO_ID"
  fi
fi

if [[ "${EUID}" -eq 0 ]]; then
  SUDO=""
  DEFAULT_SERVICE_USER="${SUDO_USER:-root}"
else
  if ! command -v sudo >/dev/null 2>&1; then
    echo "sudo is required. Install sudo or run as root."
    exit 1
  fi
  SUDO="sudo"
  DEFAULT_SERVICE_USER="${USER}"
fi

need_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1"
    exit 1
  fi
}

need_cmd apt-get
need_cmd systemctl
need_cmd python3

echo "==> Installing system dependencies (apt)"
$SUDO apt-get update -y
$SUDO apt-get install -y \
  python3 \
  python3-venv \
  python3-pip \
  python3-yaml \
  git \
  curl \
  ca-certificates \
  nodejs \
  npm \
  ripgrep

echo "==> Creating/updating Python virtualenv"
python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/python" -m pip install --upgrade pip wheel
"$VENV_DIR/bin/pip" install -r "$REPO_DIR/requirements.txt"

install_npm_cli() {
  local bin_name="$1"
  local package_name="$2"
  if command -v "$bin_name" >/dev/null 2>&1; then
    echo " - $bin_name already installed"
    return 0
  fi
  echo " - Installing $bin_name via npm package $package_name"
  if $SUDO npm install -g "$package_name"; then
    if command -v "$bin_name" >/dev/null 2>&1; then
      echo "   Installed: $bin_name"
    else
      echo "   Warning: npm install succeeded, but '$bin_name' not found in PATH."
    fi
  else
    echo "   Warning: failed to install $package_name"
  fi
}

echo "==> Installing CLI tools (best effort)"
install_npm_cli "codex" "@openai/codex"
install_npm_cli "claude" "@anthropic-ai/claude-code"
install_npm_cli "gemini" "@google/gemini-cli"
install_npm_cli "qwen" "@qwen-code/qwen-code"
install_npm_cli "kimi" "@moonshot-ai/kimi-code"

install_grok_cli() {
  if command -v grok >/dev/null 2>&1; then
    echo " - grok already installed"
    return 0
  fi
  echo " - Installing grok via official xAI installer"
  if curl -fsSL https://x.ai/cli/install.sh | bash; then
    if command -v grok >/dev/null 2>&1; then
      echo "   Installed: grok"
    else
      echo "   Warning: xAI installer completed, but 'grok' not found in PATH."
    fi
  else
    echo "   Warning: failed to install grok via official xAI installer"
  fi
}

install_grok_cli

read_required() {
  local prompt="$1"
  local value=""
  while true; do
    read -r -p "$prompt" value
    if [[ -n "$value" ]]; then
      printf '%s' "$value"
      return 0
    fi
    echo "Value is required."
  done
}

echo
echo "==> Required bot parameters"
if [[ "$NON_INTERACTIVE" -eq 1 ]]; then
  BOT_TOKEN="${BOT_TOKEN:-}"
  WHITELIST_RAW="${WHITELIST_RAW:-}"
  ADMINS_RAW="${ADMINS_RAW:-}"
  USER_WORKDIRS_RAW="${USER_WORKDIRS_RAW:-}"
  WORKDIR="${WORKDIR:-$HOME/projects}"
  if [[ -z "$BOT_TOKEN" || -z "$WHITELIST_RAW" || -z "$ADMINS_RAW" ]]; then
    echo "In --non-interactive mode required params are missing."
    echo "Required: --bot-token, --whitelist, --admins (or SETUP_BOT_TOKEN/SETUP_WHITELIST_RAW/SETUP_ADMINLIST_RAW)."
    exit 1
  fi
else
  BOT_TOKEN="${BOT_TOKEN:-$(read_required "Telegram bot token: ")}"
  WHITELIST_RAW="${WHITELIST_RAW:-$(read_required "Allowed chat IDs (comma-separated, e.g. 12345,-10098765): ")}"
  if [[ -z "$ADMINS_RAW" ]]; then
    read -r -p "Admin chat IDs (comma-separated) [same as whitelist]: " ADMINS_RAW
  fi
  ADMINS_RAW="${ADMINS_RAW:-$WHITELIST_RAW}"
  if [[ -z "$USER_WORKDIRS_RAW" ]]; then
    echo "Optional: per-user allowed projects for non-admins."
    echo "Format: CHATID:/abs/p1,/abs/p2;CHATID2:/abs/p3"
    read -r -p "user_workdirs (leave empty to skip): " USER_WORKDIRS_RAW
  fi
  if [[ -z "$WORKDIR" ]]; then
    read -r -p "defaults.workdir [${HOME}/projects]: " WORKDIR
  fi
  WORKDIR="${WORKDIR:-$HOME/projects}"
fi
mkdir -p "$WORKDIR"

echo
echo "==> OpenAI parameters (required)"
OPENAI_API_KEY="${OPENAI_API_KEY:-}"
OPENAI_MODEL="${OPENAI_MODEL:-}"
OPENAI_BIG_MODEL="${OPENAI_BIG_MODEL:-}"
OPENAI_BASE_URL="${OPENAI_BASE_URL:-}"
ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY:-}"
GOOGLE_API_KEY="${GOOGLE_API_KEY:-}"
QWEN_API_KEY="${QWEN_API_KEY:-}"
XAI_API_KEY="${XAI_API_KEY:-}"
KIMI_API_KEY="${KIMI_API_KEY:-}"
ZAI_API_KEY="${ZAI_API_KEY:-}"
TAVILY_API_KEY="${TAVILY_API_KEY:-}"
JINA_API_KEY="${JINA_API_KEY:-}"

if [[ "$NON_INTERACTIVE" -ne 1 ]]; then
  if [[ -z "$OPENAI_API_KEY" ]]; then
    OPENAI_API_KEY="$(read_required "OPENAI_API_KEY: ")"
  fi
  if [[ -z "$OPENAI_MODEL" ]]; then
    OPENAI_MODEL="$(read_required "OPENAI_MODEL (e.g. gpt-4.1-mini): ")"
  fi
  if [[ -z "$OPENAI_BIG_MODEL" ]]; then
    OPENAI_BIG_MODEL="$(read_required "OPENAI_BIG_MODEL (e.g. gpt-4.1): ")"
  fi
  read -r -p "OPENAI_BASE_URL [https://api.openai.com]: " OPENAI_BASE_URL
fi

if [[ -z "$OPENAI_API_KEY" || -z "$OPENAI_MODEL" || -z "$OPENAI_BIG_MODEL" ]]; then
  echo "OpenAI parameters are required."
  echo "Set OPENAI_API_KEY, OPENAI_MODEL and OPENAI_BIG_MODEL (via env or prompts)."
  exit 1
fi

echo
echo "==> Optional non-OpenAI API keys (press Enter to skip)"
if [[ "$NON_INTERACTIVE" -ne 1 ]]; then
  read -r -p "ANTHROPIC_API_KEY: " ANTHROPIC_API_KEY
  read -r -p "GOOGLE_API_KEY: " GOOGLE_API_KEY
  read -r -p "QWEN_API_KEY: " QWEN_API_KEY
  read -r -p "XAI_API_KEY: " XAI_API_KEY
  read -r -p "KIMI_API_KEY: " KIMI_API_KEY
  read -r -p "ZAI_API_KEY: " ZAI_API_KEY
  read -r -p "TAVILY_API_KEY: " TAVILY_API_KEY
  read -r -p "JINA_API_KEY: " JINA_API_KEY
fi

OPENAI_BASE_URL="${OPENAI_BASE_URL:-https://api.openai.com}"

echo
echo "==> Writing config.yaml"
if [[ -f "$CONFIG_PATH" ]]; then
  cp "$CONFIG_PATH" "$CONFIG_PATH.bak.$(date +%Y%m%d%H%M%S)"
fi

export SETUP_BOT_TOKEN="$BOT_TOKEN"
export SETUP_REPO_DIR="$REPO_DIR"
export SETUP_WHITELIST_RAW="$WHITELIST_RAW"
export SETUP_ADMINLIST_RAW="$ADMINS_RAW"
export SETUP_USER_WORKDIRS_RAW="$USER_WORKDIRS_RAW"
export SETUP_WORKDIR="$WORKDIR"
export SETUP_OPENAI_API_KEY="$OPENAI_API_KEY"
export SETUP_OPENAI_MODEL="$OPENAI_MODEL"
export SETUP_OPENAI_BIG_MODEL="$OPENAI_BIG_MODEL"
export SETUP_OPENAI_BASE_URL="$OPENAI_BASE_URL"
export SETUP_ZAI_API_KEY="$ZAI_API_KEY"
export SETUP_TAVILY_API_KEY="$TAVILY_API_KEY"
export SETUP_JINA_API_KEY="$JINA_API_KEY"

python3 - <<'PY'
import os
import yaml

repo_dir = os.environ["SETUP_REPO_DIR"]
config_example = os.path.join(repo_dir, "config_example.yaml")
config_path = os.path.join(repo_dir, "config.yaml")

with open(config_example, "r", encoding="utf-8") as f:
    cfg = yaml.safe_load(f)

def parse_ids(raw: str):
    out = []
    raw = (raw or "").replace(" ", "")
    for part in raw.split(","):
        if not part:
            continue
        out.append(int(part))
    return out

raw_whitelist = os.environ.get("SETUP_WHITELIST_RAW", "")
raw_admins = os.environ.get("SETUP_ADMINLIST_RAW", "")
whitelist_ids = parse_ids(raw_whitelist)
admin_ids = parse_ids(raw_admins)

whitelist_set = set(whitelist_ids)
admins_not_in_whitelist = [x for x in admin_ids if x not in whitelist_set]
if admins_not_in_whitelist:
    raise SystemExit(f"Admins must be a subset of whitelist_chat_ids. Offenders: {admins_not_in_whitelist}")

def parse_user_workdirs(raw: str):
    """
    SPEC format:
      "CHATID:/abs/p1,/abs/p2;CHATID2:/abs/p3"
    """
    raw = (raw or "").strip()
    if not raw:
        return {}
    mapping = {}
    chunks = [c.strip() for c in raw.split(";") if c.strip()]
    for chunk in chunks:
        if ":" not in chunk:
            raise ValueError(f"Invalid user-workdirs chunk (missing ':'): {chunk!r}")
        chat_str, paths_str = chunk.split(":", 1)
        chat_id = int(chat_str.strip())
        paths = [p.strip() for p in paths_str.split(",") if p.strip()]
        if not paths:
            continue
        mapping[chat_id] = paths
    return mapping

cfg["telegram"]["token"] = os.environ["SETUP_BOT_TOKEN"]
cfg["telegram"]["whitelist_chat_ids"] = whitelist_ids
cfg["telegram"]["admlist_chat_ids"] = admin_ids
cfg["telegram"]["user_workdirs"] = parse_user_workdirs(os.environ.get("SETUP_USER_WORKDIRS_RAW", ""))
cfg["defaults"]["workdir"] = os.environ["SETUP_WORKDIR"]

def set_or_none(path, value):
    d = cfg
    for key in path[:-1]:
        d = d.setdefault(key, {})
    d[path[-1]] = value if value else None

set_or_none(["defaults", "openai_api_key"], os.environ.get("SETUP_OPENAI_API_KEY", ""))
set_or_none(["defaults", "openai_model"], os.environ.get("SETUP_OPENAI_MODEL", ""))
set_or_none(["defaults", "openai_big_model"], os.environ.get("SETUP_OPENAI_BIG_MODEL", ""))
cfg["defaults"]["openai_base_url"] = os.environ.get("SETUP_OPENAI_BASE_URL", "https://api.openai.com")
set_or_none(["defaults", "zai_api_key"], os.environ.get("SETUP_ZAI_API_KEY", ""))
set_or_none(["defaults", "tavily_api_key"], os.environ.get("SETUP_TAVILY_API_KEY", ""))
set_or_none(["defaults", "jina_api_key"], os.environ.get("SETUP_JINA_API_KEY", ""))

with open(config_path, "w", encoding="utf-8") as f:
    yaml.safe_dump(cfg, f, sort_keys=False, allow_unicode=False)
PY

echo "==> Writing .env (for CLI auth keys)"
{
  echo "# Generated by setup_bot.sh on $(date -u +"%Y-%m-%dT%H:%M:%SZ")"
  [[ -n "$OPENAI_API_KEY" ]] && echo "OPENAI_API_KEY=$OPENAI_API_KEY"
  [[ -n "$OPENAI_MODEL" ]] && echo "OPENAI_MODEL=$OPENAI_MODEL"
  [[ -n "$OPENAI_BIG_MODEL" ]] && echo "OPENAI_BIG_MODEL=$OPENAI_BIG_MODEL"
  [[ -n "$OPENAI_BASE_URL" ]] && echo "OPENAI_BASE_URL=$OPENAI_BASE_URL"
  [[ -n "$ANTHROPIC_API_KEY" ]] && echo "ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY"
  [[ -n "$GOOGLE_API_KEY" ]] && echo "GOOGLE_API_KEY=$GOOGLE_API_KEY"
  [[ -n "$QWEN_API_KEY" ]] && echo "QWEN_API_KEY=$QWEN_API_KEY"
  [[ -n "$XAI_API_KEY" ]] && echo "XAI_API_KEY=$XAI_API_KEY"
  [[ -n "$KIMI_API_KEY" ]] && echo "KIMI_API_KEY=$KIMI_API_KEY"
  [[ -n "$ZAI_API_KEY" ]] && echo "ZAI_API_KEY=$ZAI_API_KEY"
  [[ -n "$TAVILY_API_KEY" ]] && echo "TAVILY_API_KEY=$TAVILY_API_KEY"
  [[ -n "$JINA_API_KEY" ]] && echo "JINA_API_KEY=$JINA_API_KEY"
} > "$ENV_PATH"
chmod 600 "$ENV_PATH"

echo
echo "==> systemd service setup"
if [[ "$NON_INTERACTIVE" -ne 1 ]]; then
  read -r -p "Service name [cli-proxy-bot]: " SERVICE_NAME
fi
SERVICE_NAME="${SERVICE_NAME:-cli-proxy-bot}"
if [[ "$NON_INTERACTIVE" -ne 1 ]]; then
  read -r -p "Run service as user [$DEFAULT_SERVICE_USER]: " SERVICE_USER
fi
SERVICE_USER="${SERVICE_USER:-$DEFAULT_SERVICE_USER}"
SERVICE_PATH="/etc/systemd/system/${SERVICE_NAME}.service"

cat <<SERVICE | $SUDO tee "$SERVICE_PATH" >/dev/null
[Unit]
Description=CLI Proxy Telegram Bridge Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_USER
WorkingDirectory=$REPO_DIR
EnvironmentFile=$ENV_PATH
Environment=PYTHONUNBUFFERED=1
Environment=PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
ExecStart=$VENV_DIR/bin/python $REPO_DIR/bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
SERVICE

$SUDO systemctl daemon-reload
$SUDO systemctl enable --now "$SERVICE_NAME"

if id "$SERVICE_USER" >/dev/null 2>&1; then
  if [[ "$NON_INTERACTIVE" -ne 1 ]]; then
    read -r -p "Give '$SERVICE_USER' ownership of repo and workdir? [Y/n]: " CHOWN_REPLY
  fi
  if [[ "$NON_INTERACTIVE" -eq 1 ]]; then
    CHOWN_REPLY="${CHOWN_REPLY:-n}"
  else
    CHOWN_REPLY="${CHOWN_REPLY:-Y}"
  fi
  if [[ "$CHOWN_REPLY" =~ ^[Yy]$ ]]; then
    $SUDO chown -R "$SERVICE_USER:$SERVICE_USER" "$REPO_DIR"
    $SUDO chown -R "$SERVICE_USER:$SERVICE_USER" "$WORKDIR"
  fi
else
  echo "Warning: user '$SERVICE_USER' does not exist. Ownership not changed."
fi

echo
echo "==> Setup completed"
echo "Service: $SERVICE_NAME"
echo "Config:  $CONFIG_PATH"
echo "Env:     $ENV_PATH"
echo
echo "Useful commands:"
echo "  sudo systemctl status $SERVICE_NAME --no-pager -l"
echo "  sudo journalctl -u $SERVICE_NAME -f"
