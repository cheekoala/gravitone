#!/bin/sh
# gravitone installer for Linux, macOS, *BSD and WSL.
#
#   ./install.sh                 install for the current user
#   ./install.sh --yes           never ask anything; take every default
#   ./install.sh --with-player   also install an audio player (asks first)
#   ./install.sh --shortcut      add menu and desktop shortcuts without asking
#   ./install.sh --no-shortcut   skip the shortcuts
#   ./install.sh --path          put ~/.local/bin on your PATH without asking
#   ./install.sh --no-path       leave your shell profile alone
#   ./install.sh --prefix DIR    install somewhere other than ~/.local
#   ./install.sh --uninstall     remove it again (add --purge to drop settings)
#
# Double-clicking this in Dolphin, Nautilus or Thunar runs it with no terminal
# attached, so everything it prints goes nowhere and it looks like nothing
# happened. When that is the case it reopens itself in a terminal window; pass
# --no-terminal to stop that.
#
# Nothing here needs root except an optional player install through your own
# package manager, which is always shown before it runs.
set -eu

APP=gravitone
PREFIX="${PREFIX:-$HOME/.local}"
BIN="$PREFIX/bin"
VENV="${GRAVITONE_VENV:-$HOME/.local/share/gravitone/venv}"
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
PURGE=0
SHORTCUT=ask
PATH_SETUP=ask
REOPEN=1
ASSUME_YES=0
while [ $# -gt 0 ]; do
  case "$1" in
    --with-player) WITH_PLAYER=1 ;;
    --uninstall) UNINSTALL=1 ;;
    --purge) PURGE=1 ;;
    --shortcut) SHORTCUT=yes ;;
    --no-shortcut) SHORTCUT=no ;;
    --path) PATH_SETUP=yes ;;
    --no-path) PATH_SETUP=no ;;
    --no-terminal) REOPEN=0 ;;
    --yes|-y) ASSUME_YES=1 ;;
    --prefix) shift; [ $# -gt 0 ] || die "--prefix needs a folder"; PREFIX="$1" ;;
    --prefix=*) PREFIX="${1#--prefix=}" ;;
    -h|--help) sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
  shift
done
BIN="$PREFIX/bin"

# Answer a question, or take the default without asking. --yes and a missing
# terminal are the same thing: nobody is there to answer.
ask() {
  # ask "prompt" "default"  ->  prints y or n
  if [ "$ASSUME_YES" -eq 1 ] || [ ! -t 0 ]; then printf '%s' "$2"; return; fi
  printf '%s ' "$1" >&2
  read -r reply || reply=""
  case "$reply" in
    [yY]*) printf 'y' ;;
    [nN]*) printf 'n' ;;
    *) printf '%s' "$2" ;;
  esac
}

# If anything below falls over, say where rather than leaving a half-install
# and a bare shell error.
on_failure() {
  status=$?
  [ "$status" -eq 0 ] && exit 0
  warn "the install stopped early (exit $status)."
  say "Nothing outside $PREFIX and $VENV was touched."
  say "If you cannot tell why, run it again with:  sh -x $0"
  exit "$status"
}
trap on_failure EXIT

