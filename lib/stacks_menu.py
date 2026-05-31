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

# ── Data layer ───────────────────────────────────────────────────────────────
data_lock = threading.Lock()
app_data = {
    "stacks": [],       # [{name, running, stopped, missing, total}]
    "containers": [],   # [{name, status, image, stack}]
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
            stacks.append({
                'name': name, 'running': running,
                'stopped': stopped, 'total': total,
                'file': path
            })
    except: pass
    return stacks

def get_containers():
    containers = []
    try:
        r = subprocess.run(
            ['docker','ps','-a','--format',
             '{"name":"{{.Names}}","status":"{{.Status}}","image":"{{.Image}}","state":"{{.State}}"}'],
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

def refresh_data():
    while True:
        stacks = get_stacks()
        containers = get_containers()
        with data_lock:
            app_data['stacks'] = stacks
            app_data['containers'] = containers
            app_data['last_update'] = time.time()
        time.sleep(5)

# ── Drawing helpers ──────────────────────────────────────────────────────────
def draw_header(win, title, w):
    try:
        win.attron(curses.color_pair(C_HEADER))
        win.addstr(0, 0, ' ' * (w-1))
        x = (w - len(title)) // 2
        win.addstr(0, max(0,x), title[:w-1])
        win.attroff(curses.color_pair(C_HEADER))
    except: pass

def draw_tabs(win, y, w, tabs, active):
    win.addstr(y, 0, ' ' * w, curses.color_pair(C_DIM))
    x = 2
    for i, tab in enumerate(tabs):
        label = f'  {tab}  '
        if i == active:
            win.addstr(y, x, label, curses.color_pair(C_SELECTED))
        else:
            win.addstr(y, x, label, curses.color_pair(C_DIM))
        x += len(label) + 1

def draw_footer(win, h, w, hints):
    msg = '  '.join(hints)
    try:
        win.attron(curses.color_pair(C_DIM))
        win.addstr(h-1, 0, (' ' * (w-1)))
        win.addstr(h-1, 2, msg[:w-4])
        win.attroff(curses.color_pair(C_DIM))
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

        k = popup.getch()
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
                k = popup.getch()
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
    ("↑  Scale ON",                           "scale_on"),
    ("↓  Scale OFF",                          "scale_off"),
    ("↑  Proxy ON",                           "proxy_on"),
    ("↓  Proxy OFF",                          "proxy_off"),
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

def do_container_action(stdscr, container_name, stack_file, action):
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
    else: return
    run_log_popup(stdscr, f'{action} → {container_name}', cmd)

# ── Tab views ────────────────────────────────────────────────────────────────
TABS = ['Containers', 'Stacks', 'Logs', 'Dynamics', 'Art', 'Backup', 'Build', 'Configs']

def draw_containers_tab(win, h, w, containers, sel, scroll):
    win.addstr(3, 2, f'{"NAME":<35} {"STATUS":<12} {"IMAGE":<30}',
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


        is_running = state.lower() == 'running'
        color = C_RUNNING if is_running else C_STOPPED
        indicator = '●' if is_running else '○'

        line = f'{indicator} {name:<34} {status:<12} {image}'
        if idx == sel:
            win.addstr(y, 2, line[:w-4], curses.color_pair(C_SELECTED))
        else:
            win.addstr(y, 2, f'{indicator} ', curses.color_pair(color))
            win.addstr(y, 4, f'{name:<34} {status:<12} {image}'[:w-6],
                      curses.color_pair(C_NORMAL))

def draw_stacks_tab(win, h, w, stacks, sel, scroll):
    win.addstr(3, 2, f'{"STACK":<25} {"RUN":>4} {"STOP":>5} {"TOTAL":>6} {"STATUS":<10}',
               curses.color_pair(C_ACCENT))
    win.addstr(4, 2, '─' * (w-4), curses.color_pair(C_DIM))

    visible = h - 7
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

        line = f'{name:<25} {running:>4} {stopped:>5} {total:>6}  {status}'
        if idx == sel:
            win.addstr(y, 2, line[:w-4], curses.color_pair(C_SELECTED))
        else:
            win.addstr(y, 2, f'{name:<25} {running:>4} {stopped:>5} {total:>6}  ',
                      curses.color_pair(C_NORMAL))
            win.addstr(y, 2+len(f'{name:<25} {running:>4} {stopped:>5} {total:>6}  '),
                      status[:w-4], curses.color_pair(color))

def draw_logs_tab(win, h, w, log_lines, sel, scroll):
    try:
        win.addstr(3, 2, "DOCKER LOGS", curses.color_pair(C_ACCENT))
        win.addstr(4, 2, "─" * (w-4), curses.color_pair(C_DIM))
        import glob as _glob
        _log_dir = '/srv/stacks'
        sources = [(f.split('/')[-1], f'cat {f}') for f in sorted(_glob.glob(f'{_log_dir}/stacks_*.log'))]
        if not sources: sources = [('No logs found', 'echo No stacks logs found')]
        visible = h - 7
        for i, (label, _) in enumerate(sources):
            y = 5 + i
            if y >= h - 2: break
            if i == sel:
                try: win.addstr(y, 2, f"  ▶  {label:<30}", curses.color_pair(C_SELECTED))
                except: pass
            else:
                try: win.addstr(y, 2, f"     {label:<30}", curses.color_pair(C_NORMAL))
                except: pass
    except: pass
    return sources

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
            if i == sel:
                try: win.addstr(y, 2, f"  ▶  {label:<50}", curses.color_pair(C_SELECTED))
                except: pass
            else:
                try: win.addstr(y, 2, f"     {label:<50}", curses.color_pair(C_NORMAL))
                except: pass
        return files
    except: return []

def draw_art_tab(win, h, w):
    try:
        win.addstr(3, 2, "ART INJECTION", curses.color_pair(C_ACCENT))
        win.addstr(4, 2, "─" * (w-4), curses.color_pair(C_DIM))
        actions = [
            ("I", "Inject art into ALL stacks"),
            ("S", "Strip art from ALL stacks"),
            ("D", "Inject art into ALL dynamics"),
            ("X", "Strip art from ALL dynamics"),
            ("E", "Edit art.conf (art config)"),
            ("G", "Generate dynamics from ALL stacks"),
            ("F", "Force regenerate ALL (overwrite)"),
            ("R", "Repair ALL dynamic configs"),
            ("U", "Edit stack_urls.conf (URLs config)"),
        ]
        for i, (key, desc) in enumerate(actions):
            try:
                win.addstr(6+i, 4, f"[{key}]", curses.color_pair(C_ACCENT))
                win.addstr(6+i, 9, desc, curses.color_pair(C_NORMAL))
            except: pass
    except: pass

def draw_backup_tab(win, h, w):


    win.addstr(3, 2, 'BACKUP', curses.color_pair(C_ACCENT))
    win.addstr(4, 2, '─' * (w-4), curses.color_pair(C_DIM))
    actions = [
        ('B', 'Run full backup now'),
        ('P', 'Run pre-backup snapshot'),
        ('L', 'View backup log'),
        ('R', 'Restore from backup'),
    ]
    for i, (key, desc) in enumerate(actions):
        win.addstr(6+i, 4, f'[{key}]', curses.color_pair(C_ACCENT))
        win.addstr(6+i, 9, desc, curses.color_pair(C_NORMAL))

def draw_build_tab(win, h, w):
    win.addstr(3, 2, 'BUILD', curses.color_pair(C_ACCENT))
    win.addstr(4, 2, '─' * (w-4), curses.color_pair(C_DIM))
    actions = [
        ('N', 'New stack from template'),
        ('A', 'Add service to stack'),
        ('G', 'Generate dynamic config'),
        ('I', 'Generate global inject'),
    ]
    for i, (key, desc) in enumerate(actions):
        win.addstr(6+i, 4, f'[{key}]', curses.color_pair(C_ACCENT))
        win.addstr(6+i, 9, desc, curses.color_pair(C_NORMAL))

CONFIG_FILES = [
    ("stacks.conf",        "stacks.conf"),
    ("global_inject.conf", "global_inject.conf"),
    ("menu.conf",     "menu.conf"),
    ("stack_urls.conf",    "stack_urls.conf"),
    ("backup.conf",        "backup.conf"),
    ("build.conf",         "build.conf"),
]

def draw_configs_tab(win, h, w, sel):

    win.addstr(3, 2, 'CONFIGS', curses.color_pair(C_ACCENT))
    win.addstr(4, 2, '─' * (w-4), curses.color_pair(C_DIM))
    for i, (label, _) in enumerate(CONFIG_FILES):
        y = 6 + i
        if i == sel:
            win.addstr(y, 2, f'  {label:<30}', curses.color_pair(C_SELECTED))
        else:
            win.addstr(y, 2, f'  {label:<30}', curses.color_pair(C_NORMAL))

# ── Main TUI ─────────────────────────────────────────────────────────────────
def main(stdscr):
    init_colors()
    curses.curs_set(0)
    stdscr.nodelay(False)
    stdscr.timeout(1000)
    try: curses.mousemask(curses.ALL_MOUSE_EVENTS)
    except: pass

    # Start background data refresh
    t = threading.Thread(target=refresh_data, daemon=True)
    t.start()

    # Wait for first data load
    stdscr.addstr(0, 0, 'Loading...', curses.color_pair(C_DIM))
    stdscr.refresh()
    while True:
        with data_lock:
            if app_data['last_update'] > 0:
                break
        time.sleep(0.1)

    tab    = 0   # 0=containers 1=stacks 2=backup 3=build 4=configs
    sel    = 0
    scroll = 0
    cfg_sel = 0

    FOOTER_HINTS = {
        0: ['↑↓ Navigate', '↔ Switch Tab', 'ENTER Action', 'Q Quit'],
        1: ['↑↓ Navigate', '↔ Switch Tab', 'ENTER Action', 'A All-Stacks', 'Q Quit'],
        2: ['↑↓ Select', '↔ Tab', 'ENTER Open', 'Q Quit'],
        3: ['↑↓ Select', '↔ Tab', 'ENTER Edit', 'A Inject Art', 'Q Quit'],
        4: ['I Inject All', 'S Strip All', 'D Dyn Inject', 'X Dyn Strip', 'E Edit Art Conf', 'Q Quit'],
        6: ['N/Enter Build', 'G Gen Dynamics', 'D Gen All', 'I Gen Inject', 'Q Quit'],
        2: ['↔ Switch Tab', 'Q Quit'],
        3: ['↔ Switch Tab', 'Q Quit'],
        4: ['↑↓ Navigate', '↔ Switch Tab', 'ENTER Edit', 'Q Quit'],
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

        # Tabs
        draw_tabs(stdscr, 2, w, TABS, tab)

        # Content
        with data_lock:
            containers = list(app_data['containers'])
            stacks     = list(app_data['stacks'])

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
            draw_art_tab(stdscr, h, w)
        elif tab == 5:
            draw_backup_tab(stdscr, h, w)
        elif tab == 6:
            draw_build_tab(stdscr, h, w, sel)
        elif tab == 7:
            draw_configs_tab(stdscr, h, w, cfg_sel)

        draw_footer(stdscr, h, w, FOOTER_HINTS.get(tab, []))
        stdscr.refresh()

        k = stdscr.getch()
        if k == -1: continue
        if k == curses.KEY_RESIZE:
            stdscr.clear()
            continue

        # Global keys
        if k in (ord('q'), ord('Q')): break
        if k == curses.KEY_RIGHT:
            tab = (tab + 1) % len(TABS)
            sel = 0; scroll = 0
            continue
        if k == curses.KEY_LEFT:
            tab = (tab - 1) % len(TABS)
            sel = 0; scroll = 0
            continue

        # Tab-specific keys
        if tab == 0:  # Containers
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
            elif k in (ord('a'), ord('A')):
                result = run_popup_action(stdscr, 'ALL Stacks', GLOBAL_ACTIONS)
                if result and result[1]:
                    do_global_action(stdscr, result[1])

        elif tab == 2:  # Logs
            import glob as _glob
            _log_dir = '/srv/stacks'
            _log_files = sorted(_glob.glob(f'{_log_dir}/stacks_*.log'))
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
            if k in (ord('i'), ord('I')):
                run_log_popup(stdscr, 'Art inject ALL stacks', f'{STACKS_BIN} art inject all')
            elif k in (ord('s'), ord('S')):
                run_log_popup(stdscr, 'Art strip ALL stacks', f'{STACKS_BIN} art strip all')
            elif k in (ord('d'), ord('D')):
                run_log_popup(stdscr, 'Art inject ALL dynamics', f'{STACKS_BIN} art dynamic inject all')
            elif k in (ord('x'), ord('X')):
                run_log_popup(stdscr, 'Art strip ALL dynamics', f'{STACKS_BIN} art dynamic strip all')
            elif k in (ord('e'), ord('E')):
                editor = os.environ.get('EDITOR', 'nano')
                curses.endwin()
                os.system(f'{editor} {CONF_DIR}/art.conf')
                stdscr = curses.initscr(); init_colors(); curses.curs_set(0); stdscr.clear()
            elif k in (ord('g'), ord('G')):
                run_log_popup(stdscr, 'Generate ALL dynamics',
                    f'python3 /usr/local/lib/stacks_gen_dynamic.py all')
            elif k in (ord('f'), ord('F')):
                run_log_popup(stdscr, 'Force regen ALL dynamics',
                    f'python3 /usr/local/lib/stacks_gen_dynamic.py all --force')
            elif k in (ord('r'), ord('R')):
                run_log_popup(stdscr, 'Repair ALL dynamics',
                    f'python3 /usr/local/lib/stacks_repair_dynamic.py {DYNAMICS_DIR}')
            elif k in (ord('u'), ord('U')):
                editor = os.environ.get('EDITOR', 'nano')
                curses.endwin()
                os.system(f'{editor} {CONF_DIR}/stack_urls.conf')
                stdscr = curses.initscr(); init_colors(); curses.curs_set(0); stdscr.clear()
        elif tab == 5:  # Backup
            if k == ord('b') or k == ord('B'):
                run_log_popup(stdscr, 'Backup', f'{STACKS_BIN} backup')
            elif k == ord('p') or k == ord('P'):
                run_log_popup(stdscr, 'Pre-backup', f'{STACKS_BIN} backup pre')
            elif k == ord('l') or k == ord('L'):
                run_log_popup(stdscr, 'Backup Log', 'cat /tmp/stacks_backup.log 2>/dev/null || echo "No log found"')

        elif tab == 6:  # Build
            if k == curses.KEY_UP: sel = max(0, sel-1)
            elif k == curses.KEY_DOWN: sel = min(len(BUILD_ITEMS)-1, sel+1)
            elif k in (10, 13):
                action = BUILD_ITEMS[sel][1]
                if action == 'build_new':
                    curses.endwin()
                    os.system(f'{STACKS_BIN} build')
                    input('\nPress Enter to return to menu...')
                    stdscr = curses.initscr(); init_colors(); curses.curs_set(0); stdscr.clear()
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

        elif tab == 7:  # Configs
            if k == curses.KEY_UP:
                cfg_sel = max(0, cfg_sel - 1)
            elif k == curses.KEY_DOWN:
                cfg_sel = min(len(CONFIG_FILES)-1, cfg_sel + 1)
            elif k in (10, 13):
                _, fname = CONFIG_FILES[cfg_sel]
                fpath = os.path.join(CONF_DIR, fname)
                editor = os.environ.get('EDITOR', 'nano')
                curses.endwin()
                os.system(f'{editor} {fpath}')
                stdscr = curses.initscr()
                init_colors()
                curses.curs_set(0)
                stdscr.clear()

def run():
    curses.wrapper(main)

if __name__ == '__main__':
    run()
