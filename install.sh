#!/bin/sh
# bgst installer for Linux, macOS, *BSD and WSL.
#
#   ./install.sh                 install for the current user
#   ./install.sh --with-player   also install an audio player (asks first)
#   ./install.sh --shortcut      add menu and desktop shortcuts without asking
#   ./install.sh --no-shortcut   skip the shortcuts
#   ./install.sh --uninstall     remove it again
#
# Double-clicking this in Dolphin, Nautilus or Thunar runs it with no terminal
# attached, so everything it prints goes nowhere and it looks like nothing
# happened. When that is the case it reopens itself in a terminal window; pass
# --no-terminal to stop that.
#
# Nothing here needs root except an optional player install through your own
# package manager, which is always shown before it runs.
set -eu

APP=bgst
PREFIX="${PREFIX:-$HOME/.local}"
BIN="$PREFIX/bin"
VENV="${BGST_VENV:-$HOME/.local/share/bgst/venv}"
SRC=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

RED=''; GREEN=''; DIM=''; BOLD=''; OFF=''
if [ -t 1 ]; then
  RED=$(printf '\033[31m'); GREEN=$(printf '\033[32m')
  DIM=$(printf '\033[2m'); BOLD=$(printf '\033[1m'); OFF=$(printf '\033[0m')
fi
say()  { printf '%s\n' "$*"; }
step() { printf '%s==>%s %s\n' "$GREEN$BOLD" "$OFF" "$*"; }
warn() { printf '%s!!%s %s\n' "$RED" "$OFF" "$*" >&2; }
die()  { warn "$*"; exit 1; }

WITH_PLAYER=0
UNINSTALL=0
SHORTCUT=ask
REOPEN=1
for arg in "$@"; do
  case "$arg" in
    --with-player) WITH_PLAYER=1 ;;
    --uninstall) UNINSTALL=1 ;;
    --shortcut) SHORTCUT=yes ;;
    --no-shortcut) SHORTCUT=no ;;
    --no-terminal) REOPEN=0 ;;
    -h|--help) sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown option: $arg" ;;
  esac
done

# -- run somewhere the output can be read ----------------------------------
# A file manager launches this with no terminal: stdout is not a tty and
# nothing is visible. Reopen in a terminal emulator so the install can be
# watched (and so the questions below can be answered).
open_in_terminal() {
  [ "$REOPEN" -eq 1 ] || return 1
  [ -t 1 ] && return 1
  [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ] || return 1
  [ -n "${BGST_REOPENED:-}" ] && return 1

  for term in konsole gnome-terminal xfce4-terminal ptyxis kitty alacritty foot xterm x-terminal-emulator; do
    command -v "$term" >/dev/null 2>&1 || continue
    BGST_REOPENED=1
    export BGST_REOPENED
    inner="'$SRC/$(basename "$0")' $* ; printf '\nPress Enter to close '; read -r _"
    case "$term" in
      gnome-terminal|ptyxis) "$term" -- sh -c "$inner" ;;
      konsole|xfce4-terminal) "$term" -e sh -c "$inner" ;;
      *) "$term" -e sh -c "$inner" ;;
    esac
    return 0
  done
  return 1
}

if [ "$UNINSTALL" -eq 0 ] && open_in_terminal "$@"; then
  exit 0
fi

# -- python ----------------------------------------------------------------
find_python() {
  for candidate in python3.13 python3.12 python3.11 python3.10 python3.9 python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 &&
       "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
      command -v "$candidate"
      return 0
    fi
  done
  return 1
}

# -- uninstall -------------------------------------------------------------
if [ "$UNINSTALL" -eq 1 ]; then
  step "Removing $APP"
  rm -f "$BIN/$APP"
  rm -rf "$VENV"
  rm -f "$HOME/.local/share/applications/bgst.desktop"
  rm -f "$HOME/Desktop/bgst.desktop"
  DESKTOP_DIR=$( (command -v xdg-user-dir >/dev/null 2>&1 && xdg-user-dir DESKTOP) || echo "$HOME/Desktop")
  rm -f "$DESKTOP_DIR/bgst.desktop"
  say "Removed. Your library and config were left alone:"
  say "  ${DIM}~/.local/share/custom soundtrack${OFF}   ${DIM}~/.config/bgsoundtrack${OFF}"
  exit 0
fi

PYTHON=$(find_python) || die "Python 3.9+ not found. Install python3 and run this again."
step "Using $PYTHON ($("$PYTHON" -c 'import platform; print(platform.python_version())'))"

# -- install ---------------------------------------------------------------
mkdir -p "$BIN"
if command -v pipx >/dev/null 2>&1; then
  step "Installing with pipx"
  pipx install --force "$SRC"
else
  step "Installing into $VENV"
  "$PYTHON" -m venv "$VENV" 2>/dev/null || die "could not create a venv. On Debian/Ubuntu: sudo apt install python3-venv"
  "$VENV/bin/python" -m pip install --quiet --upgrade pip
  "$VENV/bin/python" -m pip install --quiet "$SRC"
  ln -sf "$VENV/bin/$APP" "$BIN/$APP"
  say "Linked $BIN/$APP"
fi

