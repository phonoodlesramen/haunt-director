#!/usr/bin/env bash
# Haunt Director installer for a Raspberry Pi that already runs Falcon Player (FPP).
#
#   cd haunt-director && ./deploy/install.sh
#
# Safe to re-run. It never touches FPP's own settings: once the app is up, open
# the System tab and its setup check will tell you (and fix, one click each)
# anything FPP still needs.
#
# Options:
#   --dry-run       print what would happen, change nothing
#   --no-sudoers    skip installing deploy/sudoers.d/haunt-director
#   --no-service    skip the systemd service (run it by hand instead)
set -euo pipefail

DRY=0; SUDOERS=1; SERVICE=1
for a in "$@"; do
  case "$a" in
    --dry-run) DRY=1 ;;
    --no-sudoers) SUDOERS=0 ;;
    --no-service) SERVICE=0 ;;
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
    *) echo "unknown option: $a" >&2; exit 2 ;;
  esac
done

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND="$REPO/backend"
APP_USER="$(id -un)"
say()  { printf '\033[1;33m🎃 %s\033[0m\n' "$*"; }
warn() { printf '\033[1;31m⚠  %s\033[0m\n' "$*"; }
run()  { if [ "$DRY" = 1 ]; then echo "  would run: $*"; else "$@"; fi; }

[ "$(id -u)" = 0 ] && { warn "Run this as your normal user (it uses sudo where needed), not as root."; exit 1; }

say "Haunt Director installer  (repo: $REPO, user: $APP_USER)"

# --- sanity checks ---------------------------------------------------------
if [ -d /opt/fpp ]; then
  say "Found FPP in /opt/fpp."
else
  warn "No FPP found in /opt/fpp. Haunt Director drives the lights THROUGH FPP, so install FPP first"
  warn "(https://github.com/FalconChristmas/fpp). Continuing anyway so you can poke at the UI."
fi
if ! command -v python3 >/dev/null; then warn "python3 is missing."; exit 1; fi
PYV="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
  || { warn "Python $PYV found; Haunt Director needs 3.11 or newer."; exit 1; }

# --- system packages ---------------------------------------------------------
NEED=()
command -v ffmpeg >/dev/null || NEED+=(ffmpeg)
python3 -c 'import venv, ensurepip' 2>/dev/null || NEED+=(python3-venv)
if [ ${#NEED[@]} -gt 0 ]; then
  say "Installing: ${NEED[*]}"
  run sudo apt-get update
  run sudo apt-get install -y "${NEED[@]}"
else
  say "ffmpeg and python3-venv already installed."
fi

# --- groups: sound card, GPIO (sensor), video (vcgencmd for power/heat checks) ---
for g in audio gpio video; do
  if getent group "$g" >/dev/null && ! id -nG "$APP_USER" | tr ' ' '\n' | grep -qx "$g"; then
    say "Adding $APP_USER to the '$g' group (takes effect for the service right away)."
    run sudo usermod -aG "$g" "$APP_USER"
  fi
done

# --- python environment ------------------------------------------------------
if [ ! -x "$BACKEND/.venv/bin/python" ]; then
  say "Creating the Python environment (backend/.venv)…"
  run python3 -m venv "$BACKEND/.venv"
fi
say "Installing Python packages (a few minutes the first time)…"
run "$BACKEND/.venv/bin/pip" install --upgrade pip
run "$BACKEND/.venv/bin/pip" install -r "$BACKEND/requirements.txt"

# --- settings ----------------------------------------------------------------
if [ ! -f "$BACKEND/.env" ]; then
  say "Creating backend/.env from .env.example (edit it any time)."
  run cp "$BACKEND/.env.example" "$BACKEND/.env"
else
  say "Keeping your existing backend/.env."
fi

# --- sudo for the System tab's fixes and restart buttons ---------------------
if [ "$SUDOERS" = 1 ]; then
  if sudo -n true 2>/dev/null; then
    say "$APP_USER already has passwordless sudo; no sudoers file needed."
  else
    say "Installing a sudoers rule so the app can restart FPP and apply setup fixes."
    TMP="$(mktemp)"
    sed "s/@USER@/$APP_USER/g" "$REPO/deploy/sudoers.d/haunt-director" > "$TMP"
    if sudo visudo -cf "$TMP" >/dev/null; then
      run sudo install -m 0440 -o root -g root "$TMP" /etc/sudoers.d/haunt-director
    else
      warn "The sudoers rule didn't validate; skipped. The app still runs, but its Fix/Restart buttons won't."
    fi
    rm -f "$TMP"
  fi
fi

# --- service -------------------------------------------------------------------
if [ "$SERVICE" = 1 ]; then
  say "Installing the haunt-director service (starts at boot)."
  TMP="$(mktemp)"
  sed -e "s|@USER@|$APP_USER|g" -e "s|@BACKEND@|$BACKEND|g" "$REPO/deploy/haunt-director.service" > "$TMP"
  run sudo install -m 0644 "$TMP" /etc/systemd/system/haunt-director.service
  rm -f "$TMP"
  run sudo systemctl daemon-reload
  run sudo systemctl enable haunt-director
  run sudo systemctl restart haunt-director
fi

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo
say "Done! Open  http://${IP:-<your-pi>}:8000  on your phone."
say "First stop: the System tab. Every red or yellow row has a one-click fix or a hint."
[ "$SERVICE" = 0 ] && say "Run it by hand:  cd backend && .venv/bin/uvicorn haunt_director.api.app:app --host 0.0.0.0 --port 8000"
exit 0
