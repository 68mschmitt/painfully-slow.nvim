#!/usr/bin/env python3
"""
a2filter - Apple IIe VT100 terminal filter

Wraps any program in a PTY and translates its output to strict VT100 /
7-bit ASCII, suitable for an Apple IIe running Softerm 2.

Handles:
  - UTF-8 box-drawing  → VT100 special graphics character set (ESC(0)
  - ANSI 256/truecolor  → stripped (keeps bold/underline/reverse)
  - Unicode symbols      → closest ASCII approximation
  - Accented characters  → base ASCII via Unicode decomposition
  - Emoji / unknown      → '?'

Usage:
  ./a2filter.py bash                  # run bash through the filter
  ./a2filter.py nvim file.txt         # run nvim through the filter
  ./a2filter.py opencode              # run opencode through the filter
  ./a2filter.py --test                # show a test pattern
  ./a2filter.py --pipe                # stdin→stdout filter (for socat)

Options:
  --ascii-only   Use ASCII +|-/ for box-drawing instead of VT100 graphics
  --no-sgr       Strip ALL SGR sequences (including bold/underline)
  --log FILE     Write substitution log to FILE
  --stats        Print substitution statistics on exit
  --cols N       Terminal width  (default: 80)
  --rows N       Terminal height (default: 24)
"""

import argparse
import errno
import fcntl
import os
import pty
import select
import signal
import struct
import sys
import termios
import time
import tty
import unicodedata

# ═══════════════════════════════════════════════════════════════════
# Character Translation Tables
# ═══════════════════════════════════════════════════════════════════

# VT100 Special Graphics bytes (used after ESC(0):
#   j=┘  k=┐  l=┌  m=└  n=┼  q=─  t=├  u=┤  v=┴  w=┬  x=│
_H  = ord('q')   # horizontal
_V  = ord('x')   # vertical
_TL = ord('l')   # top-left corner
_TR = ord('k')   # top-right corner
_BL = ord('m')   # bottom-left corner
_BR = ord('j')   # bottom-right corner
_RT = ord('t')   # right tee  (├)
_LT = ord('u')   # left tee   (┤)
_DT = ord('w')   # down tee   (┬)
_UT = ord('v')   # up tee     (┴)
_CR = ord('n')   # cross      (┼)

# Ranges: (start_codepoint, end_codepoint_inclusive, vt100_gfx_byte)
_BOX_RANGES = [
    # Horizontal / vertical lines (light, heavy, dashed variants)
    (0x2500, 0x2501, _H),   # ─ ━
    (0x2502, 0x2503, _V),   # │ ┃
    (0x2504, 0x2505, _H),   # ┄ ┅
    (0x2506, 0x2507, _V),   # ┆ ┇
    (0x2508, 0x2509, _H),   # ┈ ┉
    (0x250A, 0x250B, _V),   # ┊ ┋
    # Corners
    (0x250C, 0x250F, _TL),  # ┌ ┍ ┎ ┏
    (0x2510, 0x2513, _TR),  # ┐ ┑ ┒ ┓
    (0x2514, 0x2517, _BL),  # └ ┕ ┖ ┗
    (0x2518, 0x251B, _BR),  # ┘ ┙ ┚ ┛
    # Tees
    (0x251C, 0x2523, _RT),  # ├ and variants
    (0x2524, 0x252B, _LT),  # ┤ and variants
    (0x252C, 0x2533, _DT),  # ┬ and variants
    (0x2534, 0x253B, _UT),  # ┴ and variants
    # Crosses
    (0x253C, 0x254B, _CR),  # ┼ and variants
    # More dashed
    (0x254C, 0x254D, _H),   # ╌ ╍
    (0x254E, 0x254F, _V),   # ╎ ╏
    # Double-line
    (0x2550, 0x2550, _H),   # ═
    (0x2551, 0x2551, _V),   # ║
    (0x2552, 0x2554, _TL),  # ╒ ╓ ╔
    (0x2555, 0x2557, _TR),  # ╕ ╖ ╗
    (0x2558, 0x255A, _BL),  # ╘ ╙ ╚
    (0x255B, 0x255D, _BR),  # ╛ ╜ ╝
    (0x255E, 0x2560, _RT),  # ╞ ╟ ╠
    (0x2561, 0x2563, _LT),  # ╡ ╢ ╣
    (0x2564, 0x2566, _DT),  # ╤ ╥ ╦
    (0x2567, 0x2569, _UT),  # ╧ ╨ ╩
    (0x256A, 0x256C, _CR),  # ╪ ╫ ╬
    # Rounded corners
    (0x256D, 0x256D, _TL),  # ╭
    (0x256E, 0x256E, _TR),  # ╮
    (0x256F, 0x256F, _BR),  # ╯
    (0x2570, 0x2570, _BL),  # ╰
    # Fragments
    (0x2574, 0x2574, _H),   # ╴ light left
    (0x2575, 0x2575, _V),   # ╵ light up
    (0x2576, 0x2576, _H),   # ╶ light right
    (0x2577, 0x2577, _V),   # ╷ light down
    (0x2578, 0x2578, _H),   # ╸ heavy left
    (0x2579, 0x2579, _V),   # ╹ heavy up
    (0x257A, 0x257A, _H),   # ╺ heavy right
    (0x257B, 0x257B, _V),   # ╻ heavy down
    (0x257C, 0x257C, _H),   # ╼ light-left heavy-right
    (0x257D, 0x257D, _V),   # ╽ light-up heavy-down
    (0x257E, 0x257E, _H),   # ╾ heavy-left light-right
    (0x257F, 0x257F, _V),   # ╿ heavy-up light-down
]

