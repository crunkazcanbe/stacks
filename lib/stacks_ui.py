#!/usr/bin/env python3
"""
stacks_ui.py — Shared UI functions for stacks and stacks_menu
- strip_noise(line)     : clean a log line of ANSI/noise
- is_noise(line)        : True if line should be skipped
- STACKS_ART            : the ASCII art as a string
"""
import re

STACKS_ART = """
  ____  _____  _    ____ _  _______ 
 / ___||_   _|/ \\  / ___| |/ /  ___|
 \\___ \\  | | / _ \\| |   | ' /|___ \\
  ___) | | |/ ___ \\ |___| . \\ ___) |
 |____/  |_/_/   \\_\\____|_|\\_\\____/ 
"""

# Lines that are definitely noise - never show in log display
_NOISE = re.compile(
    r'[\x1b\x00-\x1f\x7f]'      # control/escape chars
    r'|[░█]{2,}'                  # block chars from loading bars
    r'|\[[\s#>\-=]{3,}'          # old loading bar brackets
    r'|Press Ctrl'                # cancel hints
    r'|=== '                      # sequence markers
    r'|SEQUENCE'
    r'|____'                      # ASCII art fragments
    r'|\\___'
    r'|/ ___'
    r'|\|____'
)

# Art-specific lines to skip
_ART = re.compile(r'^[\s_/\\|.=\[\](){}#*\-]+$')

def strip_noise(line):
    """Strip ANSI codes and control characters from a log line."""
    line = re.sub(r'\x1b[^a-zA-Z]*[a-zA-Z]', '', line)
    line = re.sub(r'[\x00-\x1f\x7f]', '', line)
    return line.strip()

def is_noise(line):
    """Return True if this line should NOT be shown in a log display."""
    if not line or len(line) < 3:
        return True
    if _NOISE.search(line):
        return True
    if _ART.match(line):
        return True
    # Pure percentage/progress lines
    if re.match(r'^[\d\s%]+$', line):
        return True
    return False

def clean_log_line(raw):
    """Strip and filter a raw log line. Returns clean line or empty string."""
    line = strip_noise(raw)
    if is_noise(line):
        return ''
    return line
