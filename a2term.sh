#!/bin/bash
#
# a2term.sh - Apple IIe terminal session via A2Pico
#
# Connects an Apple IIe running Softerm 2 (with super-serial-card
# firmware on the A2Pico) as a Linux terminal over USB serial.
#
# Usage:
#   ./a2term.sh              # filtered mode (default, handles full-screen apps)
#   ./a2term.sh -r           # raw/unfiltered pipe mode (simple shell only)
#   ./a2term.sh -r -p        # raw/unfiltered PTY mode
#   ./a2term.sh -c           # clean/isolated mode (no .bashrc or .config)
#   ./a2term.sh /dev/ttyACM1 # specify device
#
# The default (filtered) mode runs all output through a2filter.py, which
# translates UTF-8 box-drawing to VT100 special graphics, strips ANSI
# colors, and maps Unicode to ASCII.  This is required for modern TUI
# programs (neovim, opencode, claude-code, etc.) to render correctly.
#
# Requires: socat, python3

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FILTER="$SCRIPT_DIR/a2filter.py"

TERM_TYPE=vt100
RAW_MODE=0
USE_PTY=0
FILTER_ARGS=()
CLEAN_MODE=0

# --- Parse flags ---
while getopts "rpasc" opt; do
    case $opt in
        r) RAW_MODE=1 ;;
        p) USE_PTY=1 ;;
        a) FILTER_ARGS+=(--ascii-only) ;;
        s) FILTER_ARGS+=(--stats) ;;
        c) CLEAN_MODE=1 ;;
        *) echo "Usage: $0 [-r] [-p] [-a] [-s] [-c] [device]" >&2
           echo "  -r  Raw mode (no filter, original behavior)" >&2
           echo "  -p  PTY mode without filter (use with -r)" >&2
           echo "  -a  ASCII-only box drawing (no VT100 graphics)" >&2
           echo "  -s  Print filter statistics on disconnect" >&2
           echo "  -c  Clean/isolated shell (no .bashrc or .config)" >&2
           exit 1 ;;
    esac
done
shift $((OPTIND - 1))

