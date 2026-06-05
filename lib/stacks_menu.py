#!/usr/bin/env python3
"""
StacksMenu — Interactive TUI for stacks
stacks menu  →  launches this
Same look/feel as menu but focused on stack/container management
"""

import curses, os, re, subprocess, threading, time, json, sys
from datetime import datetime

STACKS_DIR = "/srv/stacks/Stacks"
CONF_DIR   = os.path.expanduser("~/.config/stacks")
STACKS_BIN = "/usr/local/bin/stacks"
DYNAMICS_DIR = "/srv/stacks/Configs/Dynamics"

# ── Color pairs ──────────────────────────────────────────────────────────────
C_HEADER    = 1
C_NORMAL    = 2
C_SELECTED  = 3
C_ACCENT    = 4
C_DIM       = 5
C_POPUP_BDR = 6
C_POPUP_SEL = 7
C_GREEN     = 8
C_RED       = 9
C_YELLOW    = 10
C_CYAN      = 11
C_RUNNING   = 12
C_STOPPED   = 13
C_TRANS_BLUE = 14
C_TRANS_PINK = 15
C_TRANS_WHITE = 16

def init_colors():
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(C_HEADER,    81,  17)
    curses.init_pair(C_NORMAL,   252,  -1)
    curses.init_pair(C_SELECTED,  16,  75)
    curses.init_pair(C_ACCENT,    81,  -1)
    curses.init_pair(C_DIM,      245,  -1)
    curses.init_pair(C_POPUP_BDR,135,  -1)
    curses.init_pair(C_POPUP_SEL, 16, 135)
    curses.init_pair(C_GREEN,     82,  -1)
    curses.init_pair(C_RED,      196,  -1)
    curses.init_pair(C_YELLOW,   220,  -1)
    curses.init_pair(C_CYAN,      81,  -1)
    curses.init_pair(C_RUNNING,   82,  -1)
    curses.init_pair(C_STOPPED,  240,  -1)
    curses.init_pair(C_TRANS_BLUE,  117, -1)
    curses.init_pair(C_TRANS_PINK,  218, -1)
    curses.init_pair(C_TRANS_WHITE, 231, -1)


WHALE_ART = [
    "                  ##        .",
    "            ## ## ##       ==",
    "         ## ## ## ## ##   ===",
    "     /=====================\\___/ ===",
    " ~~ {~~  ~~~~  ~~~  ~~~~  ~~ ~ /   ===-  ~~~",
    "     \\______ o            __/",
    "      \\      \\         __/",
    "       \\      \\______ __/",
    "        \\_______________/",
]

