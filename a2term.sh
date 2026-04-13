#!/bin/bash
#
# a2term.sh - Apple IIe terminal session via A2Pico
#
# Connects an Apple IIe running Softerm 2 (with super-serial-card
# firmware on the A2Pico) as a Linux terminal over USB serial.
#
# Usage:
#   ./a2term.sh              # auto-detect device, pipe mode
#   ./a2term.sh -p           # PTY mode (needed for full-screen apps)
#   ./a2term.sh /dev/ttyACM1 # specify device
#   ./a2term.sh -p /dev/ttyACM1
#
# Requires: socat

set -euo pipefail

TERM_TYPE=vt100
USE_PTY=0

# --- Parse flags ---
while getopts "p" opt; do
    case $opt in
        p) USE_PTY=1 ;;
        *) echo "Usage: $0 [-p] [device]" >&2; exit 1 ;;
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

if (( USE_PTY )); then
    echo "A2Pico terminal: $DEVICE (PTY mode)"
else
    echo "A2Pico terminal: $DEVICE (pipe mode)"
fi
echo "Ctrl-C here to disconnect."
echo ""

# --- Launch session ---
if (( USE_PTY )); then
    # PTY mode: shell gets a real terminal. Needed for full-screen apps
    # like vim/neovim. The PTY handles CR/LF translation and echo.
    exec socat \
        "FILE:$DEVICE,raw,echo=0,clocal=1,hupcl=0" \
        "EXEC:env TERM=$TERM_TYPE /bin/bash -i,pty,sane,setsid,ctty,stderr"
else
    # Pipe mode: simpler, proven reliable. Good for shell commands.
    # opost+onlcr translates LF -> CRLF on output.
    # icrnl translates Apple's CR -> LF on input.
    exec socat \
        "FILE:$DEVICE,raw,echo=0,clocal=1,hupcl=0,opost=1,onlcr=1,icrnl=1" \
        "EXEC:env TERM=$TERM_TYPE PS1='\\w\\$ ' /bin/bash -i,stderr"
fi