# Build VT100 special graphics map: char → bytes
VT100_GFX = {}
for _start, _end, _gfx in _BOX_RANGES:
    for _cp in range(_start, _end + 1):
        VT100_GFX[chr(_cp)] = bytes([_gfx])

# Build ASCII fallback map for box-drawing: char → str
_GFX_TO_ASCII = {
    _H: '-', _V: '|', _TL: '+', _TR: '+', _BL: '+', _BR: '+',
    _RT: '+', _LT: '+', _DT: '+', _UT: '+', _CR: '+',
}
ASCII_BOX = {}
for _start, _end, _gfx in _BOX_RANGES:
    for _cp in range(_start, _end + 1):
        ASCII_BOX[chr(_cp)] = _GFX_TO_ASCII[_gfx]
# Diagonals (no VT100 graphics equivalent)
ASCII_BOX['\u2571'] = '/'
ASCII_BOX['\u2572'] = '\\'
ASCII_BOX['\u2573'] = 'X'

# General Unicode → ASCII substitution
ASCII_SUB = {
    # Block elements (U+2580-U+259F)
    '\u2580': '#',  '\u2581': '_',  '\u2582': '_',  '\u2583': '_',
    '\u2584': '#',  '\u2585': '#',  '\u2586': '#',  '\u2587': '#',
    '\u2588': '#',  '\u2589': '#',  '\u258A': '#',  '\u258B': '#',
    '\u258C': '|',  '\u258D': '|',  '\u258E': '|',  '\u258F': '|',
    '\u2590': '|',
    '\u2591': '.',  '\u2592': ':',  '\u2593': '#',
    '\u2594': '-',  '\u2595': '|',
    # Arrows
    '\u2190': '<',  '\u2191': '^',  '\u2192': '>',  '\u2193': 'v',
    '\u2194': '<>', '\u2195': '|',
    '\u21d0': '<=', '\u21d2': '=>', '\u21d1': '^',  '\u21d3': 'v',
    '\u21b5': ' ',  '\u21b3': '>', '\u21aa': '>',  '\u21a9': '<',
    # Bullets and shapes
    '\u2022': '*',  '\u2023': '>',  '\u2043': '-',
    '\u25cf': '*',  '\u25cb': 'o',  '\u25ef': 'O',
    '\u25a0': '#',  '\u25a1': '[]', '\u25a2': '[]',
    '\u25b2': '^',  '\u25b6': '>',  '\u25bc': 'v',  '\u25c0': '<',
    '\u25c6': '*',  '\u25c7': 'o',  '\u25c8': '*',
    '\u25aa': '.',  '\u25ab': '.',
    '\u2b24': '*',  '\u2b55': 'o',
    # Checkmarks / ballots
    '\u2713': '+',  '\u2714': '+',  '\u2715': 'x',
    '\u2717': 'x',  '\u2718': 'x',  '\u2716': 'x',
    '\u2610': '[ ]', '\u2611': '[+]', '\u2612': '[x]',
    # Typography
    '\u2026': '...', '\u22ef': '...',
    '\u2014': '--',  '\u2013': '-',  '\u2012': '-',  '\u2015': '--',
    '\u201c': '"',   '\u201d': '"',  '\u201e': '"',
    '\u2018': "'",   '\u2019': "'",  '\u201a': ',',
    '\u00ab': '<<',  '\u00bb': '>>',
    '\u2039': '<',   '\u203a': '>',
    '\u2033': '"',   '\u2032': "'",
    # Math
    '\u00b1': '+/-', '\u00d7': 'x',  '\u00f7': '/',
    '\u2248': '~=',  '\u2260': '!=', '\u2264': '<=', '\u2265': '>=',
    '\u221e': 'inf', '\u2211': 'E',  '\u220f': 'Pi', '\u221a': 'V',
    '\u2261': '===', '\u2227': '&&', '\u2228': '||', '\u00ac': '!',
    '\u2234': ':.:', '\u2235': '.:.',
    # Copyright / legal
    '\u00a9': '(c)', '\u00ae': '(R)', '\u2122': '(TM)',
    # Misc symbols
    '\u00b0': 'o',   '\u00b7': '.',  '\u00a7': 'S',  '\u00b6': 'P',
    '\u2020': '+',   '\u2021': '++', '\u2605': '*',  '\u2606': '*',
    '\u266a': '#',   '\u266b': '#',
    '\u26a0': '!',   '\u26a1': '!',
    '\u2328': '[K]',
    # Whitespace normalization
    '\u00a0': ' ',   '\u2002': ' ',  '\u2003': ' ',  '\u2004': ' ',
    '\u2005': ' ',   '\u2006': ' ',  '\u2007': ' ',  '\u2008': ' ',
    '\u2009': ' ',   '\u200a': ' ',
    # Zero-width (drop)
    '\u200b': '',    '\u200c': '',   '\u200d': '',   '\ufeff': '',
    '\u200e': '',    '\u200f': '',
    # Braille spinner frames (common in TUI progress indicators)
    '\u2800': ' ',
    '\u280b': '-',  '\u2819': '\\', '\u2839': '|',  '\u2838': '/',
    '\u283c': '-',  '\u2834': '\\', '\u2826': '|',  '\u2827': '/',
    '\u2807': '-',  '\u280f': '\\',
    '\u2846': '|',  '\u2844': '|',  '\u2860': '.',  '\u2870': '.',
    '\u2818': '.',
    # Powerline / nerd-font glyphs (common in shell prompts, status bars)
    '\ue0a0': '@',  # branch symbol
    '\ue0a1': 'LN', # line number
    '\ue0a2': '!',  # padlock
    '\ue0b0': '>',  # right triangle
    '\ue0b1': '>',  # right triangle thin
    '\ue0b2': '<',  # left triangle
    '\ue0b3': '<',  # left triangle thin
}