def draw_whale_frame(stdscr, frame):
    """Draw centered trans-pride Docker whale with a flowing color wave."""
    pairs = [C_TRANS_BLUE, C_TRANS_PINK, C_TRANS_WHITE, C_TRANS_PINK, C_TRANS_BLUE]
    try:
        h, w = stdscr.getmaxyx()
    except:
        return
    art_w = max(len(l) for l in WHALE_ART)
    art_h = len(WHALE_ART)
    top = max(0, (h - art_h - 2) // 2)
    left = max(0, (w - art_w) // 2)
    for row, line in enumerate(WHALE_ART):
        for col, ch in enumerate(line):
            if ch == ' ':
                continue
            band = int(((col / max(1, art_w)) * len(pairs)) - frame * 0.5) % len(pairs)
            try:
                stdscr.addstr(top + row, left + col, ch,
                              curses.color_pair(pairs[band]) | curses.A_BOLD)
            except:
                pass
    label = "stacks"
    try:
        stdscr.addstr(top + art_h + 1, max(0, (w - len(label)) // 2),
                      label, curses.color_pair(C_TRANS_PINK) | curses.A_BOLD)
    except:
        pass


# ── Data layer ───────────────────────────────────────────────────────────────
data_lock = threading.Lock()
app_data = {
    "stacks": [],       # [{name, running, stopped, missing, total}]
    "containers": [],   # [{name, status, image, stack}]
    "mem_stats": {},    # {container_name: "used / limit"}
    "img_sizes": {},    # {image: "size"}
    "last_update": 0,
}

def get_stacks():
    stacks = []
    try:
        yml_files = sorted(f for f in os.listdir(STACKS_DIR) if f.endswith('.yml'))
        for fname in yml_files:
            name = fname.replace('.yml','')
            path = os.path.join(STACKS_DIR, fname)
            # Get containers defined in this file
            r = subprocess.run(['docker','compose','-f',path,'ps','--format','json'],
                             capture_output=True, text=True, timeout=5)
            running = stopped = 0
            if r.returncode == 0:
                for line in r.stdout.strip().split('\n'):
                    if not line.strip(): continue
                    try:
                        c = json.loads(line)
                        if c.get('State','').lower() in ('running','healthy','starting'):
                            running += 1
                        else:
                            stopped += 1
                    except: pass
            # Count defined services
            try:
                content = open(path).read()
                total = len(re.findall(r'^\s{2}[a-zA-Z0-9_-]+:\s*$', content, re.MULTILINE))
            except: total = 0
            try: fsize = os.path.getsize(path) // 1024
            except: fsize = 0
            # Get images used by this stack
            images_used = re.findall(r'^    image:\s*(\S+)', open(path).read(), re.MULTILINE)
            stacks.append({
                'name': name, 'running': running,
                'stopped': stopped, 'total': total,
                'file': path, 'size_kb': fsize,
                'images': images_used
            })
    except: pass
    return stacks

def get_containers():
    containers = []
    try:
        r = subprocess.run(
            ['docker','ps','-a','--format',
             '{"name":"{{.Names}}","status":"{{.Status}}","image":"{{.Image}}","state":"{{.State}}","stack":"{{.Label \"com.docker.compose.project\"}}"}'],
            capture_output=True, text=True, timeout=10)
        if r.returncode == 0:
            for line in r.stdout.strip().split('\n'):
                if not line.strip(): continue
                try:
                    c = json.loads(line)
                    containers.append(c)
                except: pass
    except: pass
    # Sort: running first, then stopped
    running = [c for c in containers if c.get('state','').lower() == 'running']
    others  = [c for c in containers if c.get('state','').lower() != 'running']
    return running + others

def fetch_mem_stats():
    """Fetch docker stats and image sizes in background."""
    while True:
        try:
            r = subprocess.run(
                ['docker','stats','--no-stream','--format',
                 '{{.Name}}\t{{.MemUsage}}'],
                capture_output=True, text=True, timeout=20)
            if r.returncode == 0:
                mem = {}
                for line in r.stdout.strip().split('\n'):
                    if '\t' in line:
                        n, m = line.split('\t', 1)
                        mem[n.strip()] = m.strip()
                with data_lock:
                    app_data['mem_stats'] = mem
        except: pass
        # Fetch image sizes
        try:
            r2 = subprocess.run(
                ['docker','images','--format','{{.Repository}}:{{.Tag}}\t{{.Size}}'],
                capture_output=True, text=True, timeout=10)
            if r2.returncode == 0:
                imgs = {}
                for line in r2.stdout.strip().split('\n'):
                    if '\t' in line:
                        img, sz = line.split('\t', 1)
                        imgs[img.strip()] = sz.strip()
                with data_lock:
                    app_data['img_sizes'] = imgs
        except: pass
        time.sleep(15)

def refresh_data():
    while True:
        stacks = get_stacks()
        containers = get_containers()
        with data_lock:
            app_data['stacks'] = stacks
            app_data['containers'] = containers
            app_data['last_update'] = time.time()
        time.sleep(8)

# ── Drawing helpers ──────────────────────────────────────────────────────────
def draw_header(win, title, w):
    try:
        win.attron(curses.color_pair(C_HEADER))
        win.addstr(0, 0, ' ' * (w-1))
        x = (w - len(title)) // 2
        win.addstr(0, max(0,x), title[:w-1])
        win.attroff(curses.color_pair(C_HEADER))
    except: pass

# ── Tab Registration Helper ──────────────────────────────────────────────────
# Use register_tab(name) to add a new tab to the TUI.
# Then add: draw function, key handler (elif tab == N), footer hints.
# Example: register_tab("MyTab") → adds to TABS list at runtime
# ✔ draw_<name>_tab(win, h, w, sel) — draw function
# ✔ elif tab == N: — key handler in main loop
# ✔ N: ['hints'] — add to FOOTER_HINTS dict
def register_tab(name):
    """Register a new tab. Call before main() runs. Returns tab index."""
    if name not in TABS:
        TABS.append(name)
    return TABS.index(name)

def draw_tabs(win, y, w, tabs, active):
    """Draw scrolling tab bar - always shows active tab."""
    try: win.addstr(y, 0, ' ' * (w-1), curses.color_pair(C_DIM))
    except: pass
    # Build all labels
    labels = [f' {t} ' for t in tabs]
    widths = [len(l)+1 for l in labels]
    total = sum(widths)
    if total <= w - 2:
        # All fit
        x = 1
        for i, label in enumerate(labels):
            try:
                attr = curses.color_pair(C_SELECTED) if i == active else curses.color_pair(C_DIM)
                win.addstr(y, x, label, attr)
            except: pass
            x += widths[i]
    else:
        # Scroll: center active tab
        # Find x offset so active tab is visible
        x_positions = []
        x = 1
        for w2 in widths:
            x_positions.append(x)
            x += w2
        # Scroll offset: try to center active
        active_x = x_positions[active]
        offset = max(0, active_x - w//2)
        # Draw with offset
        x = 1
        if offset > 0:
            try: win.addstr(y, x, '◀', curses.color_pair(C_DIM))
            except: pass
            x += 2
        for i, label in enumerate(labels):
            draw_x = x_positions[i] - offset + (2 if offset > 0 else 0)
            if draw_x < 1: continue
            if draw_x + len(label) > w - 2:
                try: win.addstr(y, w-3, '▶', curses.color_pair(C_DIM))
                except: pass
                break
            try:
                attr = curses.color_pair(C_SELECTED) if i == active else curses.color_pair(C_DIM)
                win.addstr(y, draw_x, label, attr)
            except: pass

def draw_footer(win, h, w, hints):
    msg = '  '.join(hints)
    try:
        win.attron(curses.color_pair(C_DIM))
        win.addstr(h-1, 0, (' ' * (w-1)))
        win.addstr(h-1, 2, msg[:w-4])
        win.attroff(curses.color_pair(C_DIM))
    except: pass

_FILTER_ALPHA = "abcdefghijklmnopqrstuvwxyz#"

def list_filter(items, key_fns, letter, inline):
    """Filter items by a leading-letter jump and/or an inline substring.
    key_fns[0] is the primary key (used for the letter jump); all key_fns are
    searched for the inline substring. Returns the filtered list."""
    out = items
    if letter:
        if letter == "#":
            out = [it for it in out
                   if (str(key_fns[0](it))[:1] or " ") and not str(key_fns[0](it))[:1].isalpha()]
        else:
            out = [it for it in out
                   if str(key_fns[0](it)).lower().lstrip("●○■⚠ ").startswith(letter)]
    if inline:
        f = inline.lower()
        out = [it for it in out
               if any(f in str(fn(it)).lower() for fn in key_fns)]
    return out

def draw_filter_bar(win, y, w, letter, inline, inline_mode, shown, total):
    """Draw an alphabet/search bar (registry-style) on a single row."""
    try: win.addstr(y, 0, " " * (w-1), curses.color_pair(C_DIM))
    except: pass
    x = 2
    if inline_mode:
        s = f"/ {inline}_"
        try: win.addstr(y, x, s[:w-4], curses.color_pair(C_YELLOW))
        except: pass
        return
    for ch in _FILTER_ALPHA:
        if x > w - 18: break
        attr = curses.color_pair(C_SELECTED) if letter == ch else curses.color_pair(C_ACCENT)
        try: win.addstr(y, x, ch, attr)
        except: pass
        x += 2
    tail = f" [{shown}/{total}]"
    if inline:
        tail = f" /{inline}{tail}"
    try: win.addstr(y, max(x, w-len(tail)-2), tail[:w-2], curses.color_pair(C_DIM))
    except: pass

def draw_border_box(win, y, x, h, w, title=''):
    try:
        win.attron(curses.color_pair(C_POPUP_BDR))
        win.addstr(y, x, '╔' + '═'*(w-2) + '╗')
        for i in range(1, h-1):
            win.addstr(y+i, x, '║')
            win.addstr(y+i, x+w-1, '║')
        try:
            win.addstr(y+h-1, x, '╚' + '═'*(w-2) + '╝')
        except: pass
        if title:
            t = f' {title} '
            try:
                win.addstr(y, x + (w-len(t))//2, t)
            except: pass
        win.attroff(curses.color_pair(C_POPUP_BDR))
    except: pass

# ── Popup action menu ────────────────────────────────────────────────────────
def run_popup_action(stdscr, title, actions):
    """Show a popup with selectable actions. Returns chosen action or None."""
    h, w = stdscr.getmaxyx()
    pw = max(len(title)+6, max(len(a[0])+6 for a in actions)+4, 36)
    ph = len(actions) + 4
    py = (h - ph) // 2
    px = (w - pw) // 2

    popup = curses.newwin(ph, pw, py, px)
    popup.keypad(True)
    sel = 0

    while True:
        popup.clear()
        draw_border_box(popup, 0, 0, ph, pw, title[:pw-4])
        for i, (label, _) in enumerate(actions):
            y = i + 2
            try:
                if i == sel:
                    popup.addstr(y, 2, f'  {label:<{pw-6}}', curses.color_pair(C_POPUP_SEL))
                else:
                    popup.addstr(y, 2, f'  {label:<{pw-6}}', curses.color_pair(C_NORMAL))
            except: pass
        popup.refresh()

        try:
            k = popup.getch()
        except curses.error:
            k = curses.KEY_RESIZE
        if k == curses.KEY_UP:
            sel = (sel - 1) % len(actions)
        elif k == curses.KEY_DOWN:
            sel = (sel + 1) % len(actions)
        elif k in (10, 13):
            return actions[sel]
        elif k == 27:  # ESC
            return None

# ── Log popup (shows command output line by line) ────────────────────────────
def run_log_popup(stdscr, title, cmd):
    import time as _t
    h, w = stdscr.getmaxyx()
    pw = min(w-6,70); ph=7; py=(h-ph)//2; px=(w-pw)//2
    popup = curses.newwin(ph,pw,py,px)
    popup.nodelay(True)
    try: curses.mousemask(0)
    except: pass
    bar_w=pw-6; pct=0; frame=0
    spinner="⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    def draw(done=False):
        try:
            popup.clear()
            draw_border_box(popup,0,0,ph,pw,f" {title[:pw-4]} ")
            filled=int(bar_w*pct/100)
            bar="█"*filled+"░"*(bar_w-filled)
            try: popup.addstr(2,3,f"[{bar}]",curses.color_pair(C_CYAN))
            except: pass
            if done:
                try: popup.addstr(3,3,"✔ Done — press any key",curses.color_pair(C_GREEN))
                except: pass
            else:
                sp=spinner[frame%len(spinner)]
                try: popup.addstr(3,3,f"{sp} {title}... {pct}%",curses.color_pair(C_YELLOW))
                except: pass
            popup.refresh()
        except: pass
    stdscr.clear(); stdscr.refresh(); draw()
    proc=subprocess.Popen(cmd,shell=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    try:
        while proc.poll() is None:
            pct=min(95,pct+2); frame+=1; draw(); _t.sleep(0.1)
            k=popup.getch()
            if k == curses.KEY_MOUSE: continue
            if k in (27,ord("q"),ord("Q")): proc.terminate(); break
    except KeyboardInterrupt: proc.terminate()
    proc.wait(); popup.nodelay(False)
    pct=100; draw(done=True); popup.getch()

def clean_log_line(raw):
    """Strip ANSI and noise from a log line."""
    import re as _re
    line = _re.sub(r'\x1b[^a-zA-Z]*[a-zA-Z]', '', raw)
    line = _re.sub(r'[\x00-\x1f\x7f]', '', line).strip()
    if not line or len(line) < 3: return ''
    # Block chars and art
    if _re.search(r'[░█]{2,}|Press Ctrl|===|____|\\___|/ ___', line): return ''
    if _re.match(r'^[\s_/\\|.=\[\](){}#*\-]+$', line): return ''
    if _re.match(r'^[\[\]#>\-\s\d%]+$', line): return ''
    # Countdown/timer lines
    if _re.search(r'\d+\s+seconds? remaining', line): return ''
    if _re.match(r'^\[\d{2}:\d{2}:\d{2}\]\s+\.\.\.\s+\d+', line): return ''
    # Sablier/watchdog noise
    if _re.search(r'sablier|watchdog|Restarting|-> Restart', line, _re.IGNORECASE): return ''
    # Lines with emoji/icons (non-ASCII symbols used as decorators)
    if _re.search(r'[\U00002500-\U00002BFF\U0001F000-\U0001FFFF]', line): return ''
    # Only keep lines with actual words
    if not _re.search(r'[a-zA-Z]{3,}', line): return ''
    return line


def _bw_input(popup, pw, ph, prompt, default, bar_w, pct, title, spinner, frame):
    """Single line text input inside popup."""
    try:
        popup.nodelay(False)  # MUST block - no auto-advance
        popup.timeout(-1)
        popup.clear()
        draw_border_box(popup, 0, 0, ph, pw, f" {title[:pw-4]} ")
        filled = int(bar_w * pct / 100)
        bar = "█" * filled + "░" * (bar_w - filled)
        try: popup.addstr(2, 2, f"[{bar}]", curses.color_pair(C_CYAN))
        except: pass
        sp = spinner[frame % len(spinner)]
        try: popup.addstr(3, 3, f"{sp} {pct}%", curses.color_pair(C_YELLOW))
        except: pass
        try: popup.addstr(5, 3, prompt[:pw-6], curses.color_pair(C_ACCENT))
        except: pass
        if default:
            try: popup.addstr(6, 3, f"default: {default[:pw-14]}", curses.color_pair(C_DIM))
            except: pass
        try: popup.addstr(7, 3, "> ", curses.color_pair(C_NORMAL))
        except: pass
        popup.refresh()
        curses.curs_set(1)
        # Manual input loop - ignores resize/mouse events
        val = []
        popup.nodelay(True)
        # Drain all buffered keys
        while popup.getch() != -1: pass
        popup.nodelay(False)
        popup.timeout(-1)
        while True:
            ch = popup.getch()
            if ch == curses.KEY_RESIZE: continue  # ignore keyboard popup
            if ch == curses.KEY_MOUSE: continue
            if ch in (10, 13):  # Enter
                break
            elif ch == 27:  # ESC = go back
                return None
            elif ch == 3:  # Ctrl+C = go back
                return None
            elif ch == curses.KEY_F1:  # F1 = go back
                return None
            elif ch == curses.KEY_LEFT:  # Left arrow = go back
                return None
            elif ch in (curses.KEY_BACKSPACE, 127, 8):
                if val: val.pop()
            elif 32 <= ch <= 126:
                val.append(chr(ch))
            # Redraw input line
            try:
                popup.addstr(7, 3, "> " + "".join(val) + " " * (pw-12), curses.color_pair(C_NORMAL))
                popup.move(7, 5 + len(val))
            except: pass
            popup.refresh()
        curses.curs_set(0)
        result = "".join(val).strip()
        return result if result else default
    except:
        curses.curs_set(0)
        return None  # any error = treat as ESC/back


def _bw_select(popup, pw, ph, prompt, items, bar_w, pct, title, spinner, frame):
    """Scrollable list selection inside popup."""
    if not items: return None
    # Add cancel option
    items = list(items) + ["✕  Cancel"]
    sel = 0; scroll = 0
    visible = ph - 6
    popup.nodelay(False)
    while True:
        popup.clear()
        draw_border_box(popup, 0, 0, ph, pw, f" {title[:pw-4]} ")
        filled = int(bar_w * pct / 100)
        bar = "█" * filled + "░" * (bar_w - filled)
        try: popup.addstr(2, 2, f"[{bar}]", curses.color_pair(C_CYAN))
        except: pass
        try: popup.addstr(3, 3, prompt[:pw-6], curses.color_pair(C_ACCENT))
        except: pass
        for i in range(min(visible, len(items))):
            idx = scroll + i
            if idx >= len(items): break
            y = 4 + i
            label = str(items[idx])[:pw-6]
            if idx == sel:
                try: popup.addstr(y, 2, f" ▶ {label:<{pw-6}}", curses.color_pair(C_SELECTED))
                except: pass
            else:
                try: popup.addstr(y, 2, f"   {label:<{pw-6}}", curses.color_pair(C_NORMAL))
                except: pass
        popup.refresh()
        try:
            try:
                k = popup.getch()
            except curses.error:
                k = curses.KEY_RESIZE
        except curses.error:
            k = curses.KEY_RESIZE
        if k == curses.KEY_RESIZE: continue
        if k == curses.KEY_MOUSE: continue
        if k == curses.KEY_LEFT: return None  # left arrow = go back
        if k == curses.KEY_UP:
            if sel > 0: sel -= 1
            if sel < scroll: scroll = sel
        elif k == curses.KEY_DOWN:
            if sel < len(items)-1: sel += 1
            if sel >= scroll + visible: scroll = sel - visible + 1
        elif k in (10, 13):
            if items[sel] == "✕  Cancel": return None
            return items[sel]
        elif k in (27, 3, curses.KEY_F1): return None


def _bw_yesno(popup, pw, ph, prompt, default, bar_w, pct, title, spinner, frame):
    """Yes/No selection inside popup."""
    sel = 0 if default.lower() == "y" else 1
    popup.nodelay(False)
    while True:
        popup.clear()
        draw_border_box(popup, 0, 0, ph, pw, f" {title[:pw-4]} ")
        filled = int(bar_w * pct / 100)
        bar = "█" * filled + "░" * (bar_w - filled)
        try: popup.addstr(2, 2, f"[{bar}]", curses.color_pair(C_CYAN))
        except: pass
        sp = spinner[frame % len(spinner)]
        try: popup.addstr(3, 3, f"{sp} {pct}%", curses.color_pair(C_YELLOW))
        except: pass
        try: popup.addstr(5, 3, prompt[:pw-6], curses.color_pair(C_ACCENT))
        except: pass
        yes_attr = curses.color_pair(C_SELECTED) if sel==0 else curses.color_pair(C_NORMAL)
        no_attr  = curses.color_pair(C_SELECTED) if sel==1 else curses.color_pair(C_NORMAL)
        try: popup.addstr(7, 6,  "  YES  ", yes_attr)
        except: pass
        try: popup.addstr(7, 16, "  NO   ", no_attr)
        except: pass
        try: popup.addstr(ph-2, 2, "←→ Select  ENTER confirm  ESC/Ctrl+C cancel", curses.color_pair(C_DIM))
        except: pass
        popup.refresh()
        try:
            try:
                k = popup.getch()
            except curses.error:
                k = curses.KEY_RESIZE
        except curses.error:
            k = curses.KEY_RESIZE
        if k == curses.KEY_RESIZE: continue
        if k == curses.KEY_MOUSE: continue
        if k == curses.KEY_LEFT and sel == 0: return None  # left on YES = go back
        if k in (curses.KEY_LEFT, curses.KEY_RIGHT): sel = 1 - sel
        elif k in (10, 13): return "y" if sel==0 else "n"
        elif k in (ord("y"), ord("Y")): return "y"
        elif k in (ord("n"), ord("N")): return "n"
        elif k == 27: return None
        elif k == 3: return None  # Ctrl+C = back
        elif k == curses.KEY_F1: return None


def _bw_status(popup, pw, ph, msg, bar_w, pct, title, spinner, frame):
    popup.clear()
    draw_border_box(popup, 0, 0, ph, pw, f" {title[:pw-4]} ")
    filled = int(bar_w * pct / 100)
    bar = "█" * filled + "░" * (bar_w - filled)
    try: popup.addstr(2, 2, f"[{bar}]", curses.color_pair(C_CYAN))
    except: pass
    sp = spinner[frame % len(spinner)]
    try: popup.addstr(3, 3, f"{sp} {msg[:pw-6]}", curses.color_pair(C_YELLOW))
    except: pass
    popup.refresh()



# ── Registry search - reusable curses image picker ───────────────────────────
def _load_registry_searchers():
    """Load all registry search functions from stacks_search.py."""
    import importlib.util as _ilu
    spec = _ilu.spec_from_file_location("stacks_search", "/usr/local/lib/stacks_search.py")
    mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.REGISTRIES, mod.search_all

def registry_search_popup(stdscr, term, bar_w, pct, title, spinner, frame):
    """
    Full curses multi-registry image search.
    Left/Right = switch registry tab
    Up/Down    = scroll results
    Enter      = select image
    ESC/Ctrl+C = cancel
    Returns selected image string or None.
    """
    import concurrent.futures as _cf
    import time as _t
    try:
        return _registry_search_inner(stdscr, term, bar_w, pct, title, spinner, frame)
    except (KeyboardInterrupt, Exception):
        return None

def _registry_search_inner(stdscr, term, bar_w, pct, title, spinner, frame):
    import concurrent.futures as _cf
    import time as _t

    try:
        REGISTRIES, search_all = _load_registry_searchers()
    except Exception as e:
        return None

    reg_names = ["ALL"] + list(REGISTRIES.keys())
    reg_idx = [0]
    results = {}
    sel = [0]
    scroll = [0]
    search_done = False
    letter_filter = [None]  # None=all, 'a'-'z'=filter
    inline_filter = [""]    # typed filter
    inline_mode = [False]   # True when typing inline filter

    h, w = stdscr.getmaxyx()
    pw = min(w-4, 82); ph = min(h-4, 30)
    py = (h-ph)//2; px = (w-pw)//2
    popup = curses.newwin(ph, pw, py, px)
    popup.keypad(True)
    popup.nodelay(True)

    ALPHA = "abcdefghijklmnopqrstuvwxyz#"

    def get_visible():
        if reg_names[reg_idx[0]] == "ALL":
            out = []
            for rlist in results.values():
                out += [r for r in rlist if "_error" not in r]
        else:
            out = [r for r in results.get(reg_names[reg_idx[0]], []) if "_error" not in r]
        # Apply letter filter
        if letter_filter[0]:
            lf = letter_filter[0]
            if lf == "#":
                out = [r for r in out if r.get("pull","") and not r["pull"][0].isalpha()]
            else:
                out = [r for r in out if r.get("pull","").lower().startswith(lf) or
                       r.get("pull","").lower().split("/")[-1].startswith(lf)]
        # Apply inline filter
        if inline_filter[0]:
            f = inline_filter[0].lower()
            out = [r for r in out if f in r.get("pull","").lower() or f in r.get("desc","").lower()]
        return out

    def human_num(n):
        if not n: return ""
        try: n = int(n)
        except: return str(n)[:6]
        if n >= 1_000_000_000: return f"{n//1_000_000_000}B"
        if n >= 1_000_000: return f"{n//1_000_000}M"
        if n >= 1_000: return f"{n//1_000}K"
        return str(n)

    def draw(loading=False):
        try:
            popup.clear()
            draw_border_box(popup, 0, 0, ph, pw, f" Search: {term[:pw-12]} ")

            # Registry tabs - 2 rows
            tab_x = 2
            tab_y = 2
            for i, rname in enumerate(reg_names):
                short = rname.split()[0][:8]
                cnt = len([r for r in results.get(rname,[]) if "_error" not in r]) if rname != "ALL" else sum(len([r for r in v if "_error" not in r]) for v in results.values())
                label = f"{short}({cnt})"
                if tab_x + len(label) + 1 > pw - 2:
                    tab_y += 1
                    tab_x = 2
                if tab_y > 3: break  # max 2 rows
                if i == reg_idx[0]:
                    try: popup.addstr(tab_y, tab_x, label, curses.color_pair(C_SELECTED))
                    except: pass
                else:
                    try: popup.addstr(tab_y, tab_x, label, curses.color_pair(C_DIM))
                    except: pass
                tab_x += len(label) + 1

            # Alphabet filter row (row 4 after 2 tab rows)
            ax = 2
            try: popup.addstr(4, ax, "Filter: ", curses.color_pair(C_DIM))
            except: pass
            ax = 10
            for ch in ALPHA:
                attr = curses.color_pair(C_SELECTED) if letter_filter[0]==ch else curses.color_pair(C_ACCENT)
                try: popup.addstr(4, ax, ch, attr)
                except: pass
                ax += 2
                if ax > pw-4: break

            # Inline search box
            if inline_mode[0]:
                try: popup.addstr(5, 2, f"/ {inline_filter[0]}_"[:pw-4], curses.color_pair(C_YELLOW))
                except: pass
            else:
                try: popup.addstr(5, 2, "/ search  ↔ reg  ↑↓ scroll  ENTER select  ESC cancel"[:pw-4], curses.color_pair(C_DIM))
                except: pass
            try: popup.addstr(6, 2, "─"*(pw-4), curses.color_pair(C_DIM))
            except: pass

            visible_items = get_visible()
            list_h = ph - 11
            items_to_show = visible_items[scroll[0]:scroll[0]+list_h]

            if loading and not visible_items:
                sp = spinner[frame[0] % len(spinner)]
                try: popup.addstr(ph//2, pw//2-8, f"{sp} Searching...", curses.color_pair(C_YELLOW))
                except: pass
            else:
                for i, item in enumerate(items_to_show):
                    y = 7 + i
                    if y >= ph-3: break
                    idx = scroll[0] + i
                    pull = item.get("pull","")
                    # Skip helm/kubectl install strings
                    if pull.startswith("helm ") or pull.startswith("kubectl "): continue
                    pulls = human_num(item.get("pulls","") or item.get("pull_count",""))
                    stars = human_num(item.get("stars","") or item.get("star_count",""))
                    # Image size from local cache
                    img_sz = app_data["img_sizes"].get(pull, app_data["img_sizes"].get(pull+":latest",""))[:6]
                    pull_str = pull[:38]
                    stat_str = f"↓{pulls:<5} ★{stars:<5} {img_sz:<7}"
                    line = f"{pull_str:<38} {stat_str}"[:pw-4]
                    if idx == sel[0]:
                        try: popup.addstr(y, 2, line, curses.color_pair(C_SELECTED))
                        except: pass
                    else:
                        try:
                            popup.addstr(y, 2, f"{pull_str:<38} ", curses.color_pair(C_NORMAL))
                            popup.addstr(y, 41, f"↓{pulls:<5}", curses.color_pair(C_GREEN if pulls else C_DIM))
                            popup.addstr(y, 48, f"★{stars:<5}", curses.color_pair(C_YELLOW if stars else C_DIM))
                            popup.addstr(y, 54, f"{img_sz:<7}", curses.color_pair(C_CYAN if img_sz else C_DIM))
                        except: pass

            # Footer
            total = len(get_visible())
            try: popup.addstr(ph-2, 2, f"[{total}]  ◀▶ reg  ↑↓  ENTER select  Q/ESC/Ctrl+C cancel"[:pw-4], curses.color_pair(C_DIM))
            except: pass
            popup.refresh()
        except: pass

    frame = [0]
    draw(loading=True)

    # Start search in background
    def do_search():
        nonlocal search_done
        all_results = search_all(term, 1, 50)
        for k, v in all_results.items():
            results[k] = v
        search_done = True

    import threading
    t = threading.Thread(target=do_search, daemon=True)
    t.start()

    while True:
        frame[0] += 1
        draw(loading=not search_done)
        _t.sleep(0.08)

        try:
            k = popup.getch()
        except curses.error:
            k = curses.KEY_RESIZE
        if k == -1: continue
        if k == curses.KEY_MOUSE: continue
        if k == curses.KEY_RESIZE: continue

        visible_items = get_visible()
        list_h = ph - 9

        if inline_mode[0]:
            # Typing inline filter
            if k in (10, 13): inline_mode[0] = False
            elif k == 27: inline_mode[0] = False; inline_filter[0] = ""
            elif k in (curses.KEY_BACKSPACE, 127, 8):
                inline_filter[0] = inline_filter[0][:-1]
            elif 32 <= k <= 126:
                inline_filter[0] += chr(k)
            sel[0] = 0; scroll[0] = 0
            continue

        if k == curses.KEY_UP:
            if sel[0] > 0: sel[0] -= 1
            if sel[0] < scroll[0]: scroll[0] = sel[0]
        elif k == curses.KEY_DOWN:
            if sel[0] < len(visible_items)-1: sel[0] += 1
            if sel[0] >= scroll[0] + list_h: scroll[0] = sel[0] - list_h + 1
        elif k == curses.KEY_LEFT:
            reg_idx[0] = (reg_idx[0] - 1) % len(reg_names)
            sel[0] = 0; scroll[0] = 0
        elif k == curses.KEY_RIGHT:
            reg_idx[0] = (reg_idx[0] + 1) % len(reg_names)
            sel[0] = 0; scroll[0] = 0
        elif k == ord("/"):
            inline_mode[0] = True; inline_filter[0] = ""
        elif k == ord("#"):
            letter_filter[0] = None if letter_filter[0]=="#" else "#"
            sel[0] = 0; scroll[0] = 0
        elif 97 <= k <= 122:  # a-z
            ch = chr(k)
            letter_filter[0] = None if letter_filter[0]==ch else ch
            sel[0] = 0; scroll[0] = 0
        elif k in (10, 13):
            if visible_items and sel[0] < len(visible_items):
                return visible_items[sel[0]].get("pull","")
            return None
        elif k in (27, 3, ord("q"), ord("Q")):
            return None

def run_build_wizard(stdscr, new_stack=False):
    """Full curses build wizard with back navigation."""
    import subprocess as _sp, glob as _gl, time as _t
    import sys as _bwsys
    _bw_log = open("/tmp/build_crash.log", "w")
    def _bwlog(msg):
        _bw_log.write(msg + "\n"); _bw_log.flush()
    h, w = stdscr.getmaxyx()
    pw = min(w-4, 74); ph = 14
    py = (h-ph)//2; px = (w-pw)//2
    popup = curses.newwin(ph, pw, py, px)
    popup.keypad(True)
    bar_w = pw - 6
    spinner = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    frame = [0]
    pct = [0]
    title = "Build New Service"
    def update_title():
        nonlocal title
        titles = ["Stack","Image","Name","IP","Port","Database","Redis","Companion","Start"]
        t = titles[min(step, len(titles)-1)] if step < len(titles) else "Build"
        title = f"Build [{step+1}/9] {t}"
    stdscr.refresh()

    def status(msg, p):
        pct[0]=p; frame[0]+=1
        _bw_status(popup, pw, ph, msg, bar_w, pct[0], title, spinner, frame[0])

    def inp(prompt, default=""):
        frame[0]+=1
        curses.flushinp()
        import time as _t; _t.sleep(0.15)
        return _bw_input(popup, pw, ph, prompt, default, bar_w, pct[0], title, spinner, frame[0])

    def sel(prompt, items):
        frame[0]+=1
        curses.flushinp()
        import time as _t; _t.sleep(0.15)
        return _bw_select(popup, pw, ph, prompt, items, bar_w, pct[0], title, spinner, frame[0])

    def yn(prompt, default="n"):
        frame[0]+=1
        curses.flushinp()
        import time as _t; _t.sleep(0.15)
        return _bw_yesno(popup, pw, ph, prompt, default, bar_w, pct[0], title, spinner, frame[0])

    # State for all steps
    state = {
        "target_stack": None, "stack_name": None,
        "image": None, "svc_name": None,
        "svc_ip": None, "svc_port": "8080",
        "db_info": None, "redis_info": None,
        "companion_info": None,
    }

    # Load stacks list
    stacks = sorted([f.replace(".yml","") for f in os.listdir(STACKS_DIR)
                     if f.endswith(".yml") and not f.startswith("db_")])
    _raw = stacks[:]
    stacks_display = []
    for _s in _raw:
        try:
            _c = open(os.path.join(STACKS_DIR,_s+".yml")).read()
            _n = len(re.findall(r"^    container_name:", _c, re.MULTILINE))
            stacks_display.append(f"{_s:<20} [{_n} svc]")
        except: stacks_display.append(_s)

    step = 0
    STEPS = ["stack", "image", "name", "ip", "port", "db", "redis", "companion", "netvol", "start"]

    while True:
        current = STEPS[step] if step < len(STEPS) else "done"
        update_title()
        # Debug log
        try:
            with open("/tmp/wizard_debug.log", "a") as _dbg:
                _dbg.write(f"step={step} current={current}\n")
        except: pass

        if current == "stack":
            pct[0] = 5
            if new_stack:
                result = inp("New stack name (e.g. srvs_3):", state.get("stack_name") or "srvs_3")
                if result is None or result == "":
                    return  # ESC on first question = close
                state["stack_name"] = result
                state["target_stack"] = result
                fpath = os.path.join(STACKS_DIR, result + ".yml")
                if not os.path.exists(fpath):
                    template = (
                        f"name: {result}\n\n"
                        "x-common: &common-caps\n"
                        "  restart: unless-stopped\n"
                        "  logging:\n"
                        "    driver: json-file\n"
                        "    options: {max-size: 10m, max-file: '3'}\n\n"
                        "services:\n\n"
                        "networks:\n"
                        "  traefik_net:\n"
                        "    external: true\n"
                    )
                    open(fpath, "w").write(template)
                stacks.append(result)
                step += 1
            else:
                result = sel("Select target stack:", stacks_display)
                if result is None:
                    return  # ESC on first question = close
                state["target_stack"] = result.split()[0].strip()
                step += 1

        elif current == "image":
            pct[0] = 15
            prev = state.get("image") or ""
            search_term = inp("Image (full tag skips search, name to search):", prev.split("/")[-1].split(":")[0] if prev else "")
            if search_term is None:
                step = max(0, step-1); continue  # ESC = back
            if not search_term:
                continue
            if "/" in search_term or ":" in search_term:
                state["image"] = search_term
                step += 1
            else:
                # Registry search - clears popup after
                popup.clear(); popup.refresh()
                chosen = registry_search_popup(stdscr, search_term, bar_w, pct[0], title, spinner, frame)
                # Recreate popup after registry search closes
                popup = curses.newwin(ph, pw, py, px)
                popup.keypad(True)
                if chosen:
                    state["image"] = chosen
                    step += 1
                # If ESC in registry, stay on image step

        elif current == "name":
            pct[0] = 25
            img_base = state["image"].split("/")[-1].split(":")[0].lower() if state["image"] else ""
            result = inp("Container name:", state.get("svc_name") or img_base)
            if result is None:
                step = max(0, step-1); continue  # ESC = back
            state["svc_name"] = result or img_base
            step += 1

        elif current == "ip":
            pct[0] = 35
            if not state.get("svc_ip"):
                try:
                    used = set()
                    for f in _gl.glob(f"{STACKS_DIR}/*.yml"):
                        for m in re.findall(r"192\.168\.1\.(\d+)", open(f).read()):
                            used.add(int(m))
                    state["svc_ip"] = "192.168.1." + str(next(x for x in range(200,254) if x not in used))
                except: state["svc_ip"] = "192.168.1.200"
            ip = inp("Service IP (192.168.1.x):", state["svc_ip"])
            if ip is None:
                step = max(0, step-1); continue
            state["svc_ip"] = ip
            step += 1

        elif current == "port":
            pct[0] = 40
            port = inp("Service port:", state.get("svc_port","8080"))
            if port is None:
                step = max(0, step-1); continue
            state["svc_port"] = port
            step += 1

        elif current == "db":
            pct[0] = 50
            needs_db = yn("Does this service need a database?", "n")
            if needs_db is None:
                step = max(0, step-1); continue
            if needs_db == "y":
                db_type = sel("Database type:", ["postgres","mysql","mariadb","mongo","redis","none"])
                if db_type is None:
                    step = max(0, step-1); continue  # ESC = back to previous step
                if db_type and db_type != "none":
                    db_stacks = sorted([f.replace(".yml","") for f in os.listdir(STACKS_DIR)
                                       if re.match(r"db_\d+\.yml", f)])
                    db_target = sel("Which DB stack:", db_stacks)
                    if db_target is None:
                        continue  # ESC = back to db_type question (re-run db step)
                    if db_target:
                        db_name = inp("DB container name:", f"{state['svc_name']}-{db_type}")
                        if db_name is None: continue
                        db_pass = inp("DB password:", "changeme")
                        if db_pass is None: continue
                        db_db = inp("DB name:", state["svc_name"].replace("-","_"))
                        if db_db is None: continue
                        state["db_info"] = {"type":db_type,"name":db_name,"pass":db_pass,"db":db_db,"stack":db_target}
            step += 1

        elif current == "redis":
            pct[0] = 60
            if not (state.get("db_info") and state["db_info"].get("type")=="redis"):
                needs_redis = yn("Does this service need Redis?", "n")
                if needs_redis is None:
                    step = max(0, step-1); continue
                if needs_redis == "y":
                    redis_name = inp("Redis container name:", f"{state['svc_name']}-redis")
                    if redis_name is None: continue
                    redis_stacks = sorted([f.replace(".yml","") for f in os.listdir(STACKS_DIR)
                                          if re.match(r"db_\d+\.yml", f)])
                    redis_stack = sel("Which DB stack for Redis:", redis_stacks)
                    state["redis_info"] = {"name":redis_name,"stack":redis_stack}
            step += 1

        elif current == "companion":
            pct[0] = 70
            needs_comp = yn("Does this service need a companion container?", "n")
            if needs_comp is None:
                step = max(0, step-1); continue
            if needs_comp == "y":
                comp_name = inp("Companion name:", f"{state['svc_name']}-worker")
                if comp_name is None: continue
                comp_img_term = inp("Companion image (or search):", "")
                if comp_img_term is None: continue
                if "/" in comp_img_term or ":" in comp_img_term:
                    comp_img = comp_img_term
                else:
                    popup.clear(); popup.refresh()
                    comp_img = registry_search_popup(stdscr, comp_img_term, bar_w, pct[0], title, spinner, frame)
                    popup = curses.newwin(ph, pw, py, px)
                    popup.keypad(True)
                    stdscr.clear(); stdscr.refresh()
                if comp_img:
                    comp_stack = sel("Which stack for companion:", stacks)
                    state["companion_info"] = {"name":comp_name,"image":comp_img,"stack":comp_stack or state["target_stack"]}


            step += 1
        elif current == "netvol":
            # ── Network/Volume questions ──────────────────────────────
            pct[0] = 75
            wants_netvol = yn("Auto-create network & volume for this container?", "y")
            if wants_netvol == "y":
                net_type = sel("Network/Volume type:", [
                    "External (stored in creator/core file)",
                    "Internal (stored in this compose file)"])
                if net_type is None: step = max(0, step-1); continue
                if net_type and "External" in net_type:
                    state["external_network"] = True
                    import glob as _gl2
                    _creators = []
                    for _cf in sorted(_gl2.glob(f"{STACKS_DIR}/*.yml")):
                        try:
                            if re.search(r"container_name:\s*provisioner", open(_cf).read()):
                                _creators.append(os.path.basename(_cf).replace(".yml",""))
                        except: pass
                    _creators.append("\u2795 Create new")
                    curses.flushinp()
                    popup.clear()
                    draw_border_box(popup, 0, 0, ph, pw, f" {title[:pw-4]} ")
                    popup.refresh()
                    curses.flushinp()
                    try:
                        _chosen = sel("Add to which stack?", _creators)
                    except Exception as _se:
                        import traceback
                        _chosen = None
                    curses.flushinp()
                    state["creator_stack"] = "new" if (not _chosen or _chosen == "\u2795 Create new") else _chosen
                else:
                    state["external_network"] = False
                    state["creator_stack"] = None
                state["auto_network"] = True
                state["auto_volume"] = True
            else:
                state["auto_network"] = False
                state["auto_volume"] = False
            step += 1

        elif current == "start" or step >= len(STEPS):
            break

    # ── Build scaffold ───────────────────────────────────────────────────
    svc_name = state["svc_name"]
    image = state["image"]
    svc_ip = state["svc_ip"]
    svc_port = state["svc_port"]
    target_stack = state["target_stack"]
    db_info = state["db_info"]
    redis_info = state["redis_info"]
    companion_info = state["companion_info"]

    status("Building compose scaffold...", 80)
    import json as _json
    try:
        import sys as _s; _s.path.insert(0, '/usr/local/lib'); import stacks_config as _sc
        cfg = {k: v for k, v in _sc.load_doc('build').items() if not str(k).startswith('_')}
    except Exception:
        try: cfg = _json.load(open(os.path.join(CONF_DIR, "build.conf")))
        except: cfg = {}
    container_name = svc_name
    net_name = container_name.replace("-","_") + "_net"
    cpuset = cfg.get("cpuset","0-15")
    cpu_shares = cfg.get("cpu_shares",4096)
    stop_grace = cfg.get("stop_grace_period","120s")
    stop_signal = cfg.get("stop_signal","SIGTERM")
    restart_pol = cfg.get("restart","unless-stopped")
    dns_list = cfg.get("dns",["192.168.1.114","8.8.8.8"])
    extra_env = cfg.get("extra_env",["TZ=America/New_York"])
    extra_vols = cfg.get("extra_volumes",[])
    extra_labels = cfg.get("extra_labels",[])
    do_blkio = cfg.get("blkio",True)
    do_ulimits = cfg.get("ulimits",True)
    do_deploy = cfg.get("deploy_limits",True)
    do_logging = cfg.get("logging",True)
    sab_group = cfg.get("sablier_group","") or "srvs"
    sab_enable = cfg.get("sablier_enable",True)
    log_driver = cfg.get("log_driver","json-file")
    log_max_size = cfg.get("log_max_size","10m")
    log_max_file = cfg.get("log_max_file","3")
    blkio_read = cfg.get("blkio_read_rate","500mb")
    blkio_write = cfg.get("blkio_write_rate","500mb")
    storage_size = cfg.get("storage_opt_size","10G")
    mem_limit = cfg.get("deploy_memory_limit","1G")
    cpu_limit = cfg.get("deploy_cpu_limit","0.2")
    pids_limit = cfg.get("deploy_pids_limit",1000)
    mem_res = cfg.get("deploy_memory_reservation","100M")
    domain = "example.com"

    def count_services_in_stack(fpath):
        try:
            c = open(fpath).read()
            return len(re.findall(r"^    container_name:", c, re.MULTILINE)) + 1
        except: return 1

    def load_service_desc(svc):
        default_desc = "A powerful service running on StacksServer. Edit this description in the descriptions config."
        try:
            for line in open(os.path.join(CONF_DIR, "stacks.conf")):
                l = line.strip()
                if l.startswith("BUILD_DEFAULT_DESC="): default_desc = l.split("=",1)[1].strip('" ')
        except: pass
        desc_dir = os.path.expanduser("~/.config/stacks/descriptions")
        desc_file = os.path.join(desc_dir, f"{target_stack}.conf")
        if os.path.exists(desc_file):
            try:
                content = open(desc_file).read()
                m = re.search(rf"^{re.escape(svc)}\s*\n((?:#[^\n]*\n)+)", content, re.MULTILINE)
                if m: return m.group(1).rstrip()
            except: pass
        return f"# {default_desc}"

    svc_num = count_services_in_stack(os.path.join(STACKS_DIR, target_stack + ".yml"))
    svc_desc = load_service_desc(svc_name)

    # BARE container - just image, network, volume. Fix adds everything else.
    bl = []
    bl.append(f"  {svc_name}:")
    bl.append(f"    image: {image}")
    bl.append(f"    container_name: {container_name}")
    bl.append("    networks:")
    bl.append(f"      {net_name}:")
    if svc_ip:
        bl.append(f"        ipv4_address: {svc_ip}")
    bl.append("      traefik_net:")
    bl.append("        priority: 1000")
    bl.append("    volumes:")
    bl.append(f"      - /srv/stacks/docker/{svc_name}/config:/config")

    # ── Add DB companion service if selected ──────────────────
    fpath = os.path.join(STACKS_DIR, target_stack + ".yml")
    if db_info and db_info.get("type") and db_info["type"] != "none":
        _dt = db_info["type"]
        _dn = db_info["name"]
        _dp = db_info.get("pass","changeme")
        _dd = db_info.get("db", svc_name.replace("-","_"))
        _dnet = net_name
        _dip = db_info.get("ip","")
        dbl = []
        dbl.append(f"  # DB: {_dn} ({_dt})")
        dbl.append(f"  {_dn}:")
        if cfg.get("use_common_caps",True): dbl.append("    <<: *common-caps")
        if _dt == "postgres":
            dbl.append("    image: postgres:16-alpine")
            dbl.append(f"    container_name: {_dn}")
            dbl.append(f"    hostname: {_dn}")
            dbl.append(f'    environment:\n      - POSTGRES_PASSWORD={_dp}\n      - POSTGRES_DB={_dd}\n      - POSTGRES_USER=postgres')
        elif _dt == "mysql" or _dt == "mariadb":
            img = "mariadb:11" if _dt=="mariadb" else "mysql:8"
            dbl.append(f"    image: {img}")
            dbl.append(f"    container_name: {_dn}")
            dbl.append(f"    hostname: {_dn}")
            dbl.append(f'    environment:\n      - MYSQL_ROOT_PASSWORD={_dp}\n      - MYSQL_DATABASE={_dd}\n      - MYSQL_USER=dbuser\n      - MYSQL_PASSWORD={_dp}')
        elif _dt == "mongo":
            dbl.append(f"    image: mongo:7")
            dbl.append(f"    container_name: {_dn}")
            dbl.append(f"    hostname: {_dn}")
            dbl.append(f'    environment:\n      - MONGO_INITDB_ROOT_USERNAME=admin\n      - MONGO_INITDB_ROOT_PASSWORD={_dp}\n      - MONGO_INITDB_DATABASE={_dd}')
        elif _dt == "redis":
            dbl.append(f"    image: redis:7-alpine")
            dbl.append(f"    container_name: {_dn}")
            dbl.append(f"    hostname: {_dn}")
        dbl.append(f'    cpuset: "{cpuset}"')
        dbl.append(f"    cpu_shares: {cpu_shares}")
        dbl.append(f"    restart: {restart_pol}")
        dbl.append("    networks:")
        dbl.append(f"      {_dnet}:")
        if _dip: dbl.append(f"        ipv4_address: {_dip}")
        dbl.append(f"    volumes:")
        _vol_name = f"{_dn}_data"
        dbl.append(f"      - {_vol_name}:/var/lib/{_dt}/data" if _dt not in ("redis","mongo") else f"      - {_vol_name}:/data")
        # Write DB to its target stack, not main stack
        _db_stack = db_info.get("stack") or target_stack
        _db_fpath = os.path.join(STACKS_DIR, _db_stack + ".yml")
        _db_block = "\n".join(dbl) + "\n"
        if _db_fpath == fpath:
            # Same file - append to main bl
            bl.append("")
            bl.extend(dbl)
        else:
            # Different file - inject into that stack
            try:
                _db_content = open(_db_fpath).read() if os.path.exists(_db_fpath) else f"name: {_db_stack}\nservices:\n"
                if "##STACKS_ART_START_FOOTER" in _db_content:
                    _db_content = _db_content.replace("##STACKS_ART_START_FOOTER", _db_block + "\n##STACKS_ART_START_FOOTER", 1)
                else:
                    _db_lines = _db_content.splitlines(keepends=True)
                    _ins = len(_db_lines)
                    for _di in range(len(_db_lines)-1,-1,-1):
                        if not _db_lines[_di].startswith("#") and _db_lines[_di].strip():
                            _ins = _di+1; break
                    _db_lines.insert(_ins, _db_block + "\n")
                    _db_content = "".join(_db_lines)
                open(_db_fpath, "w").write(_db_content)
            except Exception as _dbe:
                bl.append("")
                bl.extend(dbl)  # fallback to main stack

    block = "\n".join(bl) + "\n"

    # Inject into stack
    fpath = os.path.join(STACKS_DIR, target_stack + ".yml")
    try:
        fcontent = open(fpath).read()
        if "##STACKS_ART_START_FOOTER" in fcontent:
            fcontent = fcontent.replace("##STACKS_ART_START_FOOTER", block + "\n##STACKS_ART_START_FOOTER", 1)
        else:
            lines_f = fcontent.splitlines(keepends=True)
            insert = len(lines_f)
            for i in range(len(lines_f)-1,-1,-1):
                if not lines_f[i].startswith("#") and lines_f[i].strip():
                    insert = i+1; break
            lines_f.insert(insert, block+"\n")
            fcontent = "".join(lines_f)
        open(fpath,"w").write(fcontent)
    except Exception as e:
        import traceback
        err = traceback.format_exc()
        popup.clear()
        draw_border_box(popup, 0, 0, ph, pw, " Build Error ")
        lines_err = err.strip().split("\n")
        for ei, el in enumerate(lines_err[-8:]):
            try: popup.addstr(2+ei, 2, el[:pw-4], curses.color_pair(C_RED))
            except: pass
        try: popup.addstr(ph-2, 2, "Press any key", curses.color_pair(C_DIM))
        except: pass
        popup.nodelay(False)
        popup.refresh()
        popup.getch()
        return

    # Log the build
    try:
        import datetime as _dt
        with open("/srv/stacks/stacks_build.log", "a") as _bl:
            _bl.write(f"\n=== Wizard Build: {_dt.datetime.now()} ===\n")
            _bl.write(f"  Service:    {svc_name}\n")
            _bl.write(f"  Image:      {image}\n")
            _bl.write(f"  Stack:      {target_stack}\n")
            _bl.write(f"  IP:         {svc_ip}\n")
            _bl.write(f"  Port:       {svc_port}\n")
            if db_info: _bl.write(f"  DB:         {db_info.get('type')} ({db_info.get('name')})\n")
            _bl.write(f"  Injected:   {fpath}\n")
    except: pass

    # Write to per-stack descriptions file
    try:
        default_desc = "A powerful service running on StacksServer. Edit this description."
        for line in open(os.path.join(CONF_DIR, "stacks.conf")):
            l = line.strip()
            if l.startswith("BUILD_DEFAULT_DESC="): default_desc = l.split("=",1)[1].strip('" ')
        desc_dir = os.path.expanduser("~/.config/stacks/descriptions")
        os.makedirs(desc_dir, exist_ok=True)
        desc_file = os.path.join(desc_dir, f"{target_stack}.conf")
        try: existing = open(desc_file).read()
        except: existing = f"# {target_stack} — Service Descriptions\n# Edit the description under each service name.\n#\n"
        import re as _re2
        if not _re2.search(rf"^{_re2.escape(svc_name)}\s*$", existing, _re2.MULTILINE):
            entry = f"\n{svc_name}\n# {default_desc}\n"
            with open(desc_file, "a") as df: df.write(entry)
    except: pass

    # Auto-sync
    try:
        import importlib.util as _ilu
        spec = _ilu.spec_from_file_location("stacks_sync", "/usr/local/lib/stacks_sync.py")
        mod = _ilu.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.main()
    except: pass

    # Step 9: Start?
    pct[0] = 95
    start_action = sel("Start now?", [
        f"Start just {container_name}",
        f"Start whole stack: {target_stack}",
        f"Pull image only: {image}",
        "Don't start yet",
    ])
    if start_action and "Don't" not in start_action:
        if "whole stack" in start_action:
            run_log_popup(stdscr, f"Up {target_stack}", f"{STACKS_BIN} up {target_stack}")
        elif "Pull image" in start_action:
            run_log_popup(stdscr, f"Pull {image}", f"docker pull {image}")
        else:
            run_log_popup(stdscr, f"Start {container_name}", f"docker compose -f {fpath} up -d {svc_name}")

    # Auto-inject network and volume after build
    if state.get("auto_network") or state.get("auto_volume"):
        try:
            import sys as _sys2
            _sys2.path.insert(0, '/usr/local/lib')
            from stacks_fix import post_build_inject, load_conf as _lc
            _cfg = _lc()
            # Pass wizard choices directly into cfg
            _cfg["BUILD_AUTO_NETWORK"] = "1" if state.get("auto_network") else "0"
            _cfg["BUILD_AUTO_VOLUME"] = "1" if state.get("auto_volume") else "0"
            if state.get("external_network") is False:
                _cfg["FIX_EXTERNAL_NETWORKS"] = "0"
            if state.get("creator_stack") == "new":
                _cfg["FIX_FORCE_CREATE_CREATOR"] = "1"
            elif state.get("creator_stack"):
                _cfg["FIX_CREATOR_TARGET"] = state["creator_stack"]
            _notes = post_build_inject(fpath, svc_name, _cfg)
        except Exception as _pbe:
            pass  # non-fatal
        pass  # non-fatal

    # Done - clear everything first
    stdscr.clear(); stdscr.refresh()
    pct[0] = 100
    popup = curses.newwin(ph, pw, py, px)
    popup.keypad(True)
    popup.clear()
    draw_border_box(popup, 0, 0, ph, pw, f" {title[:pw-4]} ")
    bar = "█" * bar_w
    try: popup.addstr(2, 2, f"[{bar}]", curses.color_pair(C_CYAN))
    except: pass
    try: popup.addstr(4, 3, f"✔ {container_name} added to {target_stack}!", curses.color_pair(C_GREEN))
    except: pass
    try: popup.addstr(5, 3, f"  Image:  {image[:pw-12]}", curses.color_pair(C_DIM))
    except: pass
    try: popup.addstr(6, 3, f"  IP:     {svc_ip}  Port: {svc_port}", curses.color_pair(C_DIM))
    except: pass
    try: popup.addstr(8, 3, "Press any key", curses.color_pair(C_DIM))
    except: pass
    popup.refresh()
    popup.getch()

def _bw_input(popup, pw, ph, prompt, default, bar_w, pct, title, spinner, frame):
    """Single line text input inside popup."""
    try:
        popup.nodelay(False)  # MUST block - no auto-advance
        popup.timeout(-1)
        popup.clear()
        draw_border_box(popup, 0, 0, ph, pw, f" {title[:pw-4]} ")
        filled = int(bar_w * pct / 100)
        bar = "█" * filled + "░" * (bar_w - filled)
        try: popup.addstr(2, 2, f"[{bar}]", curses.color_pair(C_CYAN))
        except: pass
        sp = spinner[frame % len(spinner)]
        try: popup.addstr(3, 3, f"{sp} {pct}%", curses.color_pair(C_YELLOW))
        except: pass
        try: popup.addstr(5, 3, prompt[:pw-6], curses.color_pair(C_ACCENT))
        except: pass
        if default:
            try: popup.addstr(6, 3, f"default: {default[:pw-14]}", curses.color_pair(C_DIM))
            except: pass
        try: popup.addstr(7, 3, "> ", curses.color_pair(C_NORMAL))
        except: pass
        popup.refresh()
        curses.curs_set(1)
        # Manual input loop - ignores resize/mouse events
        val = []
        popup.nodelay(True)
        # Drain all buffered keys
        while popup.getch() != -1: pass
        popup.nodelay(False)
        popup.timeout(-1)
        while True:
            ch = popup.getch()
            if ch == curses.KEY_RESIZE: continue  # ignore keyboard popup
            if ch == curses.KEY_MOUSE: continue
            if ch in (10, 13):  # Enter
                break
            elif ch == 27:  # ESC
                val = []
                break
            elif ch in (curses.KEY_BACKSPACE, 127, 8):
                if val: val.pop()
            elif 32 <= ch <= 126:
                val.append(chr(ch))
            # Redraw input line
            try:
                popup.addstr(7, 3, "> " + "".join(val) + " " * (pw-12), curses.color_pair(C_NORMAL))
                popup.move(7, 5 + len(val))
            except: pass
            popup.refresh()
        curses.curs_set(0)
        result = "".join(val).strip()
        return result if result else default
    except:
        curses.curs_set(0)
        return None  # any error = treat as ESC/back


def _bw_select(popup, pw, ph, prompt, items, bar_w, pct, title, spinner, frame):
    """Scrollable list selection inside popup."""
    if not items: return None
    # Add cancel option
    items = list(items) + ["✕  Cancel"]
    sel = 0; scroll = 0
    visible = ph - 6
    popup.nodelay(False)
    while True:
        popup.clear()
        draw_border_box(popup, 0, 0, ph, pw, f" {title[:pw-4]} ")
        filled = int(bar_w * pct / 100)
        bar = "█" * filled + "░" * (bar_w - filled)
        try: popup.addstr(2, 2, f"[{bar}]", curses.color_pair(C_CYAN))
        except: pass
        try: popup.addstr(3, 3, prompt[:pw-6], curses.color_pair(C_ACCENT))
        except: pass
        for i in range(min(visible, len(items))):
            idx = scroll + i
            if idx >= len(items): break
            y = 4 + i
            label = str(items[idx])[:pw-6]
            if idx == sel:
                try: popup.addstr(y, 2, f" ▶ {label:<{pw-6}}", curses.color_pair(C_SELECTED))
                except: pass
            else:
                try: popup.addstr(y, 2, f"   {label:<{pw-6}}", curses.color_pair(C_NORMAL))
                except: pass
        popup.refresh()
        try:
            k = popup.getch()
        except curses.error:
            k = curses.KEY_RESIZE
        if k == curses.KEY_RESIZE: continue
        if k == curses.KEY_MOUSE: continue
        if k == curses.KEY_LEFT: return None  # left arrow = go back
        if k == curses.KEY_UP:
            if sel > 0: sel -= 1
            if sel < scroll: scroll = sel
        elif k == curses.KEY_DOWN:
            if sel < len(items)-1: sel += 1
            if sel >= scroll + visible: scroll = sel - visible + 1
        elif k in (10, 13):
            if items[sel] == "✕  Cancel": return None
            return items[sel]
        elif k in (27, 3, curses.KEY_F1): return None


def _bw_yesno(popup, pw, ph, prompt, default, bar_w, pct, title, spinner, frame):
    """Yes/No selection inside popup."""
    sel = 0 if default.lower() == "y" else 1
    popup.nodelay(False)
    while True:
        popup.clear()
        draw_border_box(popup, 0, 0, ph, pw, f" {title[:pw-4]} ")
        filled = int(bar_w * pct / 100)
        bar = "█" * filled + "░" * (bar_w - filled)
        try: popup.addstr(2, 2, f"[{bar}]", curses.color_pair(C_CYAN))
        except: pass
        sp = spinner[frame % len(spinner)]
        try: popup.addstr(3, 3, f"{sp} {pct}%", curses.color_pair(C_YELLOW))
        except: pass
        try: popup.addstr(5, 3, prompt[:pw-6], curses.color_pair(C_ACCENT))
        except: pass
        yes_attr = curses.color_pair(C_SELECTED) if sel==0 else curses.color_pair(C_NORMAL)
        no_attr  = curses.color_pair(C_SELECTED) if sel==1 else curses.color_pair(C_NORMAL)
        try: popup.addstr(7, 6,  "  YES  ", yes_attr)
        except: pass
        try: popup.addstr(7, 16, "  NO   ", no_attr)
        except: pass
        try: popup.addstr(ph-2, 2, "←→ Select  ENTER confirm  ESC/Ctrl+C cancel", curses.color_pair(C_DIM))
        except: pass
        popup.refresh()
        try:
            k = popup.getch()
        except curses.error:
            k = curses.KEY_RESIZE
        if k == curses.KEY_RESIZE: continue
        if k == curses.KEY_MOUSE: continue
        if k == curses.KEY_LEFT and sel == 0: return None  # left on YES = go back
        if k in (curses.KEY_LEFT, curses.KEY_RIGHT): sel = 1 - sel
        elif k in (10, 13): return "y" if sel==0 else "n"
        elif k in (ord("y"), ord("Y")): return "y"
        elif k in (ord("n"), ord("N")): return "n"
        elif k == 27: return None
        elif k == 3: return None  # Ctrl+C = back
        elif k == curses.KEY_F1: return None


def _bw_status(popup, pw, ph, msg, bar_w, pct, title, spinner, frame):
    popup.clear()
    draw_border_box(popup, 0, 0, ph, pw, f" {title[:pw-4]} ")
    filled = int(bar_w * pct / 100)
    bar = "█" * filled + "░" * (bar_w - filled)
    try: popup.addstr(2, 2, f"[{bar}]", curses.color_pair(C_CYAN))
    except: pass
    sp = spinner[frame % len(spinner)]
    try: popup.addstr(3, 3, f"{sp} {msg[:pw-6]}", curses.color_pair(C_YELLOW))
    except: pass
    popup.refresh()



# ── Registry search - reusable curses image picker ───────────────────────────
def _load_registry_searchers():
    """Load all registry search functions from stacks_search.py."""
    import importlib.util as _ilu
    spec = _ilu.spec_from_file_location("stacks_search", "/usr/local/lib/stacks_search.py")
    mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.REGISTRIES, mod.search_all

def run_sequence_popup(stdscr, title, steps):
    import time as _t
    h,w=stdscr.getmaxyx()
    pw=min(w-6,70); ph=9; py=(h-ph)//2; px=(w-pw)//2
    popup=curses.newwin(ph,pw,py,px)
    popup.nodelay(True)
    try: curses.mousemask(0)
    except: pass
    bar_w=pw-6; frame=0; total=len(steps)
    spinner="⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    last_log=[""]
    def draw(idx,slabel,done=False):
        try:
            popup.clear()
            draw_border_box(popup,0,0,ph,pw,f" {title[:pw-4]} ")
            if done: pct=100
            else: pct=min(99,int((idx/total)*99)+1)
            filled=int(bar_w*pct/100)
            bar="█"*filled+"░"*(bar_w-filled)
            # Log line - clean text only
            log=last_log[0][:pw-6]
            try: popup.addstr(2,3,log,curses.color_pair(C_DIM))
            except: pass
            try: popup.addstr(3,2,f"[{bar}]",curses.color_pair(C_CYAN))
            except: pass
            if done:
                try: popup.addstr(4,3,"✔ All done — press any key",curses.color_pair(C_GREEN))
                except: pass
            else:
                sp=spinner[frame%len(spinner)]
                try: popup.addstr(4,3,f"{sp} Step {idx+1}/{total}: {slabel}  {pct}%",curses.color_pair(C_YELLOW))
                except: pass
            popup.refresh()
        except: pass
    stdscr.clear(); stdscr.refresh()
    cancelled=False
    LOG_DIR = "/srv/stacks"
    import glob as _g
    # Get all stacks log files and their sizes before starting
    def get_log_positions():
        files = sorted(_g.glob(f"{LOG_DIR}/stacks_*.log"))
        pos = {}
        for f in files:
            try: pos[f] = os.path.getsize(f)
            except: pos[f] = 0
        return pos
    def read_new_log_lines(positions):
        lines = []
        for f, p in list(positions.items()):
            try:
                with open(f, "rb") as lf:
                    lf.seek(p)
                    for raw in lf:
                        positions[f] += len(raw)
                        cleaned = clean_log_line(raw.decode("utf-8","ignore"))
                        if cleaned: lines.append(cleaned)
            except: pass
        return lines
    for i,(slabel,cmd) in enumerate(steps):
        draw(i,slabel)
        log_positions = get_log_positions()
        proc=subprocess.Popen(cmd,shell=True,
            stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        log_positions = get_log_positions()
        try:
            while proc.poll() is None:
                new_lines = read_new_log_lines(log_positions)
                if new_lines:
                    last_log[0] = new_lines[-1]
                frame += 1
                draw(i, slabel)
                _t.sleep(0.1)
                try:
                    k = popup.getch()
                except curses.error:
                    k = curses.KEY_RESIZE
                if k == curses.KEY_MOUSE: continue
                if k == 27: proc.terminate(); cancelled=True; break
        except KeyboardInterrupt: proc.terminate(); cancelled=True
        proc.wait()
        if cancelled: break
    if not cancelled:
        draw(total,"",done=True); popup.nodelay(False); popup.getch()

def run_cmd_silent(stdscr, title, cmd):
    run_log_popup(stdscr, title, cmd)

GLOBAL_ACTIONS = [
    ("▶  Up ALL stacks",                      "up_all"),
    ("■  Down ALL stacks",                    "down_all"),
    ("↺  Restart ALL stacks",                 "restart_all"),
    ("⟳  Recreate ALL stacks",                "recreate_all"),
    ("✦  Fix ALL stacks",                     "fix_all"),
    ("◈  Repair + Recreate + Up ALL",         "repair_recreate_up_all"),
    ("◉  Full: Repair+Fix+Recreate+Up ALL",   "full_repair_all"),
    ("↑  Scale ON all",                       "scale_on_all"),
    ("↓  Scale OFF all",                      "scale_off_all"),
    ("↑  Proxy ON all",                       "proxy_on_all"),
    ("↓  Proxy OFF all",                      "proxy_off_all"),
    ("✕  Cancel",                             None),
]

STACK_ACTIONS = [
    ("▶  Start",                              "up"),
    ("■  Stop",                               "down"),
    ("↺  Restart",                            "restart"),
    ("⟳  Recreate",                           "recreate"),
    ("✦  Fix",                                "fix"),
    ("✦  Repair",                             "repair"),
    ("⟳  Recreate + Up",                      "recreate_up"),
    ("◈  Repair + Recreate + Up",             "recreate_repair"),
    ("★  Fix + Repair + Recreate + Up",       "full_repair"),
    ("◉  Repair + Fix + Recreate + Up",       "deep_repair"),
    ("↑  Scale ON",                           "scale_on"),
    ("↓  Scale OFF",                          "scale_off"),
    ("↑  Proxy ON",                           "proxy_on"),
    ("↓  Proxy OFF",                          "proxy_off"),
    ("🎨  Art Inject",                          "art_inject"),
    ("🧹  Art Strip",                           "art_strip"),
    ("✕  Cancel",                             None),
]

CONTAINER_ACTIONS = [
    ("▶  Start",                              "start"),
    ("■  Stop",                               "stop"),
    ("↺  Restart",                            "restart"),
    ("⟳  Recreate",                           "recreate"),
    ("⟳  Recreate + Up",                      "recreate_up"),
    ("✦  Fix (stack)",                        "fix"),
    ("✦  Repair (stack)",                     "repair"),
    ("✦  Fix + Repair (stack)",               "fix_repair"),
    ("◐  Fix + Recreate + Up",                "fix_recreate"),
    ("◓  Repair + Recreate + Up",             "repair_recreate"),
    ("★  Fix + Repair + Recreate + Up",       "full_repair"),
    ("◉  Repair + Fix + Recreate + Up",       "deep_repair"),
    ("↑  Scale ON",                           "scale_on"),
    ("↓  Scale OFF",                          "scale_off"),
    ("↑  Proxy ON",                           "proxy_on"),
    ("↓  Proxy OFF",                          "proxy_off"),
    ("🔍  Inspect",                           "inspect"),
    ("🌐  Edit IP",                           "edit_ip"),
    ("✕  Cancel",                             None),
]

def do_global_action(stdscr, action):
    if action is None: return
    if action == 'up_all':
        run_log_popup(stdscr, 'Up ALL', f'{STACKS_BIN} up')
    elif action == 'down_all':
        run_log_popup(stdscr, 'Down ALL', f'{STACKS_BIN} down')
    elif action == 'restart_all':
        run_log_popup(stdscr, 'Restart ALL', f'{STACKS_BIN} restart')
    elif action == 'recreate_all':
        run_log_popup(stdscr, 'Recreate ALL', f'{STACKS_BIN} up recreate')
    elif action == 'fix_all':
        run_log_popup(stdscr, 'Fix ALL', f'{STACKS_BIN} fix all')
    elif action == 'repair_recreate_up_all':
        run_sequence_popup(stdscr, 'Repair+Recreate+Up ALL', [
            ('Repair',   f'python3 /usr/local/lib/stacks_repair.py {STACKS_DIR}'),
            ('Recreate', f'{STACKS_BIN} up recreate'),
            ('Up',       f'{STACKS_BIN} up'),
        ])
    elif action == 'full_repair_all':
        run_cmd_silent(stdscr, 'Repair ALL', f'python3 /usr/local/lib/stacks_repair.py {STACKS_DIR}')
        run_cmd_silent(stdscr, 'Fix ALL', f'{STACKS_BIN} fix all')
        run_log_popup(stdscr, 'Up ALL', f'{STACKS_BIN} up')
    elif action == 'scale_on_all':
        run_log_popup(stdscr, 'Scale ON all', f'{STACKS_BIN} scale on')
    elif action == 'scale_off_all':
        run_log_popup(stdscr, 'Scale OFF all', f'{STACKS_BIN} scale off')
    elif action == 'proxy_on_all':
        run_log_popup(stdscr, 'Proxy ON all', f'{STACKS_BIN} proxy on')
    elif action == 'proxy_off_all':
        run_log_popup(stdscr, 'Proxy OFF all', f'{STACKS_BIN} proxy off')

def do_stack_action(stdscr, stack_name, action):
    if action is None: return
    if action == 'up':
        cmd = f'{STACKS_BIN} up {stack_name}'
    elif action == 'down':
        cmd = f'{STACKS_BIN} down {stack_name}'
    elif action == 'restart':
        cmd = f'{STACKS_BIN} restart {stack_name}'
    elif action == 'recreate':
        cmd = f'{STACKS_BIN} up {stack_name} recreate'
    elif action == 'fix':
        cmd = f'{STACKS_BIN} fix {stack_name}'
    elif action == 'repair':
        cmd = f'{STACKS_BIN} fix {stack_name} repair'
    elif action == 'scale_on':
        cmd = f'{STACKS_BIN} scale {stack_name} on'
    elif action == 'scale_off':
        cmd = f'{STACKS_BIN} scale {stack_name} off'
    elif action == 'proxy_on':
        cmd = f'{STACKS_BIN} proxy {stack_name} on'
    elif action == 'proxy_off':
        cmd = f'{STACKS_BIN} proxy {stack_name} off'
    elif action == 'recreate_up':
        run_cmd_silent(stdscr, f'Recreate {stack_name}', f'{STACKS_BIN} up {stack_name} recreate')
        run_log_popup(stdscr, f'Up {stack_name}', f'{STACKS_BIN} up {stack_name}')
        return
    elif action == 'full_repair':
        # Fix + Repair + Recreate + Up
        run_log_popup(stdscr, f'Fix → {stack_name}',
                     f'{STACKS_BIN} fix {stack_name}')
        run_log_popup(stdscr, f'Repair → {stack_name}',
                     f'python3 /usr/local/lib/stacks_repair.py {STACKS_DIR}/{stack_name}.yml')
        run_log_popup(stdscr, f'Recreate → {stack_name}',
                     f'{STACKS_BIN} up {stack_name} recreate')
        run_log_popup(stdscr, f'Up → {stack_name}',
                     f'{STACKS_BIN} up {stack_name}')
        return
    elif action == 'recreate_repair':
        # Recreate + Repair
        run_log_popup(stdscr, f'Repair → {stack_name}',
                     f'python3 /usr/local/lib/stacks_repair.py {STACKS_DIR}/{stack_name}.yml')
        run_log_popup(stdscr, f'Recreate → {stack_name}',
                     f'{STACKS_BIN} up {stack_name} recreate')
        return
    elif action == 'deep_repair':
        # Repair + Recreate + Fix + Up
        run_log_popup(stdscr, f'Repair → {stack_name}',
                     f'python3 /usr/local/lib/stacks_repair.py {STACKS_DIR}/{stack_name}.yml')
        run_log_popup(stdscr, f'Fix → {stack_name}',
                     f'{STACKS_BIN} fix {stack_name}')
        run_log_popup(stdscr, f'Recreate → {stack_name}',
                     f'{STACKS_BIN} up {stack_name} recreate')
        run_log_popup(stdscr, f'Up → {stack_name}',
                     f'{STACKS_BIN} up {stack_name}')
        return
    elif action == 'art_inject':
        run_log_popup(stdscr, f'Art inject: {stack_name}',
            f'{STACKS_BIN} art inject {stack_name}')
        return
    elif action == 'art_strip':
        run_log_popup(stdscr, f'Art strip: {stack_name}',
            f'{STACKS_BIN} art strip {stack_name}')
        return
    else: return
    run_log_popup(stdscr, f'{action} → {stack_name}', cmd)

def _show_container_inspect(stdscr, name):
    """Show container inspect info in a scrollable popup."""
    try:
        r = subprocess.run(['docker','inspect','--format',
            '''ID: {{.Id[:12]}}
Image: {{.Config.Image}}
Status: {{.State.Status}}
Started: {{.State.StartedAt}}
IP: {{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}
Ports: {{range $p,$b := .NetworkSettings.Ports}}{{$p}} {{end}}
CPU: {{.HostConfig.CpusetCpus}}
Memory: {{.HostConfig.Memory}}
Restart: {{.HostConfig.RestartPolicy.Name}}
Mounts: {{len .Mounts}} volumes''',
            name], capture_output=True, text=True, timeout=5)
        info = r.stdout.strip() if r.returncode==0 else r.stderr.strip()
    except Exception as e:
        info = str(e)
    h, w = stdscr.getmaxyx()
    pw = min(w-4, 70); ph = min(h-4, 18)
    py = (h-ph)//2; px = (w-pw)//2
    popup = curses.newwin(ph, pw, py, px)
    popup.keypad(True)
    popup.nodelay(False)
    stdscr.clear(); stdscr.refresh()
    lines = info.split("\n")
    scroll = 0
    while True:
        popup.clear()
        draw_border_box(popup, 0, 0, ph, pw, f" Inspect: {name[:pw-12]} ")
        visible = ph - 4
        for i, l in enumerate(lines[scroll:scroll+visible]):
            try: popup.addstr(2+i, 2, l[:pw-4], curses.color_pair(C_NORMAL))
            except: pass
        try: popup.addstr(ph-2, 2, "↑↓ Scroll  ENTER Select  ESC/← Cancel"[:pw-4], curses.color_pair(C_DIM))
        except: pass
        popup.refresh()
        try:
            k = popup.getch()
        except curses.error:
            k = curses.KEY_RESIZE
        if k == curses.KEY_RESIZE: continue
        if k == curses.KEY_MOUSE: continue
        if k == curses.KEY_LEFT: return None  # left arrow = go back
        if k == curses.KEY_UP: scroll = max(0, scroll-1)
        elif k == curses.KEY_DOWN: scroll = min(max(0,len(lines)-visible), scroll+1)
        elif k in (27, ord('q')): break


def _get_container_ip(cname):
    """Current IP for a container from ip_assignments.conf (container=IP[:port])."""
    try:
        for line in open(os.path.expanduser('~/.config/stacks/ip_assignments.conf')):
            line=line.strip()
            if line.startswith(cname+'='):
                return line.split('=',1)[1].split(':')[0].strip()
    except Exception: pass
    return ''

def _apply_container_ip(stack_file, cname, old_ip, new_ip):
    """Replace old_ip->new_ip inside cname's service block in the compose, and
    update ip_assignments.conf. Caller recreates the container afterward."""
    try:
        ap=os.path.expanduser('~/.config/stacks/ip_assignments.conf')
        lines=open(ap).read().splitlines() if os.path.exists(ap) else []
        out=[]; found=False
        for l in lines:
            if l.strip().startswith(cname+'='):
                rest=l.split('=',1)[1]; port=rest.split(':',1)[1] if ':' in rest else ''
                out.append(f"{cname}={new_ip}"+(f":{port}" if port else "")); found=True
            else: out.append(l)
        if not found: out.append(f"{cname}={new_ip}")
        open(ap,'w').write("\n".join(out)+"\n")
    except Exception: pass
    if not stack_file or not old_ip: return
    try:
        lines=open(stack_file,encoding='utf-8').read().split('\n')
        cn=None
        for i,l in enumerate(lines):
            if re.match(r'^\s*container_name:\s*"?'+re.escape(cname)+r'"?\s*$', l): cn=i; break
        if cn is None: return
        skey=cn
        while skey>0 and not re.match(r'^  [A-Za-z0-9_.-]+:\s*$', lines[skey]): skey-=1
        end=skey+1
        while end<len(lines) and not re.match(r'^  [A-Za-z0-9_.-]+:\s*$', lines[end]): end+=1
        for j in range(skey,end): lines[j]=lines[j].replace(old_ip,new_ip)
        open(stack_file,'w',encoding='utf-8').write("\n".join(lines))
    except Exception: pass

def _prompt_text(stdscr, title, prompt, default=''):
    """Centered curses single-line text input. Returns string ('' on cancel)."""
    h,w=stdscr.getmaxyx()
    pw=max(len(title)+6,len(prompt)+30,46); ph=6
    win=curses.newwin(ph,pw,(h-ph)//2,(w-pw)//2); win.keypad(True)
    buf=list(default); curses.curs_set(1)
    try:
        while True:
            win.clear()
            try: draw_border_box(win,0,0,ph,pw,title[:pw-4])
            except Exception: pass
            try: win.addstr(2,2,prompt[:pw-4],curses.color_pair(C_ACCENT))
            except Exception: pass
            try: win.addstr(3,2,('> '+''.join(buf))[:pw-4],curses.color_pair(C_NORMAL))
            except Exception: pass
            try: win.addstr(4,2,'Enter = save    Esc = cancel'[:pw-4],curses.color_pair(C_DIM))
            except Exception: pass
            win.refresh()
            k=win.getch()
            if k in (10,13): return ''.join(buf).strip()
            if k==27: return ''
            if k in (curses.KEY_BACKSPACE,127,8):
                if buf: buf.pop()
            elif 32<=k<127: buf.append(chr(k))
    finally:
        curses.curs_set(0)

def show_message_box(stdscr, title, lines):
    """Scrollable read-only message popup. Any key / ESC closes."""
    h, w = stdscr.getmaxyx()
    pw = min(w-4, max(46, max((len(l) for l in lines), default=0) + 6))
    ph = min(h-4, len(lines) + 5)
    win = curses.newwin(ph, pw, (h-ph)//2, (w-pw)//2); win.keypad(True)
    scroll = 0
    body_h = ph - 4
    while True:
        win.clear()
        try: draw_border_box(win, 0, 0, ph, pw, title[:pw-4])
        except Exception: pass
        for i, l in enumerate(lines[scroll:scroll+body_h]):
            try: win.addstr(2+i, 2, l[:pw-4], curses.color_pair(C_NORMAL))
            except Exception: pass
        try: win.addstr(ph-2, 2, "↑↓ scroll   any key / ESC close"[:pw-4], curses.color_pair(C_DIM))
        except Exception: pass
        win.refresh()
        k = win.getch()
        if k == curses.KEY_UP: scroll = max(0, scroll-1)
        elif k == curses.KEY_DOWN: scroll = min(max(0, len(lines)-body_h), scroll+1)
        else: break

def show_update_detail(stdscr, row):
    """Detail popup for an Updates-tab row: image, stacks, old vs new digests."""
    import time as _t
    lines = []
    lines.append(f"Image:   {row.get('image','')}")
    if row.get("tag"):    lines.append(f"Tag:     {row.get('tag','')}")
    stks = row.get("stacks", [])
    if stks:              lines.append(f"Stacks:  {', '.join(stks)}")
    if row["kind"] == "update":
        lines.append("Status:  UPDATE AVAILABLE")
    else:
        lines.append(f"Event:   {row.get('event','')}")
        lines.append(f"When:    {_t.strftime('%Y-%m-%d %H:%M:%S', _t.localtime(row.get('ts',0)))}")
    lines.append("")
    lines.append("OLD (was):")
    lines.append(f"  {row.get('old','') or '—'}")
    lines.append("NEW (now):")
    lines.append(f"  {row.get('new','') or '—'}")
    show_message_box(stdscr, "Update detail", lines)

def do_container_action(stdscr, container_name, stack_file, action):

    curses.flushinp()
    if action is None: return
    stack_name = os.path.basename(stack_file).replace('.yml','') if stack_file else ''
    if action == 'start':
        cmd = f'docker start {container_name}'
    elif action == 'stop':
        cmd = f'docker stop {container_name}'
    elif action == 'restart':
        cmd = f'docker restart {container_name}'
    elif action == 'recreate':
        if stack_name:
            cmd = f'{STACKS_BIN} up {stack_name} {container_name} recreate'
        else:
            cmd = f'docker restart {container_name}'
    elif action == 'scale_on':
        if stack_name:
            cmd = f'{STACKS_BIN} scale {stack_name} {container_name} on'
        else: return
    elif action == 'scale_off':
        if stack_name:
            cmd = f'{STACKS_BIN} scale {stack_name} {container_name} off'
        else: return
    elif action == 'proxy_on':
        if stack_name:
            cmd = f'{STACKS_BIN} proxy {stack_name} {container_name} on'
        else: return
    elif action == 'proxy_off':
        if stack_name:
            cmd = f'{STACKS_BIN} proxy {stack_name} {container_name} off'
        else: return
    elif action == 'inspect':
        _show_container_inspect(stdscr, container_name)
        return
    elif action == 'edit_ip':
        cur = _get_container_ip(container_name)
        new_ip = _prompt_text(stdscr, f'Edit IP — {container_name}', 'New IP:', cur or '192.168.1.')
        if not new_ip or new_ip == cur: return
        if not re.match(r'^\d{1,3}(\.\d{1,3}){3}$', new_ip):
            run_log_popup(stdscr, 'Edit IP', f'echo "Invalid IP: {new_ip}"'); return
        _apply_container_ip(stack_file, container_name, cur, new_ip)
        cmd = f'{STACKS_BIN} up {stack_name} {container_name} recreate' if stack_name else f'docker restart {container_name}'
    # ── fix/repair/recreate single-service combos (mirror the Stacks page) ────
    elif action in ('fix', 'repair', 'fix_repair', 'recreate_up',
                    'fix_recreate', 'repair_recreate', 'full_repair', 'deep_repair'):
        if not stack_name:
            run_log_popup(stdscr, 'No stack',
                f'echo "{container_name} has no known stack — cannot fix/repair."')
            return
        c_fix      = f'{STACKS_BIN} fix {stack_name}'
        c_repair   = f'{STACKS_BIN} fix {stack_name} repair'
        c_recreate = f'{STACKS_BIN} up {stack_name} {container_name} recreate'
        c_up       = f'{STACKS_BIN} up {stack_name} {container_name}'
        if action == 'fix':
            run_log_popup(stdscr, f'Fix → {stack_name}', c_fix)
        elif action == 'repair':
            run_log_popup(stdscr, f'Repair → {stack_name}', c_repair)
        elif action == 'fix_repair':
            run_cmd_silent(stdscr, f'Fix → {stack_name}', c_fix)
            run_log_popup(stdscr, f'Repair → {stack_name}', c_repair)
        elif action == 'recreate_up':
            run_cmd_silent(stdscr, f'Recreate → {container_name}', c_recreate)
            run_log_popup(stdscr, f'Up → {container_name}', c_up)
        elif action == 'fix_recreate':
            run_cmd_silent(stdscr, f'Fix → {stack_name}', c_fix)
            run_cmd_silent(stdscr, f'Recreate → {container_name}', c_recreate)
            run_log_popup(stdscr, f'Up → {container_name}', c_up)
        elif action == 'repair_recreate':
            run_cmd_silent(stdscr, f'Repair → {stack_name}', c_repair)
            run_cmd_silent(stdscr, f'Recreate → {container_name}', c_recreate)
            run_log_popup(stdscr, f'Up → {container_name}', c_up)
        elif action == 'full_repair':
            run_cmd_silent(stdscr, f'Fix → {stack_name}', c_fix)
            run_cmd_silent(stdscr, f'Repair → {stack_name}', c_repair)
            run_cmd_silent(stdscr, f'Recreate → {container_name}', c_recreate)
            run_log_popup(stdscr, f'Up → {container_name}', c_up)
        elif action == 'deep_repair':
            run_cmd_silent(stdscr, f'Repair → {stack_name}', c_repair)
            run_cmd_silent(stdscr, f'Fix → {stack_name}', c_fix)
            run_cmd_silent(stdscr, f'Recreate → {container_name}', c_recreate)
            run_log_popup(stdscr, f'Up → {container_name}', c_up)
        return
    else: return
    run_log_popup(stdscr, f'{action} → {container_name}', cmd)

# ── Tab views ────────────────────────────────────────────────────────────────
TABS = ['Containers', 'Stacks', 'Logs', 'Dynamics', 'Art', 'Backup', 'Build', 'Configs', 'Network', 'Updates']

def draw_containers_tab(win, h, w, containers, sel, scroll):
    win.addstr(3, 2, f'{"NAME":<26} {"STACK":<12} {"STATUS":<12} {"MEMORY":<19} {"SIZE":<9} {"IMAGE"}',
               curses.color_pair(C_ACCENT))
    win.addstr(4, 2, '─' * (w-4), curses.color_pair(C_DIM))

    visible = h - 7
    items = containers[scroll:scroll+visible]


    for i, c in enumerate(items):
        y = 5 + i
        idx = scroll + i
        name   = c.get('name','')[:34]
        state  = c.get('state','')
        status = c.get('status','')[:11]
        image  = c.get('image','')[:29]
        stack  = c.get('stack','')[:11]


        is_running = state.lower() == 'running'
        color = C_RUNNING if is_running else C_STOPPED
        indicator = '●' if is_running else '○'

        mem = app_data['mem_stats'].get(c.get('name',''), '')[:18]
        img_sz = app_data['img_sizes'].get(image, app_data['img_sizes'].get(image.split(':')[0]+':latest',''))[:8]
        if idx == sel:
            line = f'{indicator} {name:<26} {stack:<12} {status:<12} {mem:<19} {img_sz:<9} {image}'
            try: win.addstr(y, 2, line[:w-4], curses.color_pair(C_SELECTED))
            except: pass
        else:
            try:
                win.addstr(y, 2, f'{indicator} ', curses.color_pair(color))
                win.addstr(y, 4, f'{name:<26}', curses.color_pair(C_NORMAL))
                win.addstr(y, 31, f'{stack:<12}', curses.color_pair(C_GREEN if stack else C_DIM))
                win.addstr(y, 44, f'{status:<12}', curses.color_pair(C_DIM))
                win.addstr(y, 56, f'{mem:<19}', curses.color_pair(C_YELLOW if mem else C_DIM))
                win.addstr(y, 75, f'{img_sz:<9}', curses.color_pair(C_CYAN if img_sz else C_DIM))
                win.addstr(y, 84, f'{image}'[:w-86], curses.color_pair(C_DIM))
            except: pass

def draw_stacks_tab(win, h, w, stacks, sel, scroll):
    try:
        win.addstr(3, 2, "[ A ] All-Stacks Actions", curses.color_pair(C_ACCENT))
        win.addstr(4, 2, f'{"STACK":<20} {"RUN/T":<8} {"KB":<7}  {"IMG SIZE":<10} {"RAM":<9} {"STATUS"}',
                   curses.color_pair(C_YELLOW))
        win.addstr(5, 2, '─' * (w-4), curses.color_pair(C_DIM))
    except: pass

    visible = h - 8
    items = stacks[scroll:scroll+visible]


    for i, s in enumerate(items):
        y = 5 + i
        idx = scroll + i
        name    = s['name'][:24]
        running = s['running']
        stopped = s['stopped']
        total   = s['total']
        missing = total - running - stopped

        if running == 0:
            status = '■ DOWN'
            color  = C_STOPPED
        elif missing > 0:
            status = '⚠ PARTIAL'
            color  = C_YELLOW
        else:
            status = '● UP'
            color  = C_RUNNING

        size_kb = s.get('size_kb', 0)
        size_str = f'{size_kb}K' if size_kb < 1000 else f'{size_kb//1000}M'
        # Total image size for this stack
        img_total = 0
        for img in s.get('images', []):
            sz_str = app_data['img_sizes'].get(img, '')
            if sz_str:
                try:
                    if 'GB' in sz_str: img_total += float(sz_str.replace('GB','')) * 1024
                    elif 'MB' in sz_str: img_total += float(sz_str.replace('MB',''))
                    elif 'kB' in sz_str: img_total += float(sz_str.replace('kB','')) / 1024
                except: pass
        img_total_str = f'{img_total:.0f}MB' if img_total < 1024 else f'{img_total/1024:.1f}GB'
        # Total memory for this stack - only containers in THIS stack
        stack_mem = 0.0
        try:
            stack_content = open(s['file']).read()
            stack_containers = set(re.findall(r'container_name:\s*(\S+)', stack_content))
            for cname, mem in app_data['mem_stats'].items():
                if cname in stack_containers and '/' in mem:
                    try:
                        used = mem.split('/')[0].strip()
                        if 'MiB' in used: stack_mem += float(used.replace('MiB',''))
                        elif 'GiB' in used: stack_mem += float(used.replace('GiB','')) * 1024
                        elif 'KiB' in used: stack_mem += float(used.replace('KiB','')) / 1024
                    except: pass
        except: pass
        mem_str = f'{stack_mem:.0f}M' if running > 0 and stack_mem > 0 else ''
        if idx == sel:
            line = f'{name:<20} {running:>3}/{total:<4} {size_str:<7} {img_total_str:<10} {mem_str:<9} {status}'
            try: win.addstr(y, 2, line[:w-4], curses.color_pair(C_SELECTED))
            except: pass
        else:
            try:
                win.addstr(y, 2,  f'{name:<20} {running:>3}/{total:<4}', curses.color_pair(C_NORMAL))
                win.addstr(y, 33, f'{size_str:<7}', curses.color_pair(C_DIM))
                win.addstr(y, 41, f'{img_total_str:<10}', curses.color_pair(C_CYAN if img_total > 0 else C_DIM))
                win.addstr(y, 52, f'{mem_str:<9}', curses.color_pair(C_YELLOW if mem_str else C_DIM))
                win.addstr(y, 62, f'{status}', curses.color_pair(color))
            except: pass

def draw_logs_tab(win, h, w, log_lines, sel, scroll):
    try:
        win.addstr(3, 2, "DOCKER LOGS", curses.color_pair(C_ACCENT))
        win.addstr(4, 2, "─" * (w-4), curses.color_pair(C_DIM))
        import glob as _glob
        _log_dir = '/srv/stacks'
        sources = [(f.split('/')[-1], f'cat {f}', f) for f in sorted(_glob.glob(f'{_log_dir}/stacks_*.log'))]
        if not sources: sources = [('No logs found', 'echo No stacks logs found', '')]
        visible = h - 7
        for i, (label, _, fpath) in enumerate(sources):
            y = 5 + i
            if y >= h - 2: break
            try: fsize = f"{os.path.getsize(fpath)//1024}K" if fpath else ""
            except: fsize = ""
            line = f"{label:<35} {fsize:>6}"
            if i == sel:
                try: win.addstr(y, 2, f"  ▶  {line}", curses.color_pair(C_SELECTED))
                except: pass
            else:
                try: win.addstr(y, 2, f"     {line}", curses.color_pair(C_NORMAL))
                except: pass
    except: pass
    return [(l,c) for l,c,_ in sources]

def draw_dynamics_tab(win, h, w, sel):
    try:
        import glob as _g
        win.addstr(3, 2, "DYNAMIC CONFIGS  [ A = inject art into selected ]", curses.color_pair(C_ACCENT))
        win.addstr(4, 2, "─" * (w-4), curses.color_pair(C_DIM))
        files = sorted(_g.glob(f"{DYNAMICS_DIR}/*.yml") + _g.glob(f"{DYNAMICS_DIR}/*.yaml"))
        for i, f in enumerate(files):
            y = 5 + i
            if y >= h-2: break
            label = os.path.basename(f)
            try: fsize = f"{os.path.getsize(f)//1024}K"
            except: fsize = ""
            line = f"{label:<40} {fsize:>6}"
            if i == sel:
                try: win.addstr(y, 2, f"  ▶  {line}", curses.color_pair(C_SELECTED))
                except: pass
            else:
                try: win.addstr(y, 2, f"     {line}", curses.color_pair(C_NORMAL))
                except: pass
        return files
    except: return []

ART_ITEMS = [
    ("Inject art into ALL stacks",           "art_inject_all"),
    ("Strip art from ALL stacks",            "art_strip_all"),
    ("Inject art into ALL dynamics",         "art_inject_dyn"),
    ("Strip art from ALL dynamics",          "art_strip_dyn"),
    ("Edit art.conf",                   "edit_art"),
    ("Edit stack_urls.conf",                 "edit_urls"),
    ("Generate dynamics from ALL stacks",    "gen_dyn_all"),
    ("Force regenerate ALL dynamics",        "gen_dyn_force"),
    ("Repair ALL dynamic configs",           "repair_dyn"),
]

def draw_art_tab(win, h, w, sel=0):
    try:
        win.addstr(3, 2, "ART & DYNAMICS", curses.color_pair(C_ACCENT))
        win.addstr(4, 2, "─" * (w-4), curses.color_pair(C_DIM))
    except: pass
    for i, (label, _) in enumerate(ART_ITEMS):
        y = 5 + i
        if y >= h-2: break
        if i == sel:
            try: win.addstr(y, 2, f"  ▶  {label:<55}", curses.color_pair(C_SELECTED))
            except: pass
        else:
            try: win.addstr(y, 2, f"     {label:<55}", curses.color_pair(C_NORMAL))
            except: pass

BACKUP_ITEMS = [
    ("Run full backup now",                  "backup_full"),
    ("Run pre-backup snapshot",              "backup_pre"),
    ("View backup log",                      "backup_log"),
    ("View stacks up log",                   "view_up_log"),
    ("View stacks fix log",                  "view_fix_log"),
    ("View stacks build log",                "view_build_log"),
    ("Restore from backup",                  "backup_restore"),
]

def draw_backup_tab(win, h, w, sel=0):
    try:
        win.addstr(3, 2, "BACKUP & LOGS", curses.color_pair(C_ACCENT))
        win.addstr(4, 2, "─" * (w-4), curses.color_pair(C_DIM))
    except: pass
    for i, (label, _) in enumerate(BACKUP_ITEMS):
        y = 5 + i
        if y >= h-2: break
        if i == sel:
            try: win.addstr(y, 2, f"  ▶  {label:<55}", curses.color_pair(C_SELECTED))
            except: pass
        else:
            try: win.addstr(y, 2, f"     {label:<55}", curses.color_pair(C_NORMAL))
            except: pass

BUILD_ITEMS = [
    ('Build new service (wizard)',           'build_new'),
    ('Create new stack + add service',       'build_new_stack'),
    ('Generate dynamics from ALL stacks',    'gen_dyn_all'),
    ('Generate dynamics from one stack',     'gen_dyn_one'),
    ('Force regen ALL dynamics',             'gen_dyn_force'),
    ('Generate global inject config',        'gen_inject'),
    ('Generate sablier groups config',       'gen_groups'),
    ('Run stacks fix on ALL',                'fix_all'),
    ('Run stacks repair on ALL',             'repair_all'),
]

def draw_build_tab(win, h, w, sel=0):
    try:
        win.addstr(3, 2, 'BUILD', curses.color_pair(C_ACCENT))
        win.addstr(4, 2, '─' * (w-4), curses.color_pair(C_DIM))
    except: pass
    for i, (label, _) in enumerate(BUILD_ITEMS):
        y = 5 + i
        if y >= h-2: break
        if i == sel:
            try: win.addstr(y, 2, f'  ▶  {label:<50}', curses.color_pair(C_SELECTED))
            except: pass
        else:
            try: win.addstr(y, 2, f'     {label:<50}', curses.color_pair(C_NORMAL))
            except: pass

CONFIG_FILES = [
    ("stacks.conf",          "stacks.conf"),
    ("build.conf",           "build.conf"),
    ("all_services.txt",     "all_services.txt"),
    ("global_inject.conf",   "global_inject.conf"),
    ("menu.conf",       "menu.conf"),
    ("stack_urls.conf",      "stack_urls.conf"),
    ("backup.conf",          "backup.conf"),
    ("art.conf",        "art.conf"),
]
DESCRIPTIONS_DIR = os.path.expanduser("~/.config/stacks/descriptions")

def get_config_items():
    """Build flat list of (label, fpath, is_dir) for configs tab."""
    items = []
    for label, fname in CONFIG_FILES:
        fpath = os.path.join(CONF_DIR, fname)
        items.append((label, fpath, False))
    # Descriptions folder
    desc_dir = os.path.expanduser("~/.config/stacks/descriptions")
    try:
        desc_files = sorted(f for f in os.listdir(desc_dir) if f.endswith(".conf"))
        total_sz = sum(os.path.getsize(os.path.join(desc_dir,f)) for f in desc_files)
        items.append((f"📁 descriptions/  ({len(desc_files)} files, {max(1,total_sz//1024)}K)", desc_dir, True))
        for f in desc_files:
            items.append((f"   {f}", os.path.join(desc_dir,f), False))
    except: pass
    return items

def draw_configs_tab(win, h, w, sel):
    try:
        win.addstr(3, 2, "CONFIGS", curses.color_pair(C_ACCENT))
        win.addstr(4, 2, "─" * (w-4), curses.color_pair(C_DIM))
    except: pass
    items = get_config_items()
    for i, (label, fpath, is_dir) in enumerate(items):
        y = 6 + i
        if y >= win.getmaxyx()[0]-2: break
        try:
            sz = os.path.getsize(fpath) if not is_dir else sum(
                os.path.getsize(os.path.join(fpath,f)) for f in os.listdir(fpath)
                if f.endswith(".conf"))
            fsize = f"{max(1,sz//1024)}K"
        except: fsize = ""
        line = f"{label:<40} {fsize:>5}"
        if i == sel:
            try: win.addstr(y, 2, f"  ▶  {line}", curses.color_pair(C_SELECTED))
            except: pass
        else:
            attr = curses.color_pair(C_ACCENT) if is_dir else curses.color_pair(C_NORMAL)
            try: win.addstr(y, 2, f"     {line}", attr)
            except: pass

# Cache for network tab to avoid slow rescan on every draw
_net_cache = {"data": None, "ts": 0}

def draw_network_tab(win, h, w, sel=0):
    """IP and port collision detection tab."""
    try:
        win.addstr(3, 2, "NETWORK — IP & PORT COLLISION DETECTION", curses.color_pair(C_ACCENT))
        win.addstr(4, 2, "─" * (w-4), curses.color_pair(C_DIM))
    except: pass
    try:
        import importlib.util as _ilu, time as _t
        # Only rescan every 30 seconds
        if _net_cache["data"] is None or _t.time() - _net_cache["ts"] > 30:
            try: win.addstr(3, 42, " scanning... ", curses.color_pair(C_DIM))
            except: pass
            win.refresh()
            spec = _ilu.spec_from_file_location("stacks_collision", "/usr/local/lib/stacks_collision.py")
            mod = _ilu.module_from_spec(spec); spec.loader.exec_module(mod)
            _net_cache["data"] = {
                "ip_col": mod.get_collisions()[0],
                "port_col": mod.get_collisions()[1],
                "ip_map": mod.scan_all_ips(),
                "next_ip": mod.get_next_available_ip(),
                "port_map": mod.scan_all_ports(),
                "conf": mod.load_conf(),
            }
            _net_cache["ts"] = _t.time()
        ip_col   = _net_cache["data"]["ip_col"]
        port_col = _net_cache["data"]["port_col"]
        ip_map   = _net_cache["data"]["ip_map"]
        next_ip  = _net_cache["data"]["next_ip"]

        cfg = _net_cache["data"].get("conf", {})
        port_map = _net_cache["data"].get("port_map", {})
        def P(yy, xx, txt, cp=C_NORMAL):
            try: win.addstr(yy, xx, str(txt)[:max(0, w-xx-1)], curses.color_pair(cp))
            except Exception: pass

        # Summary
        P(5, 2, f"IPs in use: {len(ip_map)}    Ports in use: {len(port_map)}    IP collisions: {len(ip_col)}    Port collisions: {len(port_col)}", C_YELLOW)
        P(6, 2, f"Next free IP: {next_ip or 'NONE'}", C_GREEN if next_ip else C_RED)

        # Config panel
        P(8, 2, "── CONFIG ────────────────────", C_ACCENT)
        P(9, 4,  f"IP range    : {cfg.get('IP_RANGE_START','?')}  →  {cfg.get('IP_RANGE_END','?')}", C_CYAN)
        P(10, 4, f"Port range  : {cfg.get('PORT_RANGE_START','?')}  →  {cfg.get('PORT_RANGE_END','?')}", C_CYAN)
        P(11, 4, f"IP blacklist : {cfg.get('IP_BLACKLIST','') or '(none)'}", C_DIM)
        P(12, 4, f"IP whitelist : {cfg.get('IP_WHITELIST','') or '(none)'}", C_DIM)
        P(13, 4, f"Port blacklist: {cfg.get('PORT_BLACKLIST','') or '(none)'}", C_DIM)

        # Collisions
        y = 15
        if ip_col:
            P(y, 2, "⚠ IP COLLISIONS:", C_RED); y+=1
            for c in ip_col[:6]:
                P(y, 4, f"{c['type']:12} {c['ip']:18} " + ", ".join(f"{ss}/{nn}" for ss,nn in c['owners'][:3]), C_RED); y+=1
        else:
            P(y, 2, "✔ No IP collisions", C_GREEN); y+=1
        if port_col:
            P(y, 2, "⚠ PORT COLLISIONS:", C_RED); y+=1
            for c in port_col[:6]:
                P(y, 4, f"{c['type']:12} port {c['port']:8} " + ", ".join(f"{ss}/{nn}" for ss,nn in c['owners'][:3]), C_RED); y+=1
        else:
            P(y, 2, "✔ No port collisions", C_GREEN); y+=1

        # Two columns: IPs in use | Ports in use
        y += 1
        col2 = max(42, w//2)
        P(y, 2, f"IPs IN USE ({len(ip_map)})", C_YELLOW)
        P(y, col2, f"PORTS IN USE ({len(port_map)})", C_YELLOW)
        base = y+1; yy = base
        for ip, owners in sorted(ip_map.items()):
            if yy >= h-2: break
            P(yy, 4, f"{ip:<16} {','.join(nn for _,nn in owners[:1])}", C_NORMAL); yy+=1
        yy = base
        for key, owners in sorted(port_map.items()):
            if yy >= h-2: break
            P(yy, col2+2, f"{key:<22} {','.join(nn for _,nn in owners[:1])}", C_NORMAL); yy+=1
    except Exception as e:
        try: win.addstr(5, 2, f"Error: {e}", curses.color_pair(C_RED))
        except: pass

NETWORK_ACTIONS = [
    ("Scan for collisions",          "net_scan"),
    ("Show all IPs in use",          "net_ips"),
    ("Show all ports in use",        "net_ports"),
    ("Edit IP/port config",          "net_config"),
]

NETWORK_EDIT_ACTIONS = [
    ("✎  IP range — start",        "ip_start"),
    ("✎  IP range — end",          "ip_end"),
    ("✎  Port range — start",      "port_start"),
    ("✎  Port range — end",        "port_end"),
    ("➕ Add IP to blacklist",      "ip_bl_add"),
    ("➖ Remove IP from blacklist", "ip_bl_rm"),
    ("➕ Add IP to whitelist",      "ip_wl_add"),
    ("➖ Remove IP from whitelist", "ip_wl_rm"),
    ("➕ Add port to blacklist",    "port_bl_add"),
    ("➖ Remove port from blacklist","port_bl_rm"),
    ("↻  Rescan now",              "rescan"),
    ("✕  Cancel",                  None),
]

def do_network_action(stdscr, action):
    """Edit network settings in stacks.yaml (the master). Invalidates the scan cache."""
    if action is None: return
    import sys as _s; _s.path.insert(0, "/usr/local/lib"); import stacks_config as SC
    cfg = SC.load()
    scalars = {"ip_start":("ip_range_start","IP_RANGE_START","Start IP:"),
               "ip_end":("ip_range_end","IP_RANGE_END","End IP:"),
               "port_start":("port_range_start","PORT_RANGE_START","Start port:"),
               "port_end":("port_range_end","PORT_RANGE_END","End port:")}
    adds = {"ip_bl_add":"ip_blacklist","ip_wl_add":"ip_whitelist","port_bl_add":"port_blacklist"}
    rms  = {"ip_bl_rm":"ip_blacklist","ip_wl_rm":"ip_whitelist","port_bl_rm":"port_blacklist"}
    if action in scalars:
        fk, ik, prompt = scalars[action]
        v = _prompt_text(stdscr, f"Edit {fk}", prompt, cfg.get(ik, ""))
        if v: SC.yaml_set_scalar(fk, v)
    elif action in adds:
        key = adds[action]
        v = _prompt_text(stdscr, f"Add to {key}", "Value:", "")
        if v:
            items = SC.yaml_get_list(key)
            if v not in items:
                items.append(v); SC.yaml_set_list(key, items)
    elif action in rms:
        key = rms[action]
        items = SC.yaml_get_list(key)
        if items:
            res = run_popup_action(stdscr, f"Remove from {key}", [(x, x) for x in items] + [("✕ Cancel", None)])
            if res and res[1]:
                SC.yaml_set_list(key, [x for x in items if x != res[1]])
    _net_cache["data"] = None  # force rescan so the tab reflects the change


def get_update_rows():
    """Build the unified Updates-tab row list: available updates first, then
    update history (newest first). Each row is a dict the tab can render/filter."""
    rows = []
    summary = {"updates": 0, "ok": 0, "errors": 0, "hist": 0}
    try:
        import sys as _s
        if "/usr/local/lib" not in _s.path: _s.path.insert(0, "/usr/local/lib")
        import importlib, stacks_updates as _su
        importlib.reload(_su)
    except Exception:
        _su = None
    # available updates (from cache)
    try:
        import json as _j
        cache_file = os.path.expanduser("~/.config/stacks/update_cache.json")
        cache = _j.load(open(cache_file)) if os.path.exists(cache_file) else {}
        for v in cache.values():
            if not isinstance(v, dict): continue
            if v.get("has_update"):
                summary["updates"] += 1
                rows.append({
                    "kind": "update", "image": v.get("image",""),
                    "tag": v.get("tag",""), "stacks": v.get("stacks",[]),
                    "old": v.get("local_digest",""), "new": v.get("remote_digest",""),
                    "ts": v.get("checked", 0),
                })
            elif v.get("error"):
                summary["errors"] += 1
            else:
                summary["ok"] += 1
    except Exception:
        pass
    # history (newest first)
    try:
        if _su:
            for r in _su.get_history():
                summary["hist"] += 1
                rows.append({
                    "kind": "hist", "image": r.get("image",""),
                    "tag": r.get("tag",""), "stacks": r.get("stacks",[]),
                    "event": r.get("event",""), "ts": r.get("ts", 0),
                    "old": r.get("old",""), "new": r.get("new",""),
                    "old_short": r.get("old_short","—"), "new_short": r.get("new_short","—"),
                })
    except Exception:
        pass
    return rows, summary

def _row_image(r):
    return r.get("image","")

def draw_updates_tab(win, h, w, rows, summary, sel, scroll):
    """Image update tracker tab — available updates + searchable history."""
    import time as _t
    try:
        win.addstr(3, 2, f"⬆ Updates: {summary['updates']}   ✔ OK: {summary['ok']}   "
                         f"✘ Err: {summary['errors']}   ⟳ History: {summary['hist']}",
                   curses.color_pair(C_YELLOW))
        win.addstr(4, 2, f'{"WHEN":<13} {"EVENT":<10} {"IMAGE":<40} {"OLD → NEW"}',
                   curses.color_pair(C_ACCENT))
        win.addstr(5, 2, "─" * (w-4), curses.color_pair(C_DIM))
    except: pass
    if not rows:
        try: win.addstr(7, 2, "No updates or history yet. Press C to check for updates.",
                        curses.color_pair(C_DIM))
        except: pass
        return
    visible = h - 8
    for i, r in enumerate(rows[scroll:scroll+visible]):
        y = 6 + i
        idx = scroll + i
        img = r.get("image","")[:40]
        if r["kind"] == "update":
            when = "now"
            ev = "AVAILABLE"
            ver = "update ready"
            color = C_GREEN
            mark = "⬆"
        else:
            when = _t.strftime("%m-%d %H:%M", _t.localtime(r.get("ts", 0)))
            ev = r.get("event","")
            ver = f'{r.get("old_short","—")} → {r.get("new_short","—")}'
            color = C_CYAN if ev == "pulled" else C_YELLOW
            mark = "⬇" if ev == "pulled" else "⬆"
        line = f'{when:<13} {ev:<10} {img:<40} {ver}'
        if idx == sel:
            try: win.addstr(y, 2, f"{mark} " + line[:w-6], curses.color_pair(C_SELECTED))
            except: pass
        else:
            try:
                win.addstr(y, 2, f"{mark} {when:<13} ", curses.color_pair(C_DIM))
                win.addstr(y, 17, f"{ev:<10} ", curses.color_pair(color))
                win.addstr(y, 28, f"{img:<40} ", curses.color_pair(C_NORMAL))
                win.addstr(y, 69, f"{ver}"[:w-71], curses.color_pair(C_DIM))
            except: pass

UPDATES_ACTIONS = [
    ("Check for updates (all images)",    "upd_check_all"),
    ("Check for updates (running only)",  "upd_check_running"),
    ("Force re-check (bypass cache)",     "upd_check_force"),
    ("Pull all available updates",        "upd_pull_all"),
    ("View update cache",                 "upd_view_cache"),
]

# ── Main TUI ─────────────────────────────────────────────────────────────────
def main(stdscr):
    init_colors()
    curses.curs_set(0)
    stdscr.nodelay(False)
    stdscr.timeout(200)
    curses.flushinp()  # clear any buffered keypresses on launch
    try: curses.mousemask(curses.ALL_MOUSE_EVENTS)
    except: pass

    # Start background data refresh
    t = threading.Thread(target=refresh_data, daemon=True)
    t.start()
    t2 = threading.Thread(target=fetch_mem_stats, daemon=True)
    t2.start()

    # Wait for first data load
    _wf = 0
    while True:
        with data_lock:
            if app_data['last_update'] > 0:
                break
        try:
            stdscr.erase()
            draw_whale_frame(stdscr, _wf)
            stdscr.refresh()
        except:
            pass
        _wf += 1
        time.sleep(0.08)
    try: stdscr.erase()
    except: pass

    tab    = 0   # 0=containers 1=stacks 2=backup 3=build 4=configs
    sel    = 0
    scroll = 0
    cfg_sel = 0

    # Per-tab letter-jump / inline-search filter (tabs 0,1,9). Resets on tab switch.
    flt_letter = None    # None or 'a'-'z'/'#'
    flt_inline = ""      # typed substring
    flt_mode   = False   # True while typing the inline filter
    FILTERABLE = (0, 1, 9)

    FOOTER_HINTS = {
        0: ['↑↓ Nav', 'a-z Jump', '/ Search', '↔ Tab', 'ENTER Action', 'Q Quit'],
        1: ['↑↓ Nav', 'a-z Jump', '/ Search', '↔ Tab', 'ENTER Action', 'A All-Stacks', 'Q Quit'],
        2: ['↑↓ Select', '↔ Tab', 'ENTER Open', 'Q Quit'],
        3: ['↑↓ Select', '↔ Tab', 'ENTER Edit', 'A Inject Art', 'Q Quit'],
        4: ['I Inject All', 'S Strip All', 'D Dyn Inject', 'X Dyn Strip', 'E Edit Art Conf', 'Q Quit'],
        6: ['↑↓ Navigate', 'ENTER Select', 'Q Quit'],
        7: ['↑↓ Navigate', '↔ Switch Tab', 'ENTER Edit', 'Q Quit'],
        8: ['A Edit', 'S Scan', 'E Edit YAML', '↔ Tab', 'Q Quit'],
        9: ['↑↓ Nav', 'a-z Jump', '/ Search', 'ENTER Detail', 'C Check', 'F Force', 'P Pull', 'Q Quit'],
    }

    while True:
        h, w = stdscr.getmaxyx()
        stdscr.erase()

        # Header
        now = datetime.now().strftime('%H:%M:%S')
        with data_lock:
            nc = len(app_data['containers'])
            nr = sum(1 for c in app_data['containers'] if c.get('state','').lower()=='running')
        title = f'  ✦ STACKSSTACKS  ·  {nr}/{nc} running  ·  {now}  '
        draw_header(stdscr, title, w)
        with open("/tmp/tab_live.txt","w") as _f: _f.write(f"tab={tab} {TABS[tab] if tab < len(TABS) else chr(63)}\n")
        with open("/tmp/tab_live.txt","w") as _f: _f.write(f"tab={tab} {TABS[tab] if tab < len(TABS) else chr(63)}\n")

        # Content data
        with data_lock:
            containers = list(app_data['containers'])
            stacks     = list(app_data['stacks'])
        update_rows, update_summary = [], {}
        if tab == 9:
            update_rows, update_summary = get_update_rows()

        # Apply per-tab letter/inline filter (tabs 0,1,9)
        flt_shown = flt_total = 0
        if tab == 0:
            flt_total = len(containers)
            containers = list_filter(containers,
                [lambda c: c.get('name',''), lambda c: c.get('image',''), lambda c: c.get('stack','')],
                flt_letter, flt_inline)
            flt_shown = len(containers)
        elif tab == 1:
            flt_total = len(stacks)
            stacks = list_filter(stacks, [lambda s: s.get('name','')], flt_letter, flt_inline)
            flt_shown = len(stacks)
        elif tab == 9:
            flt_total = len(update_rows)
            update_rows = list_filter(update_rows,
                [lambda r: r.get('image',''), lambda r: r.get('event','')], flt_letter, flt_inline)
            flt_shown = len(update_rows)

        # Tabs + filter bar / divider
        draw_tabs(stdscr, 1, w, TABS, tab)
        if tab in FILTERABLE:
            draw_filter_bar(stdscr, 2, w, flt_letter, flt_inline, flt_mode, flt_shown, flt_total)
        else:
            try: stdscr.addstr(2, 0, "─" * (w-1), curses.color_pair(C_DIM))
            except curses.error: pass

        if tab == 0:
            if sel >= len(containers): sel = max(0, len(containers)-1)
            draw_containers_tab(stdscr, h, w, containers, sel, scroll)
        elif tab == 1:
            if sel >= len(stacks): sel = max(0, len(stacks)-1)
            draw_stacks_tab(stdscr, h, w, stacks, sel, scroll)
        elif tab == 2:
            log_sources = draw_logs_tab(stdscr, h, w, [], sel, scroll)
        elif tab == 3:
            dyn_files = draw_dynamics_tab(stdscr, h, w, sel)
        elif tab == 4:
            draw_art_tab(stdscr, h, w, sel)
        elif tab == 5:
            draw_backup_tab(stdscr, h, w, sel)
        elif tab == 6:
            draw_build_tab(stdscr, h, w, sel)
        elif tab == 7:
            draw_configs_tab(stdscr, h, w, cfg_sel)
        elif tab == 8:
            draw_network_tab(stdscr, h, w)
        elif tab == 9:
            if sel >= len(update_rows): sel = max(0, len(update_rows)-1)
            draw_updates_tab(stdscr, h, w, update_rows, update_summary, sel, scroll)

        draw_footer(stdscr, h, w, FOOTER_HINTS.get(tab, []))
        stdscr.refresh()

        try:
            k = stdscr.getch()
        except curses.error:
            k = curses.KEY_RESIZE
        if k == -1: continue
        if k == curses.KEY_RESIZE:
            h, w = stdscr.getmaxyx()
            stdscr.clear()
            continue

        # ── Letter-jump / inline-search filter (tabs 0,1,9) ──────────────────
        if tab in FILTERABLE:
            if flt_mode:
                if k in (10, 13):
                    flt_mode = False
                elif k == 27:
                    flt_mode = False; flt_inline = ""; sel = 0; scroll = 0
                elif k in (curses.KEY_BACKSPACE, 127, 8):
                    flt_inline = flt_inline[:-1]; sel = 0; scroll = 0
                elif 32 <= k <= 126:
                    flt_inline += chr(k); sel = 0; scroll = 0
                continue
            if k == ord('/'):
                flt_mode = True; flt_inline = ""; continue
            if k == ord('#'):
                flt_letter = None if flt_letter == '#' else '#'; sel = 0; scroll = 0; continue
            if 97 <= k <= 122:  # a-z (lowercase) → letter jump (uppercase stays a command)
                ch = chr(k)
                flt_letter = None if flt_letter == ch else ch; sel = 0; scroll = 0; continue
            if k == 27 and (flt_letter or flt_inline):
                flt_letter = None; flt_inline = ""; sel = 0; scroll = 0; continue

        # Global keys  (ESC also quits — on filterable tabs the filter block above
        # consumes ESC first to clear an active search, so the next ESC exits)
        if k in (27, ord('q'), ord('Q')): break
        if k == curses.KEY_RIGHT:
            tab = (tab + 1) % len(TABS)
            curses.flushinp()
            sel = 0; scroll = 0
            flt_letter = None; flt_inline = ""; flt_mode = False
        elif k == curses.KEY_LEFT:
            tab = (tab - 1) % len(TABS)
            curses.flushinp()
            sel = 0; scroll = 0
            flt_letter = None; flt_inline = ""; flt_mode = False

        # Tab-specific keys - only process if not a tab-switch key
        if k in (curses.KEY_RIGHT, curses.KEY_LEFT):
            pass
        elif tab == 0:  # Containers
            items = containers
            vis = h - 7
            if k == curses.KEY_UP:
                if sel > 0: sel -= 1
                if sel < scroll: scroll = sel
            elif k == curses.KEY_DOWN:
                if sel < len(items)-1: sel += 1
                if sel >= scroll + vis: scroll = sel - vis + 1
            elif k in (10, 13) and items:
                c = items[sel]
                cname = c.get('name','')
                # Find which stack this container belongs to
                stack_file = None
                for s in stacks:
                    try:
                        content = open(s['file']).read()
                        if f'container_name: {cname}' in content:
                            stack_file = s['file']
                            break
                    except: pass
                result = run_popup_action(stdscr,
                    f'Container: {cname[:20]}', CONTAINER_ACTIONS)
                if result and result[1]:
                    curses.flushinp()
                    do_container_action(stdscr, cname, stack_file, result[1])

        elif tab == 1:  # Stacks
            items = stacks
            vis = h - 7
            if k == curses.KEY_UP:
                if sel > 0: sel -= 1
                if sel < scroll: scroll = sel
            elif k == curses.KEY_DOWN:
                if sel < len(items)-1: sel += 1
                if sel >= scroll + vis: scroll = sel - vis + 1
            elif k in (10, 13) and items:
                s = items[sel]
                result = run_popup_action(stdscr,
                    f'Stack: {s["name"][:20]}', STACK_ACTIONS)
                if result and result[1]:
                    do_stack_action(stdscr, s['name'], result[1])
            elif k == ord('A'):  # lowercase a is the letter-jump filter
                result = run_popup_action(stdscr, 'ALL Stacks', GLOBAL_ACTIONS)
                if result and result[1]:
                    do_global_action(stdscr, result[1])

        elif tab == 2:  # Logs
            import glob as _glob
            _log_dir = '/srv/stacks'
            _log_files = sorted(_glob.glob(f'{_log_dir}/stacks_*.log'))
    # Add build log if it exists
            # Add build log if it exists
            _build_log = f'{_log_dir}/stacks_build.log'
            if os.path.exists(_build_log) and _build_log not in _log_files: _log_files.insert(0, _build_log)
            log_sources = [(f.split('/')[-1], f'cat {f}') for f in _log_files]
            if not log_sources:
                log_sources = [('No logs found', 'echo No stacks logs found')]
            if k == curses.KEY_UP: sel = max(0, sel-1)
            elif k == curses.KEY_DOWN: sel = min(len(log_sources)-1, sel+1)
            elif k in (10, 13) and 0 <= sel < len(log_sources):
                label, cmd = log_sources[sel]
                fpath = cmd.replace("cat ", "")
                editor = os.environ.get("EDITOR", "nano")
                curses.endwin()
                os.system(f"{editor} {fpath}")
                stdscr = curses.initscr()
                init_colors()
                curses.curs_set(0)
                stdscr.clear()
        elif tab == 3:  # Dynamics
            import glob as _g
            dyn_files = sorted(_g.glob(f'{DYNAMICS_DIR}/*.yml') + _g.glob(f'{DYNAMICS_DIR}/*.yaml'))
            if k == curses.KEY_UP: sel = max(0, sel-1)
            elif k == curses.KEY_DOWN: sel = min(len(dyn_files)-1, sel+1)
            elif k in (10, 13) and dyn_files:
                editor = os.environ.get('EDITOR', 'nano')
                curses.endwin()
                os.system(f'{editor} {dyn_files[sel]}')
                stdscr = curses.initscr(); init_colors(); curses.curs_set(0); stdscr.clear()
            elif k in (ord('a'), ord('A')) and dyn_files:
                fname = os.path.basename(dyn_files[sel]).replace('.yml','').replace('.yaml','')
                dyn_actions = [
                    ('🎨  Art Inject',       'dyn_art_inject'),
                    ('🧹  Art Strip',        'dyn_art_strip'),
                    ('🔧  Repair',           'dyn_repair'),
                    ('⚙  Regenerate',       'dyn_gen'),
                    ('⚙  Force Regen',      'dyn_gen_force'),
                    ('✕  Cancel',           None),
                ]
                result = run_popup_action(stdscr, f'Dynamic: {fname[:20]}', dyn_actions)
                if result and result[1] == 'dyn_art_inject':
                    run_log_popup(stdscr, f'Art inject: {fname}',
                        f'{STACKS_BIN} art dynamic inject {dyn_files[sel]}')
                elif result and result[1] == 'dyn_art_strip':
                    run_log_popup(stdscr, f'Art strip: {fname}',
                        f'{STACKS_BIN} art dynamic strip {dyn_files[sel]}')
                elif result and result[1] == 'dyn_repair':
                    run_log_popup(stdscr, f'Repair: {fname}',
                        f'python3 /usr/local/lib/stacks_repair_dynamic.py {dyn_files[sel]}')
                elif result and result[1] == 'dyn_gen':
                    stack_name = fname + '.yml'
                    run_log_popup(stdscr, f'Gen: {fname}',
                        f'python3 /usr/local/lib/stacks_gen_dynamic.py {stack_name}')
                elif result and result[1] == 'dyn_gen_force':
                    stack_name = fname + '.yml'
                    run_log_popup(stdscr, f'Force gen: {fname}',
                        f'python3 /usr/local/lib/stacks_gen_dynamic.py {stack_name} --force')
        elif tab == 4:  # Art
            if k == curses.KEY_UP: sel = max(0, sel-1)
            elif k == curses.KEY_DOWN: sel = min(len(ART_ITEMS)-1, sel+1)
            elif k in (10, 13):
                action = ART_ITEMS[sel][1]
                if action == 'art_inject_all':
                    run_log_popup(stdscr, 'Art inject ALL', f'{STACKS_BIN} art inject all')
                elif action == 'art_strip_all':
                    run_log_popup(stdscr, 'Art strip ALL', f'{STACKS_BIN} art strip all')
                elif action == 'art_inject_dyn':
                    run_log_popup(stdscr, 'Art inject dynamics', f'{STACKS_BIN} art dynamic inject all')
                elif action == 'art_strip_dyn':
                    run_log_popup(stdscr, 'Art strip dynamics', f'{STACKS_BIN} art dynamic strip all')
                elif action == 'edit_art':
                    curses.endwin()
                    os.system(f'{os.environ.get("EDITOR","nano")} {CONF_DIR}/art.conf')
                    stdscr=curses.initscr(); init_colors(); curses.curs_set(0); stdscr.clear()
                elif action == 'edit_urls':
                    curses.endwin()
                    os.system(f'{os.environ.get("EDITOR","nano")} {CONF_DIR}/stack_urls.conf')
                    stdscr=curses.initscr(); init_colors(); curses.curs_set(0); stdscr.clear()
                elif action == 'gen_dyn_all':
                    run_log_popup(stdscr, 'Gen ALL dynamics', f'python3 /usr/local/lib/stacks_gen_dynamic.py all')
                elif action == 'gen_dyn_force':
                    run_log_popup(stdscr, 'Force regen ALL', f'python3 /usr/local/lib/stacks_gen_dynamic.py all --force')
                elif action == 'repair_dyn':
                    run_log_popup(stdscr, 'Repair ALL dynamics', f'python3 /usr/local/lib/stacks_repair_dynamic.py {DYNAMICS_DIR}')
        elif tab == 5:  # Backup
            if k == curses.KEY_UP: sel = max(0, sel-1)
            elif k == curses.KEY_DOWN: sel = min(len(BACKUP_ITEMS)-1, sel+1)
            elif k in (10, 13):
                action = BACKUP_ITEMS[sel][1]
                if action == 'backup_full':
                    run_log_popup(stdscr, 'Full Backup', f'{STACKS_BIN} backup')
                elif action == 'backup_pre':
                    run_log_popup(stdscr, 'Pre-backup', f'{STACKS_BIN} backup pre')
                elif action == 'backup_log':
                    curses.endwin()
                    os.system(f'{os.environ.get("EDITOR","nano")} /tmp/stacks_backup.log')
                    stdscr=curses.initscr(); init_colors(); curses.curs_set(0); stdscr.clear()
                elif action == 'view_up_log':
                    curses.endwin()
                    os.system(f'{os.environ.get("EDITOR","nano")} {STACKS_DIR}/../stacks_up.log')
                    stdscr=curses.initscr(); init_colors(); curses.curs_set(0); stdscr.clear()
                elif action == 'view_fix_log':
                    curses.endwin()
                    os.system(f'{os.environ.get("EDITOR","nano")} {STACKS_DIR}/../stacks_fix.log')
                    stdscr=curses.initscr(); init_colors(); curses.curs_set(0); stdscr.clear()
                elif action == 'view_build_log':
                    curses.endwin()
                    os.system(f'{os.environ.get("EDITOR","nano")} {STACKS_DIR}/../stacks_build.log')
                    stdscr=curses.initscr(); init_colors(); curses.curs_set(0); stdscr.clear()
                elif action == 'backup_restore':
                    run_log_popup(stdscr, 'Restore', f'{STACKS_BIN} backup restore')
        elif tab == 6:  # Build
            if k == curses.KEY_UP: sel = max(0, sel-1)
            elif k == curses.KEY_DOWN: sel = min(len(BUILD_ITEMS)-1, sel+1)
            elif k in (10, 13):
                action = BUILD_ITEMS[sel][1]
                if action == 'build_new':
                    run_build_wizard(stdscr)
                    stdscr.clear()
                elif action == 'build_new_stack':
                    run_build_wizard(stdscr, new_stack=True)
                    stdscr.clear()
                elif action == 'gen_dyn_all':
                    run_log_popup(stdscr, 'Gen ALL dynamics', f'python3 /usr/local/lib/stacks_gen_dynamic.py all')
                elif action == 'gen_dyn_force':
                    run_log_popup(stdscr, 'Force regen ALL', f'python3 /usr/local/lib/stacks_gen_dynamic.py all --force')
                elif action == 'gen_dyn_one':
                    run_log_popup(stdscr, 'Gen dynamics (stacks with traefik)', f'python3 /usr/local/lib/stacks_gen_dynamic.py all')
                elif action == 'gen_inject':
                    run_log_popup(stdscr, 'Gen global inject', f'python3 /usr/local/lib/stacks_gen_gi.py {CONF_DIR}/global_inject.conf {STACKS_DIR}')
                elif action == 'gen_groups':
                    run_log_popup(stdscr, 'Gen sablier groups', f'{STACKS_BIN} gen srvs')
                elif action == 'fix_all':
                    run_log_popup(stdscr, 'Fix ALL', f'{STACKS_BIN} fix all')
                elif action == 'repair_all':
                    run_log_popup(stdscr, 'Repair ALL', f'python3 /usr/local/lib/stacks_repair.py {STACKS_DIR}')

        elif tab == 8:  # Network
            if k in (ord("a"), ord("A")):
                res = run_popup_action(stdscr, "Network — Edit", NETWORK_EDIT_ACTIONS)
                if res: do_network_action(stdscr, res[1])
                stdscr.clear()
            elif k in (10, 13, ord("s"), ord("S")):
                run_log_popup(stdscr, "Scan collisions", "python3 /usr/local/lib/stacks_collision.py")
                _net_cache["data"] = None
                stdscr.clear()
            elif k in (ord("e"), ord("E")):
                curses.endwin()
                os.system(f'{os.environ.get("EDITOR","nano")} {os.path.expanduser("~/.config/stacks/stacks.yaml")}')
                stdscr = curses.initscr(); init_colors(); curses.curs_set(0); stdscr.clear()
                _net_cache["data"] = None
        elif tab == 9:  # Updates  (lowercase letters are the letter-jump filter)
            items = update_rows
            vis = h - 8
            if k == curses.KEY_UP:
                if sel > 0: sel -= 1
                if sel < scroll: scroll = sel
            elif k == curses.KEY_DOWN:
                if sel < len(items)-1: sel += 1
                if sel >= scroll + vis: scroll = sel - vis + 1
            elif k in (10, 13) and items:
                show_update_detail(stdscr, items[sel])
                stdscr.clear()
            elif k == ord("C"):
                run_log_popup(stdscr, "Check updates", "python3 /usr/local/lib/stacks_updates.py")
                stdscr.clear()
            elif k == ord("F"):
                run_log_popup(stdscr, "Force check", "python3 /usr/local/lib/stacks_updates.py --force")
                stdscr.clear()
            elif k == ord("P"):
                run_log_popup(stdscr, "Pull updates", "python3 /usr/local/lib/stacks_updates.py --pull")
                stdscr.clear()
        elif tab == 7:  # Configs
            if k == curses.KEY_UP:
                cfg_sel = max(0, cfg_sel - 1)
            elif k == curses.KEY_DOWN: cfg_sel = min(len(get_config_items())-1, cfg_sel+1)
            elif k in (10, 13):
                label, fpath, is_dir = get_config_items()[cfg_sel]
                if not is_dir:
                    editor = os.environ.get('EDITOR', 'nano')
                    curses.endwin()
                    os.system(f'{editor} {fpath}')
                    stdscr = curses.initscr(); init_colors(); curses.curs_set(0); stdscr.clear()
                init_colors()
                curses.curs_set(0)
                stdscr.clear()

def run():
    curses.wrapper(main)

if __name__ == '__main__':
    run()