# --- Find A2Pico serial device ---
find_device() {
    local devices=()
    # Linux: /dev/ttyACM*
    # macOS: /dev/cu.usbmodem* (preferred -- doesn't block on carrier detect)
    for pattern in /dev/ttyACM* /dev/cu.usbmodem*; do
        for d in $pattern; do
            [[ -c "$d" ]] && devices+=("$d")
        done
    done
    if (( ${#devices[@]} == 0 )); then
        return 1
    fi
    if (( ${#devices[@]} == 1 )); then
        echo "${devices[0]}"
    else
        local last="${devices[${#devices[@]}-1]}"
        echo "Multiple serial devices found:" >&2
        for d in "${devices[@]}"; do
            echo "  $d" >&2
        done
        echo "Using $last" >&2
        echo "$last"
    fi
}

# --- Preflight checks ---
if ! command -v socat >/dev/null 2>&1; then
    echo "error: socat not installed (sudo pacman -S socat)" >&2
    exit 1
fi

if (( ! RAW_MODE )); then
    if [[ ! -x "$FILTER" ]]; then
        echo "error: $FILTER not found (required for filtered mode)" >&2
        echo "  use -r for raw/unfiltered mode" >&2
        exit 1
    fi
    if ! command -v python3 >/dev/null 2>&1; then
        echo "error: python3 not found (required for filtered mode)" >&2
        exit 1
    fi
fi

DEVICE="${1:-}"
if [[ -z "$DEVICE" ]]; then
    DEVICE=$(find_device) || {
        echo "error: no serial device found (tried /dev/ttyACM* and /dev/cu.usbmodem*) -- is the A2Pico connected?" >&2
        exit 1
    }
fi

if [[ ! -c "$DEVICE" ]]; then
    echo "error: $DEVICE is not a character device" >&2
    exit 1
fi

if lsof "$DEVICE" >/dev/null 2>&1; then
    echo "error: $DEVICE already in use:" >&2
    lsof "$DEVICE"
    exit 1
fi

# --- Clean/isolated mode setup ---
if (( CLEAN_MODE )); then
    CLEAN_HOME=$(mktemp -d)
    trap 'rm -rf "$CLEAN_HOME"' EXIT
    cat > "$CLEAN_HOME/.bashrc" << 'RCEOF'
# Minimal isolated environment for Apple IIe terminal
PS1='\w\$ '
export TERM="${TERM:-vt100}"
export LANG="${LANG:-en_US.UTF-8}"
RCEOF
    export HOME="$CLEAN_HOME"
    export SHELL=/bin/bash
    export XDG_CONFIG_HOME="$CLEAN_HOME/.config"
    export XDG_DATA_HOME="$CLEAN_HOME/.local/share"
fi

# --- Helper: run socat (skip exec in clean mode so EXIT trap can clean up) ---
run_socat() {
    if (( CLEAN_MODE )); then
        socat "$@"
    else
        exec socat "$@"
    fi
}

if (( RAW_MODE )); then
    if (( USE_PTY )); then
        echo "A2Pico terminal: $DEVICE (raw PTY mode, no filter)"
    else
        echo "A2Pico terminal: $DEVICE (raw pipe mode, no filter)"
    fi
else
    echo "A2Pico terminal: $DEVICE (filtered mode)"
    if [[ ${#FILTER_ARGS[@]} -gt 0 ]]; then
        echo "  filter options: ${FILTER_ARGS[*]}"
    fi
fi
if (( CLEAN_MODE )); then
    echo "  clean mode: HOME=$CLEAN_HOME"
fi
echo "Ctrl-C here to disconnect."
echo ""

# --- Launch session ---
if (( RAW_MODE )); then
    # --- Raw mode: original behavior, no filter ---
    if (( USE_PTY )); then
        # PTY mode: shell gets a real terminal.
        run_socat \
            "FILE:$DEVICE,raw,echo=0,clocal=1,hupcl=0" \
            "EXEC:env TERM=$TERM_TYPE /bin/bash -i,pty,sane,setsid,ctty,stderr"
    else
        # Pipe mode: simpler, proven reliable.
        # opost+onlcr translates LF -> CRLF on output.
        # icrnl translates Apple's CR -> LF on input.
        run_socat \
            "FILE:$DEVICE,raw,echo=0,clocal=1,hupcl=0,opost=1,onlcr=1,icrnl=1" \
            "EXEC:env TERM=$TERM_TYPE PS1='\\w\\$ ' /bin/bash -i,stderr"
    fi
else
    # --- Filtered mode: a2filter handles the PTY ---
    # a2filter.py creates its own PTY for the child process with
    # standard line discipline (echo, icrnl, onlcr).  The serial
    # device is fully raw -- no CR/LF processing on the socat side,
    # because the inner PTY already converts LF -> CRLF on output
    # and CR -> LF on input.
    #
    # Data flow:
    #   Apple IIe  <-->  serial (raw)  <-->  socat  <-->  socat PTY
    #     <-->  a2filter (stdin raw)  <-->  inner PTY (sane)  <-->  bash
    #
    # Output: bash LF -> inner PTY onlcr -> CRLF -> a2filter -> serial -> Apple
    # Input:  Apple CR -> serial -> a2filter -> inner PTY icrnl -> LF -> bash
    FILTER_CMD="$FILTER"
    if [[ ${#FILTER_ARGS[@]} -gt 0 ]]; then
        FILTER_CMD="$FILTER ${FILTER_ARGS[*]}"
    fi
    run_socat \
        "FILE:$DEVICE,raw,echo=0,clocal=1,hupcl=0" \
        "EXEC:$FILTER_CMD -- /bin/bash -i,pty,setsid,ctty,stderr"
fi