# SGR parameters that VT100 actually supports
VT100_SGR = frozenset({0, 1, 4, 5, 7, 22, 24, 25, 27})


# ═══════════════════════════════════════════════════════════════════
# VT100 Output Filter
# ═══════════════════════════════════════════════════════════════════

class VT100Filter:
    """State machine that parses terminal output and translates it to
    strict VT100 / 7-bit ASCII."""

    # Parser states
    NORMAL  = 0
    ESC     = 1   # saw ESC
    CSI     = 2   # saw ESC [
    OSC     = 3   # saw ESC ]  (strip entirely)
    OSC_ESC = 4   # saw ESC inside OSC (looking for \)
    CHARSET = 5   # saw ESC ( or ESC )
    DCS     = 6   # saw ESC P  (strip entirely)
    DCS_ESC = 7   # saw ESC inside DCS
    UTF8    = 8   # collecting UTF-8 continuation bytes

    def __init__(self, ascii_only=False, strip_all_sgr=False, log_file=None):
        self.ascii_only = ascii_only
        self.strip_all_sgr = strip_all_sgr
        self.log_file = log_file
        # Parser state
        self.state = self.NORMAL
        self.buf = bytearray()
        self.utf8_buf = bytearray()
        self.utf8_needed = 0
        # VT100 graphics mode tracking
        self.in_gfx = False        # filter's own gfx mode switch
        self.program_gfx = False   # program sent ESC(0
        # Statistics
        self.stats = {
            'total': 0, 'ascii': 0, 'vt100_gfx': 0,
            'ascii_sub': 0, 'decomposed': 0, 'unknown': 0,
            'sgr_total': 0, 'sgr_kept': 0, 'sgr_stripped': 0,
            'osc_stripped': 0,
        }
        self.char_counts = {}      # {char: (count, method, replacement)}

    # ── Public API ────────────────────────────────────────────

    def feed(self, data):
        """Process a chunk of bytes. Returns filtered bytes."""
        out = bytearray()
        for b in data:
            out.extend(self._byte(b))
        return bytes(out)

    def flush(self):
        """Flush pending state (call when stream ends)."""
        out = bytearray()
        if self.in_gfx:
            out.extend(b'\x1b(B')
            self.in_gfx = False
        # Flush any incomplete UTF-8
        if self.state == self.UTF8:
            out.extend(b'?')
            self.state = self.NORMAL
        # Flush any incomplete escape sequence
        if self.state in (self.ESC, self.CSI, self.CHARSET):
            self.state = self.NORMAL
        return bytes(out)

    def format_stats(self):
        """Return a human-readable statistics summary."""
        s = self.stats
        lines = [
            '',
            '=== a2filter statistics ===',
            f'  Characters processed: {s["total"]}',
        ]
        if s['total'] > 0:
            for key, label in [
                ('ascii', 'ASCII pass-through'),
                ('vt100_gfx', 'VT100 graphics'),
                ('ascii_sub', 'ASCII substitution'),
                ('decomposed', 'Unicode decomposed'),
                ('unknown', 'Unknown (? replacement)'),
            ]:
                count = s[key]
                pct = 100.0 * count / s['total'] if s['total'] else 0
                lines.append(f'  {label}: {count} ({pct:.1f}%)')
        lines.append(f'  SGR sequences: {s["sgr_total"]} total, '
                     f'{s["sgr_stripped"]} stripped, {s["sgr_kept"]} kept')
        lines.append(f'  OSC sequences stripped: {s["osc_stripped"]}')
        if self.char_counts:
            lines.append('')
            lines.append('  Substituted characters (top 30):')
            top = sorted(self.char_counts.items(),
                        key=lambda x: x[1][0], reverse=True)[:30]
            for char, (count, method, repl) in top:
                cp = ord(char)
                try:
                    name = unicodedata.name(char, f'U+{cp:04X}')
                except ValueError:
                    name = f'U+{cp:04X}'
                lines.append(f'    U+{cp:04X} {char} {name}')
                lines.append(f'           -> {repl!r} ({method}) x{count}')
        lines.append('')
        return '\n'.join(lines)

    # ── State machine ─────────────────────────────────────────

    def _byte(self, b):
        if self.state == self.NORMAL:
            return self._st_normal(b)
        elif self.state == self.ESC:
            return self._st_esc(b)
        elif self.state == self.CSI:
            return self._st_csi(b)
        elif self.state == self.OSC:
            return self._st_osc(b)
        elif self.state == self.OSC_ESC:
            return self._st_osc_esc(b)
        elif self.state == self.CHARSET:
            return self._st_charset(b)
        elif self.state == self.DCS:
            return self._st_dcs(b)
        elif self.state == self.DCS_ESC:
            return self._st_dcs_esc(b)
        elif self.state == self.UTF8:
            return self._st_utf8(b)
        return bytes([b])

    def _st_normal(self, b):
        if b == 0x1b:
            self.state = self.ESC
            self.buf = bytearray([b])
            return b''
        if b < 0x20 or b == 0x7f:
            # Control chars: pass through (CR, LF, TAB, BS, BEL, etc.)
            return bytes([b])
        if 0x20 <= b <= 0x7e:
            self.stats['total'] += 1
            self.stats['ascii'] += 1
            return bytes([b])
        if b >= 0xc0:
            # UTF-8 leading byte
            self.state = self.UTF8
            self.utf8_buf = bytearray([b])
            if b < 0xe0:
                self.utf8_needed = 1
            elif b < 0xf0:
                self.utf8_needed = 2
            else:
                self.utf8_needed = 3
            return b''
        # Stray continuation byte (0x80-0xBF) outside a sequence
        self.stats['total'] += 1
        self.stats['unknown'] += 1
        return b'?'

    def _st_esc(self, b):
        self.buf.append(b)
        if b == ord('['):
            self.state = self.CSI
            return b''
        if b == ord(']'):
            self.state = self.OSC
            return b''
        if b == ord('P'):
            self.state = self.DCS
            return b''
        if b in (ord('('), ord(')')):
            self.state = self.CHARSET
            return b''
        # ESC + single letter: pass through (cursor save/restore, etc.)
        self.state = self.NORMAL
        return bytes(self.buf)

    def _st_csi(self, b):
        self.buf.append(b)
        # Parameters and intermediates: 0x20-0x3F
        # Final byte: 0x40-0x7E
        if 0x40 <= b <= 0x7e:
            self.state = self.NORMAL
            return self._handle_csi(bytes(self.buf))
        if b < 0x20 or b > 0x7e:
            # Invalid byte in CSI - dump buffer and reprocess
            self.state = self.NORMAL
            return bytes(self.buf)
        return b''

    def _st_osc(self, b):
        self.buf.append(b)
        if b == 0x07:
            # BEL terminates OSC
            self.state = self.NORMAL
            self.stats['osc_stripped'] += 1
            return b''
        if b == 0x1b:
            self.state = self.OSC_ESC
            return b''
        return b''

    def _st_osc_esc(self, b):
        self.buf.append(b)
        if b == ord('\\'):
            # ST (ESC \) terminates OSC
            self.state = self.NORMAL
            self.stats['osc_stripped'] += 1
            return b''
        # Not ST - continue collecting OSC
        self.state = self.OSC
        return b''

    def _st_dcs(self, b):
        self.buf.append(b)
        if b == 0x1b:
            self.state = self.DCS_ESC
            return b''
        return b''

    def _st_dcs_esc(self, b):
        self.buf.append(b)
        if b == ord('\\'):
            self.state = self.NORMAL
            return b''
        self.state = self.DCS
        return b''

    def _st_charset(self, b):
        self.buf.append(b)
        self.state = self.NORMAL
        # Track the program's charset state
        parent = self.buf[1]  # ord('(') or ord(')')
        if parent == ord('('):
            self.program_gfx = (b == ord('0'))
        # Pass through -- the program knows what it's doing
        return bytes(self.buf)

    def _st_utf8(self, b):
        if 0x80 <= b <= 0xbf:
            self.utf8_buf.append(b)
            self.utf8_needed -= 1
            if self.utf8_needed == 0:
                self.state = self.NORMAL
                return self._handle_utf8()
            return b''
        # Bad continuation - emit ? for what we had, reprocess this byte
        self.state = self.NORMAL
        self.stats['total'] += 1
        self.stats['unknown'] += 1
        return b'?' + self._byte(b)

    # ── Sequence handlers ─────────────────────────────────────

    def _handle_csi(self, seq):
        """Handle a complete CSI sequence."""
        final = seq[-1:].decode('ascii', errors='replace')
        if final == 'm':
            return self._handle_sgr(seq)
        # All other CSI sequences: pass through
        # (cursor movement, erase, scroll, DEC private modes, etc.)
        return seq

    def _handle_sgr(self, seq):
        """Filter SGR (Select Graphic Rendition) parameters."""
        self.stats['sgr_total'] += 1
        # Extract params between ESC[ and m
        try:
            params_str = seq[2:-1].decode('ascii')
        except UnicodeDecodeError:
            self.stats['sgr_stripped'] += 1
            return b''

        if self.strip_all_sgr:
            # Keep only reset
            self.stats['sgr_stripped'] += 1
            if not params_str or params_str == '0':
                self.stats['sgr_kept'] += 1
                return b'\x1b[0m'
            return b''

        if not params_str:
            self.stats['sgr_kept'] += 1
            return b'\x1b[m'

        parts = []
        for p in params_str.split(';'):
            try:
                parts.append(int(p))
            except ValueError:
                parts.append(0)

        kept = []
        i = 0
        while i < len(parts):
            p = parts[i]
            if p in VT100_SGR:
                kept.append(p)
                i += 1
            elif p in (38, 48):
                # Extended color: skip sub-parameters
                if i + 1 < len(parts) and parts[i + 1] == 5:
                    i += 3   # 38;5;N
                elif i + 1 < len(parts) and parts[i + 1] == 2:
                    i += 5   # 38;2;R;G;B
                else:
                    i += 2
                continue
            else:
                i += 1
                continue

        if not kept:
            self.stats['sgr_stripped'] += 1
            return b''

        self.stats['sgr_kept'] += 1
        return ('\x1b[' + ';'.join(str(p) for p in kept) + 'm').encode('ascii')

    def _handle_utf8(self):
        """Handle a fully decoded UTF-8 character."""
        try:
            char = self.utf8_buf.decode('utf-8')
        except UnicodeDecodeError:
            self.stats['total'] += 1
            self.stats['unknown'] += 1
            return b'?'

        self.stats['total'] += 1

        # --- VT100 special graphics (box-drawing) ---
        if not self.ascii_only and char in VT100_GFX:
            self.stats['vt100_gfx'] += 1
            repl_bytes = VT100_GFX[char]
            self._count(char, 'vt100_gfx', chr(repl_bytes[0]))
            out = bytearray()
            if not self.in_gfx:
                out.extend(b'\x1b(0')
                self.in_gfx = True
            out.extend(repl_bytes)
            return bytes(out)

        # --- Exit graphics mode if we're in it ---
        out = bytearray()
        if self.in_gfx:
            out.extend(b'\x1b(B')
            self.in_gfx = False

        # --- ASCII box-drawing fallback ---
        if char in ASCII_BOX and self.ascii_only:
            self.stats['ascii_sub'] += 1
            repl = ASCII_BOX[char]
            self._count(char, 'ascii_box', repl)
            out.extend(repl.encode('ascii'))
            return bytes(out)

        # --- General ASCII substitutions ---
        if char in ASCII_SUB:
            self.stats['ascii_sub'] += 1
            repl = ASCII_SUB[char]
            self._count(char, 'ascii_sub', repl)
            out.extend(repl.encode('ascii'))
            return bytes(out)

        # --- Diagonal box-drawing (not in VT100 gfx) ---
        if char in ASCII_BOX:
            self.stats['ascii_sub'] += 1
            repl = ASCII_BOX[char]
            self._count(char, 'ascii_box', repl)
            out.extend(repl.encode('ascii'))
            return bytes(out)

        # --- Unicode NFKD decomposition ---
        nfkd = unicodedata.normalize('NFKD', char)
        base = ''.join(c for c in nfkd if unicodedata.category(c) != 'Mn')
        if base and all(0x20 <= ord(c) <= 0x7e for c in base):
            self.stats['decomposed'] += 1
            self._count(char, 'decomposed', base)
            out.extend(base.encode('ascii'))
            return bytes(out)

        # --- Unknown character ---
        self.stats['unknown'] += 1
        self._count(char, 'unknown', '?')
        out.extend(b'?')
        return bytes(out)

    # ── Helpers ────────────────────────────────────────────────

    def _count(self, char, method, repl):
        """Track per-character substitution counts."""
        if char in self.char_counts:
            count, _, _ = self.char_counts[char]
            self.char_counts[char] = (count + 1, method, repl)
        else:
            self.char_counts[char] = (1, method, repl)
        self._log(char, method, repl)

    def _log(self, char, method, repl):
        """Write a substitution entry to the log file."""
        if self.log_file is None:
            return
        cp = ord(char)
        try:
            name = unicodedata.name(char, f'U+{cp:04X}')
        except ValueError:
            name = f'U+{cp:04X}'
        self.log_file.write(
            f'[CHAR] U+{cp:04X} {char} ({name}) -> {repl!r} [{method}]\n')
        self.log_file.flush()


