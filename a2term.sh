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

# --- Parse flags ---
while getopts "rpas" opt; do
    case $opt in
        r) RAW_MODE=1 ;;
        p) USE_PTY=1 ;;
        a) FILTER_ARGS+=(--ascii-only) ;;
        s) FILTER_ARGS+=(--stats) ;;
        *) echo "Usage: $0 [-r] [-p] [-a] [-s] [device]" >&2
           echo "  -r  Raw mode (no filter, original behavior)" >&2
           echo "  -p  PTY mode without filter (use with -r)" >&2
           echo "  -a  ASCII-only box drawing (no VT100 graphics)" >&2
           echo "  -s  Print filter statistics on disconnect" >&2
           exit 1 ;;
    esac
done
shift $((OPTIND - 1))

# --- Find A2Pico serial device ---
find_device() {
    local devices=(/dev/ttyACM*)
    if [[ ! -c "${devices[0]:-}" ]]; then
        return 1
    fi
    if (( ${#devices[@]} == 1 )); then
        echo "${devices[0]}"
    else
        echo "Multiple serial devices found:" >&2
        for d in "${devices[@]}"; do
            echo "  $d" >&2
        done
        # Use the last one -- ttyACM0 is often something else
        echo "Using ${devices[-1]}" >&2
        echo "${devices[-1]}"
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
        echo "error: no /dev/ttyACM* found -- is the A2Pico connected?" >&2
        exit 1
    }
fi

if [[ ! -c "$DEVICE" ]]; then
    echo "error: $DEVICE is not a character device" >&2
    exit 1
fi

if fuser "$DEVICE" >/dev/null 2>&1; then
    echo "error: $DEVICE already in use:" >&2
    fuser -v "$DEVICE" 2>&1
    exit 1
fi

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
echo "Ctrl-C here to disconnect."
echo ""

# --- Launch session ---
if (( RAW_MODE )); then
    # --- Raw mode: original behavior, no filter ---
    if (( USE_PTY )); then
        # PTY mode: shell gets a real terminal.
        exec socat \
            "FILE:$DEVICE,raw,echo=0,clocal=1,hupcl=0" \
            "EXEC:env TERM=$TERM_TYPE /bin/bash -i,pty,sane,setsid,ctty,stderr"
    else
        # Pipe mode: simpler, proven reliable.
        # opost+onlcr translates LF -> CRLF on output.
        # icrnl translates Apple's CR -> LF on input.
        exec socat \
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
    exec socat \
        "FILE:$DEVICE,raw,echo=0,clocal=1,hupcl=0" \
        "EXEC:$FILTER_CMD -- /bin/bash -i,pty,setsid,ctty,stderr"
fi