# -- run somewhere the output can be read ----------------------------------
# A file manager launches this with no terminal: stdout is not a tty and
# nothing is visible. Reopen in a terminal emulator so the install can be
# watched (and so the questions below can be answered).
open_in_terminal() {
  [ "$REOPEN" -eq 1 ] || return 1
  [ -t 1 ] && return 1
  [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ] || return 1
  [ -n "${GRAVITONE_REOPENED:-}" ] && return 1

  for term in konsole gnome-terminal xfce4-terminal ptyxis kitty alacritty foot xterm x-terminal-emulator; do
    command -v "$term" >/dev/null 2>&1 || continue
    GRAVITONE_REOPENED=1
    export GRAVITONE_REOPENED
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
  rm -f "$BIN/$APP" "$BIN/grav"
  rm -rf "$VENV"
  command -v pipx >/dev/null 2>&1 && pipx uninstall gravitone >/dev/null 2>&1 || true
  rm -f "$HOME/.local/share/applications/gravitone.desktop"
  rm -f "$HOME/.local/share/icons/hicolor/scalable/apps/gravitone.svg"
  rm -f "$HOME/Desktop/gravitone.desktop"
  DESKTOP_DIR=$( (command -v xdg-user-dir >/dev/null 2>&1 && xdg-user-dir DESKTOP) || echo "$HOME/Desktop")
  rm -f "$DESKTOP_DIR/gravitone.desktop"
  if [ "$PURGE" -eq 1 ]; then
    rm -rf "${XDG_CONFIG_HOME:-$HOME/.config}/gravitone"
    say "Removed, settings and all. Your music was never touched."
  else
    say "Removed. Your library and settings were left alone:"
    say "  ${DIM}~/.local/share/custom soundtrack${OFF}   ${DIM}~/.config/gravitone${OFF}"
    say "  ${DIM}(--purge also removes the settings)${OFF}"
  fi
  trap - EXIT
  exit 0
fi

PYTHON=$(find_python) || die "Python 3.9+ not found. Install python3 and run this again."
step "Using $PYTHON ($("$PYTHON" -c 'import platform; print(platform.python_version())'))"

# What is already here, so an upgrade says so instead of looking like a
# first install that mysteriously kept your music.
BEFORE=""
if [ -x "$BIN/$APP" ]; then
  BEFORE=$("$BIN/$APP" --version 2>/dev/null | awk '{print $NF}') || BEFORE=""
fi
WANT=$(sed -n 's/^version *= *"\(.*\)"/\1/p' "$SRC/pyproject.toml" 2>/dev/null | head -1)
if [ -n "$BEFORE" ]; then
  if [ "$BEFORE" = "$WANT" ]; then
    step "Reinstalling $APP $WANT"
  else
    step "Upgrading $APP $BEFORE -> ${WANT:-newer}"
  fi
fi

# -- install ---------------------------------------------------------------
mkdir -p "$BIN"
if command -v pipx >/dev/null 2>&1; then
  step "Installing with pipx"
  pipx install --force "$SRC"
else
  step "Installing into $VENV"
  "$PYTHON" -m venv "$VENV" 2>/dev/null ||
    die "could not create a virtualenv. On Debian/Ubuntu: sudo apt install python3-venv"
  "$VENV/bin/python" -m pip install --quiet --upgrade pip ||
    warn "could not update pip in the virtualenv; carrying on with the one it has"
  "$VENV/bin/python" -m pip install --quiet "$SRC" ||
    die "pip could not install $APP. Run it again without --quiet to see why:
    $VENV/bin/python -m pip install '$SRC'"
  ln -sf "$VENV/bin/$APP" "$BIN/$APP"
  ln -sf "$VENV/bin/grav" "$BIN/grav" 2>/dev/null || true
  say "Linked $BIN/$APP  (and $BIN/grav)"
fi

# -- did it actually work? -------------------------------------------------
# An installer that reports success without ever running the thing is just a
# hopeful copy.
INSTALLED=""
if [ -x "$BIN/$APP" ]; then
  INSTALLED=$("$BIN/$APP" --version 2>/dev/null) || INSTALLED=""
elif command -v "$APP" >/dev/null 2>&1; then
  INSTALLED=$("$APP" --version 2>/dev/null) || INSTALLED=""
fi
[ -n "$INSTALLED" ] || die "installed, but '$APP --version' did not run. Something is wrong with the virtualenv at $VENV"
step "Installed $INSTALLED"

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
    say "No audio player found. $APP needs one of ffmpeg / mpv / vlc."
    say "  ${DIM}$CMD${OFF}"
    if [ "$(ask "Run it now? [y/N]" n)" = y ]; then
      sh -c "$CMD" || warn "that did not work - install a player yourself and try again"
    else
      say "Skipped - run it yourself later, then: $APP doctor"
    fi
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
  # The icon has to be on the icon path before the menu entry points at it,
  # or the launcher falls back to a generic note.
  ICONS="$HOME/.local/share/icons/hicolor/scalable/apps"
  mkdir -p "$ICONS"
  cp "$SRC/docs/icon.svg" "$ICONS/gravitone.svg" 2>/dev/null || true
  command -v gtk-update-icon-cache >/dev/null 2>&1 && \
    gtk-update-icon-cache -q -t -f "$HOME/.local/share/icons/hicolor" 2>/dev/null || true
  sed "s|Exec=gravitone ui|Exec=$BIN/$APP ui|" "$SRC/packaging/gravitone.desktop" > "$APPS/gravitone.desktop"
  chmod +x "$APPS/gravitone.desktop"
  command -v update-desktop-database >/dev/null 2>&1 && \
    update-desktop-database "$APPS" >/dev/null 2>&1 || true
  step "Added gravitone to your application menu"

  DESKTOP_DIR=$( (command -v xdg-user-dir >/dev/null 2>&1 && xdg-user-dir DESKTOP) || echo "$HOME/Desktop")
  if [ -d "$DESKTOP_DIR" ]; then
    cp "$APPS/gravitone.desktop" "$DESKTOP_DIR/gravitone.desktop"
    chmod +x "$DESKTOP_DIR/gravitone.desktop"
    # KDE refuses to run a desktop file it does not trust; this is the flag
    # Plasma sets when you click "Trust this executable".
    command -v kwriteconfig5 >/dev/null 2>&1 && \
      kwriteconfig5 --file "$DESKTOP_DIR/gravitone.desktop" --group "Desktop Entry" \
        --key "X-KDE-AuthorizeExecute" "true" 2>/dev/null || true
    step "Put a shortcut on your desktop"
  fi
}