# ═══════════════════════════════════════════════════════════════════
# PTY Wrapper
# ═══════════════════════════════════════════════════════════════════

def set_winsize(fd, rows, cols):
    """Set the terminal window size on a file descriptor."""
    packed = struct.pack('HHHH', rows, cols, 0, 0)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, packed)


def run_pty(cmd, filt, rows=24, cols=80):
    """Fork cmd in a PTY, filter its output, display on our terminal."""
    if not os.isatty(sys.stdin.fileno()):
        print('error: stdin is not a terminal (use --pipe for non-tty)',
              file=sys.stderr)
        return 1

    # Save original terminal settings
    old_attrs = termios.tcgetattr(sys.stdin)
    exit_code = 0

    try:
        # Put our terminal into raw mode
        tty.setraw(sys.stdin)

        # Fork with PTY
        pid, master_fd = pty.fork()

        if pid == 0:
            # ── Child process ──
            set_winsize(sys.stdout.fileno(), rows, cols)
            os.environ['TERM'] = 'vt100'
            os.environ['LANG'] = 'C'
            os.environ['LC_ALL'] = 'C'
            os.environ.pop('COLORTERM', None)
            os.environ.pop('TERM_PROGRAM', None)
            os.environ['COLUMNS'] = str(cols)
            os.environ['LINES'] = str(rows)
            try:
                os.execvp(cmd[0], cmd)
            except FileNotFoundError:
                sys.stderr.write(f'a2filter: command not found: {cmd[0]}\n')
                os._exit(127)

        # ── Parent process ──
        set_winsize(master_fd, rows, cols)

        # Don't forward SIGWINCH -- keep the PTY at fixed Apple IIe size.
        # The user's actual terminal can be any size; we render into it.
        signal.signal(signal.SIGWINCH, signal.SIG_IGN)

        stdin_fd = sys.stdin.fileno()
        stdout_fd = sys.stdout.fileno()

        while True:
            try:
                rlist, _, _ = select.select([stdin_fd, master_fd], [], [], 0.1)
            except (select.error, InterruptedError):
                continue

            if stdin_fd in rlist:
                try:
                    data = os.read(stdin_fd, 1024)
                except OSError:
                    break
                if not data:
                    break
                try:
                    os.write(master_fd, data)
                except OSError:
                    break

            if master_fd in rlist:
                try:
                    data = os.read(master_fd, 16384)
                except OSError as e:
                    if e.errno == errno.EIO:
                        break  # child exited
                    raise
                if not data:
                    break
                filtered = filt.feed(data)
                if filtered:
                    os.write(stdout_fd, filtered)

        # Flush any pending graphics mode switch
        tail = filt.flush()
        if tail:
            os.write(stdout_fd, tail)

        # Collect child exit status
        _, status = os.waitpid(pid, 0)
        if os.WIFEXITED(status):
            exit_code = os.WEXITSTATUS(status)
        else:
            exit_code = 1

    finally:
        # Restore terminal
        termios.tcsetattr(sys.stdin, termios.TCSAFLUSH, old_attrs)

    return exit_code