# -- audio player ----------------------------------------------------------
have_player() {
  for p in ffplay mpv afplay vlc cvlc; do
    command -v "$p" >/dev/null 2>&1 && return 0
  done
  return 1
}

player_command() {
  if command -v brew    >/dev/null 2>&1; then echo "brew install ffmpeg"; return; fi
  if command -v apt-get >/dev/null 2>&1; then echo "sudo apt-get install -y ffmpeg"; return; fi
  if command -v dnf     >/dev/null 2>&1; then echo "sudo dnf install -y ffmpeg-free"; return; fi
  if command -v pacman  >/dev/null 2>&1; then echo "sudo pacman -S --noconfirm ffmpeg"; return; fi
  if command -v zypper  >/dev/null 2>&1; then echo "sudo zypper install -y ffmpeg"; return; fi
  if command -v apk     >/dev/null 2>&1; then echo "sudo apk add ffmpeg"; return; fi
  if command -v pkg     >/dev/null 2>&1; then echo "sudo pkg install -y ffmpeg"; return; fi
  if command -v xbps-install >/dev/null 2>&1; then echo "sudo xbps-install -y ffmpeg"; return; fi
  echo ""
}

if have_player; then
  step "Audio player found"
else
  CMD=$(player_command)
  if [ -z "$CMD" ]; then
    warn "No audio player found. Install ffmpeg, mpv or vlc with your package manager."
  elif [ "$WITH_PLAYER" -eq 1 ]; then
    step "Installing a player: $CMD"
    sh -c "$CMD"
  else
    say ""
    say "No audio player found. bgst needs one of ffmpeg / mpv / vlc."
    printf 'Run %s%s%s now? [y/N] ' "$BOLD" "$CMD" "$OFF"
    if [ -t 0 ]; then read -r reply; else reply=n; fi
    case "$reply" in [yY]*) sh -c "$CMD" ;; *) say "Skipped - run it yourself later." ;; esac
  fi
fi

# -- optional: Tk, for the system file chooser in the UI -------------------
if ! "$PYTHON" -c 'import tkinter' >/dev/null 2>&1; then
  case "$(uname -s)" in
    Linux)
      say ""
      say "Optional: Tk is missing, so the UI will use its own file browser"
      say "instead of your desktop's folder chooser. Install it with e.g."
      if command -v apt-get >/dev/null 2>&1; then say "    ${DIM}sudo apt-get install python3-tk${OFF}"
      elif command -v dnf >/dev/null 2>&1; then say "    ${DIM}sudo dnf install python3-tkinter${OFF}"
      elif command -v pacman >/dev/null 2>&1; then say "    ${DIM}sudo pacman -S tk${OFF}"
      fi
      ;;
  esac
fi

# -- shortcuts (Linux) -----------------------------------------------------
install_shortcuts() {
  APPS="$HOME/.local/share/applications"
  mkdir -p "$APPS"
  sed "s|Exec=bgst ui|Exec=$BIN/$APP ui|" "$SRC/packaging/bgst.desktop" > "$APPS/bgst.desktop"
  chmod +x "$APPS/bgst.desktop"
  step "Added bgst to your application menu"

  DESKTOP_DIR=$( (command -v xdg-user-dir >/dev/null 2>&1 && xdg-user-dir DESKTOP) || echo "$HOME/Desktop")
  if [ -d "$DESKTOP_DIR" ]; then
    cp "$APPS/bgst.desktop" "$DESKTOP_DIR/bgst.desktop"
    chmod +x "$DESKTOP_DIR/bgst.desktop"
    # KDE refuses to run a desktop file it does not trust; this is the flag
    # Plasma sets when you click "Trust this executable".
    command -v kwriteconfig5 >/dev/null 2>&1 && \
      kwriteconfig5 --file "$DESKTOP_DIR/bgst.desktop" --group "Desktop Entry" \
        --key "X-KDE-AuthorizeExecute" "true" 2>/dev/null || true
    step "Put a shortcut on your desktop"
  fi
}

if [ "$(uname -s)" = "Linux" ] && [ -f "$SRC/packaging/bgst.desktop" ]; then
  case "$SHORTCUT" in
    yes) install_shortcuts ;;
    no) ;;
    *)
      say ""
      printf 'Add bgst to your application menu and desktop? [Y/n] '
      if [ -t 0 ]; then read -r reply; else reply=y; fi
      case "$reply" in [nN]*) say "Skipped." ;; *) install_shortcuts ;; esac
      ;;
  esac
fi

# -- PATH ------------------------------------------------------------------
case ":$PATH:" in
  *":$BIN:"*) ;;
  *)
    say ""
    warn "$BIN is not on your PATH. Add this to your shell profile:"
    say "    export PATH=\"\$HOME/.local/bin:\$PATH\""
    ;;
esac

say ""
step "Done"
if [ -t 0 ]; then
  printf 'Open the control panel now? [Y/n] '
  read -r reply
  case "$reply" in [nN]*) ;; *) "$BIN/$APP" ui & sleep 1 ;; esac
fi
say ""
say "  ${BOLD}bgst ui${OFF}                     open the control panel"
say "  ${BOLD}bgst link ~/Music/album${OFF}     add music (symlinks, no copies)"
say "  ${BOLD}bgst play${OFF}                   play from the terminal"