if [ "$(uname -s)" = "Linux" ] && [ -f "$SRC/packaging/gravitone.desktop" ]; then
  case "$SHORTCUT" in
    yes) install_shortcuts ;;
    no) ;;
    *)
      say ""
      if [ "$(ask "Add $APP to your application menu and desktop? [Y/n]" y)" = y ]; then
        install_shortcuts
      else
        say "Skipped."
      fi
      ;;
  esac
fi

# -- PATH ------------------------------------------------------------------
# Without this, `gravitone` installs perfectly and then "command not found".
profile_for_shell() {
  case "$(basename "${SHELL:-/bin/sh}")" in
    zsh) echo "${ZDOTDIR:-$HOME}/.zshrc" ;;
    bash)
      # Fedora, Debian and friends source .bashrc for interactive shells.
      if [ -f "$HOME/.bashrc" ] || [ ! -f "$HOME/.bash_profile" ]; then
        echo "$HOME/.bashrc"
      else
        echo "$HOME/.bash_profile"
      fi
      ;;
    fish) echo "$HOME/.config/fish/config.fish" ;;
    ksh) echo "$HOME/.kshrc" ;;
    *) echo "$HOME/.profile" ;;
  esac
}

path_line_for() {
  # Write it relative to $HOME when we can, so the profile stays portable.
  case "$BIN" in
    "$HOME"/*) WHERE="\$HOME${BIN#"$HOME"}" ;;
    *) WHERE="$BIN" ;;
  esac
  case "$1" in
    *fish*) echo "fish_add_path \"$WHERE\"" ;;
    *) echo "export PATH=\"$WHERE:\$PATH\"" ;;
  esac
}

add_to_path() {
  PROFILE=$(profile_for_shell)
  LINE=$(path_line_for "$PROFILE")
  if [ -f "$PROFILE" ] && grep -Fq "$BIN" "$PROFILE" 2>/dev/null; then
    step "$(basename "$PROFILE") already mentions $BIN - open a new terminal"
    return 0
  fi
  mkdir -p "$(dirname "$PROFILE")"
  {
    printf '\n# added by the gravitone installer\n'
    printf '%s\n' "$LINE"
  } >> "$PROFILE"
  step "Added $BIN to your PATH in $PROFILE"
  case "$PROFILE" in
    *fish*) RELOAD="source $PROFILE" ;;   # fish 4 dropped the `.` alias
    *) RELOAD=". $PROFILE" ;;
  esac
  say "    ${DIM}open a new terminal, or run: $RELOAD${OFF}"
}

ON_PATH=0
case ":$PATH:" in
  *":$BIN:"*) ON_PATH=1 ;;
  *)
    say ""
    warn "$BIN is not on your PATH, so typing 'gravitone' will not find it yet."
    if [ "$PATH_SETUP" = "no" ]; then
      say "Add this to $(profile_for_shell) yourself:"
      say "    $(path_line_for "$(profile_for_shell)")"
    elif [ "$PATH_SETUP" = "yes" ] || [ ! -t 0 ] || [ "$ASSUME_YES" -eq 1 ]; then
      add_to_path
    elif [ "$(ask "Add it to $(basename "$(profile_for_shell)") for you? [Y/n]" y)" = y ]; then
      add_to_path
    else
      say "Left alone. The line you need:"
      say "    $(path_line_for "$(profile_for_shell)")"
    fi
    ;;
esac

# -- a last look over the whole thing --------------------------------------
say ""
step "Checking the setup"
"$BIN/$APP" doctor 2>/dev/null | sed -n '/^players found/p;/^tags & lengths/p;/^file chooser/p' |
  while IFS= read -r line; do say "    ${DIM}$line${OFF}"; done

say ""
step "Done"
if [ "$(ask "Open the control panel now? [Y/n]" y)" = y ]; then
  {
      # Detached, or it dies with this terminal when the window closes -
      # which looked exactly like "the control panel never opened".
      LOG="${XDG_STATE_HOME:-$HOME/.local/state}/gravitone"
      mkdir -p "$LOG"
      if command -v setsid >/dev/null 2>&1; then
        setsid "$BIN/$APP" ui >"$LOG/ui.log" 2>&1 < /dev/null &
      else
        nohup "$BIN/$APP" ui >"$LOG/ui.log" 2>&1 < /dev/null &
      fi
      sleep 2
      say "  started in the background - log: ${DIM}$LOG/ui.log${OFF}"
  }
fi
trap - EXIT
say ""
if [ "$ON_PATH" -eq 1 ]; then
  RUN=$APP
else
  RUN="$BIN/$APP"      # works in this terminal, before any profile is reloaded
fi
say "  ${BOLD}$RUN ui${OFF}                     open the control panel"
say "  ${BOLD}$RUN ui --stop${OFF}              stop it again"
say "  ${BOLD}$RUN link ~/Music/album${OFF}     add music (symlinks, no copies)"
say "  ${BOLD}$RUN play${OFF}                   play from the terminal"