# ═══════════════════════════════════════════════════════════════════
# Pipe Mode (stdin → filter → stdout)
# ═══════════════════════════════════════════════════════════════════

def run_pipe(filt):
    """Filter stdin to stdout as a byte stream."""
    stdin_fd = sys.stdin.buffer.fileno()
    stdout_fd = sys.stdout.buffer.fileno()

    while True:
        try:
            data = os.read(stdin_fd, 16384)
        except OSError:
            break
        if not data:
            break
        filtered = filt.feed(data)
        if filtered:
            os.write(stdout_fd, filtered)

    tail = filt.flush()
    if tail:
        os.write(stdout_fd, tail)
    return 0


# ═══════════════════════════════════════════════════════════════════
# Test Pattern
# ═══════════════════════════════════════════════════════════════════

TEST_PATTERN = """\
\x1b[1m=== a2filter Test Pattern ===\x1b[0m

\x1b[4mBox Drawing - Light\x1b[0m
  \u250c\u2500\u2500\u2500\u2500\u2500\u2500\u252c\u2500\u2500\u2500\u2500\u2500\u2500\u2510
  \u2502 left \u2502 right\u2502
  \u251c\u2500\u2500\u2500\u2500\u2500\u2500\u253c\u2500\u2500\u2500\u2500\u2500\u2500\u2524
  \u2502  A   \u2502  B   \u2502
  \u2514\u2500\u2500\u2500\u2500\u2500\u2500\u2534\u2500\u2500\u2500\u2500\u2500\u2500\u2518

\x1b[4mBox Drawing - Heavy\x1b[0m
  \u250f\u2501\u2501\u2501\u2501\u2501\u2501\u2533\u2501\u2501\u2501\u2501\u2501\u2501\u2513
  \u2503 left \u2503 right\u2503
  \u2517\u2501\u2501\u2501\u2501\u2501\u2501\u253b\u2501\u2501\u2501\u2501\u2501\u2501\u251b

\x1b[4mBox Drawing - Double\x1b[0m
  \u2554\u2550\u2550\u2550\u2550\u2550\u2550\u2566\u2550\u2550\u2550\u2550\u2550\u2550\u2557
  \u2551 left \u2551 right\u2551
  \u255a\u2550\u2550\u2550\u2550\u2550\u2550\u2569\u2550\u2550\u2550\u2550\u2550\u2550\u255d

\x1b[4mBox Drawing - Rounded\x1b[0m
  \u256d\u2500\u2500\u2500\u2500\u2500\u2500\u256e
  \u2502 round\u2502
  \u2570\u2500\u2500\u2500\u2500\u2500\u2500\u256f

\x1b[4mBlock Elements\x1b[0m
  \u2591\u2591\u2591 \u2592\u2592\u2592 \u2593\u2593\u2593 \u2588\u2588\u2588
  light  med  dark  full

\x1b[4mSymbols\x1b[0m
  Bullets:  \u2022 \u25cf \u25cb \u25c6 \u25c7
  Arrows:   \u2190 \u2191 \u2192 \u2193  \u21d0 \u21d2
  Checks:   \u2713 \u2717  \u2610 \u2611 \u2612
  Math:     \u00b1 \u00d7 \u00f7 \u2248 \u2260 \u2264 \u2265 \u221e

\x1b[4mTypography\x1b[0m
  Quotes:   \u201chello\u201d \u2018world\u2019
  Dashes:   em\u2014dash  en\u2013dash
  Ellipsis: wait\u2026
  Legal:    \u00a9 \u00ae \u2122

\x1b[4mAccented Characters (decomposition)\x1b[0m
  caf\u00e9  na\u00efve  r\u00e9sum\u00e9  \u00fcber  gar\u00e7on

\x1b[4mBraille Spinners\x1b[0m
  \u280b \u2819 \u2839 \u2838 \u283c \u2834 \u2826 \u2827 \u2807 \u280f

\x1b[4mANSI Color (should be stripped)\x1b[0m
  \x1b[31mred\x1b[0m \x1b[32mgreen\x1b[0m \x1b[34mblue\x1b[0m \x1b[1mbold\x1b[0m \x1b[4munderline\x1b[0m \x1b[7mreverse\x1b[0m
  \x1b[38;5;196m256-color\x1b[0m  \x1b[38;2;255;128;0mtruecolor\x1b[0m
  \x1b[1;31;42mbold+red+green-bg\x1b[0m -> should keep bold only

\x1b[4mPowerline Glyphs\x1b[0m
  \ue0a0 branch  \ue0b0 separator  \ue0b2 separator

\x1b[1mEnd of test pattern.\x1b[0m
"""


def run_test(filt):
    """Emit the test pattern through the filter."""
    raw = TEST_PATTERN.encode('utf-8')
    filtered = filt.feed(raw)
    filtered += filt.flush()

    # Write directly to stdout (might be in raw mode or not)
    try:
        os.write(sys.stdout.fileno(), filtered)
    except OSError:
        sys.stdout.buffer.write(filtered)
    return 0


# ═══════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════

def main():
    p = argparse.ArgumentParser(
        prog='a2filter',
        description='Apple IIe VT100 terminal filter',
        epilog='Examples:\n'
               '  %(prog)s bash              # filtered shell\n'
               '  %(prog)s nvim file.txt      # filtered nvim\n'
               '  %(prog)s --test             # show test pattern\n'
               '  %(prog)s --test --ascii-only # test without VT100 gfx\n'
               '  %(prog)s --pipe < input.raw # filter a byte stream\n',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument('--test', action='store_true',
                   help='Display test pattern through the filter')
    p.add_argument('--pipe', action='store_true',
                   help='Filter stdin to stdout (for socat integration)')
    p.add_argument('--ascii-only', action='store_true',
                   help='Use ASCII +|- for box-drawing instead of VT100 gfx')
    p.add_argument('--no-sgr', action='store_true',
                   help='Strip ALL SGR sequences (including bold/underline)')
    p.add_argument('--log', metavar='FILE',
                   help='Write substitution log to FILE (or - for stderr)')
    p.add_argument('--stats', action='store_true',
                   help='Print substitution statistics on exit')
    p.add_argument('--cols', type=int, default=80,
                   help='Terminal width (default: 80)')
    p.add_argument('--rows', type=int, default=24,
                   help='Terminal height (default: 24)')
    p.add_argument('command', nargs=argparse.REMAINDER,
                   help='Command to run (use -- before commands with flags)')

    args = p.parse_args()

    # Strip leading '--' from command if present
    if args.command and args.command[0] == '--':
        args.command = args.command[1:]

    # Validate mode
    if not args.test and not args.pipe and not args.command:
        p.print_help()
        return 1

    # Open log file
    log_file = None
    if args.log:
        if args.log == '-':
            log_file = sys.stderr
        else:
            log_file = open(args.log, 'w')

    # Create filter
    filt = VT100Filter(
        ascii_only=args.ascii_only,
        strip_all_sgr=args.no_sgr,
        log_file=log_file,
    )

    # Run appropriate mode
    try:
        if args.test:
            rc = run_test(filt)
        elif args.pipe:
            rc = run_pipe(filt)
        else:
            rc = run_pty(args.command, filt, rows=args.rows, cols=args.cols)
    finally:
        if args.stats:
            stats_text = filt.format_stats()
            # Stats go to stderr so they don't mix with filtered output
            sys.stderr.write(stats_text + '\n')
        if log_file and log_file is not sys.stderr:
            log_file.close()

    return rc


if __name__ == '__main__':
    sys.exit(main())
