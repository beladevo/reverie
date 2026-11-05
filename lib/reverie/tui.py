from __future__ import annotations

import curses
import locale
import os
import time
from pathlib import Path

from . import dashboard as views
from . import dream

TABS = ("Overview", "Memory", "Dreams", "Sessions")
EMPTY = {1: "no memory stores attached", 2: "no dreams yet, press n to dream now", 3: "no sessions waiting to be dreamed over"}
REFRESH_SECONDS = 2
MIN_SIZE = (12, 60)
HINTS = {
    "overview": "1-4 tabs · ↑↓ PgUp PgDn scroll · n dream now · r refresh · q quit",
    "list": "1-4 tabs · ↑↓ select · → open · PgUp PgDn scroll · n dream now · q quit",
    "history": "↑↓ version · ← back · PgUp PgDn scroll · q quit",
    "ops": "↑↓ change · a accept · x reject · ← back · PgUp PgDn scroll · q quit",
    "detail": "↑↓ PgUp PgDn scroll · ← back · q quit",
}


def _styles() -> dict:
    attrs = {"title": curses.A_BOLD, "dim": curses.A_DIM, "sel": curses.A_REVERSE}
    if not curses.has_colors():
        return attrs
    curses.start_color()
    try:
        curses.use_default_colors()
        background = -1
    except curses.error:
        background = curses.COLOR_BLACK
    palette = {"title": (curses.COLOR_CYAN, curses.A_BOLD), "ok": (curses.COLOR_GREEN, 0),
               "warn": (curses.COLOR_YELLOW, 0), "bad": (curses.COLOR_RED, 0), "accent": (curses.COLOR_MAGENTA, 0),
               "add": (curses.COLOR_GREEN, 0), "del": (curses.COLOR_RED, 0)}
    for pair, (name, (color, extra)) in enumerate(palette.items(), start=1):
        curses.init_pair(pair, color, background)
        attrs[name] = curses.color_pair(pair) | extra
    return attrs


class App:
    def __init__(self, screen, cwd: Path):
        self.screen, self.cwd = screen, cwd
        self.attrs = _styles()
        self.tab = 0
        self.cursor = {1: 0, 2: 0, 3: 0}
        self.top = {1: 0, 2: 0, 3: 0}
        self.focus_detail = False
        self.detail_index = 0
        self.scroll = 0
        self.confirm = None
        self.message = ("", "")
        self.items = {1: [], 2: [], 3: []}
        self.load()

    def _key(self, tab: int, item) -> object:
        if tab == 1:
            return item.key
        return item.get("id") if tab == 2 else item.get("session_id")

    def load(self) -> None:
        selected = {tab: self._key(tab, items[self.cursor[tab]]) for tab, items in self.items.items() if items}
        self.snap = views.take(self.cwd)
        self.loaded_at = time.monotonic()
        self.items = {1: views.memory_nodes(self.snap), 2: views.dream_rows(self.snap), 3: self.snap.waiting}
        for tab, items in self.items.items():
            keys = [self._key(tab, item) for item in items]
            if selected.get(tab) in keys:
                self.cursor[tab] = keys.index(selected[tab])
            self.cursor[tab] = min(self.cursor[tab], max(len(items) - 1, 0))

    def run(self) -> None:
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        self.screen.keypad(True)
        self.screen.timeout(500)
        while True:
            self.draw()
            key = self.screen.getch()
            if key == -1:
                if time.monotonic() - self.loaded_at >= REFRESH_SECONDS:
                    self.load()
                continue
            if not self.handle(key):
                return

    def selected(self):
        items = self.items.get(self.tab) or []
        return items[self.cursor[self.tab]] if items else None

    def mode(self) -> str:
        if self.tab == 0:
            return "overview"
        if not self.focus_detail:
            return "list"
        item = self.selected()
        if self.tab == 1 and item is not None and item.kind == "file":
            return "history"
        return "ops" if self.tab == 2 and item is not None and item.get("ops") else "detail"

    def detail(self, width: int) -> tuple:
        if self.tab == 0:
            return views.overview(self.snap, width), None, 0
        item = self.selected()
        if item is None:
            return [(EMPTY[self.tab], "dim")], None, 0
        mode = self.mode()
        if self.tab == 1:
            if mode == "history":
                return views.history_view(self.snap, item.att, item.rel, self.detail_index, width)
            return views.node_view(self.snap, item, width), None, 0
        if self.tab == 2:
            return views.dream_view(self.snap, item, self.detail_index if mode == "ops" else None, width)
        return views.session_view(self.snap, item, width), None, 0

    def handle(self, key: int) -> bool:
        if self.confirm:
            _, action = self.confirm
            self.confirm = None
            if key in (ord("y"), ord("Y")):
                self.perform(action)
            else:
                self.message = ("cancelled", "dim")
            return True
        self.message = ("", "")
        if key == ord("q"):
            return False
        if ord("1") <= key <= ord("4"):
            self.switch(key - ord("1"))
        elif key == 9:
            self.switch((self.tab + 1) % len(TABS))
        elif key == curses.KEY_BTAB:
            self.switch((self.tab - 1) % len(TABS))
        elif key in (curses.KEY_UP, ord("k")):
            self.move(-1)
        elif key in (curses.KEY_DOWN, ord("j")):
            self.move(1)
        elif key in (curses.KEY_RIGHT, ord("l"), 10, 13, curses.KEY_ENTER):
            if self.tab and self.selected() is not None and not self.focus_detail:
                self.focus_detail, self.detail_index, self.scroll = True, 0, 0
        elif key in (curses.KEY_LEFT, ord("h"), 27):
            self.focus_detail, self.scroll = False, 0
        elif key in (curses.KEY_NPAGE, ord(" ")):
            self.scroll += self.page()
        elif key == curses.KEY_PPAGE:
            self.scroll = max(0, self.scroll - self.page())
        elif key == ord("r"):
            self.load()
            self.message = ("refreshed", "dim")
        elif key == ord("n"):
            self.ask("Start a dream for this project now? It runs in the background on your Claude login. (y/n)", ("dream",))
        elif key in (ord("a"), ord("x")):
            self.ask_decision(accept=key == ord("a"))
        return True

    def switch(self, tab: int) -> None:
        self.tab, self.focus_detail, self.detail_index, self.scroll = tab, False, 0, 0

    def move(self, delta: int) -> None:
        mode = self.mode()
        if mode in ("history", "ops"):
            _, _, count = self.detail(80)
            self.detail_index = min(max(self.detail_index + delta, 0), max(count - 1, 0))
        elif mode in ("overview", "detail"):
            self.scroll = max(0, self.scroll + delta)
        elif self.items[self.tab]:
            self.cursor[self.tab] = min(max(self.cursor[self.tab] + delta, 0), len(self.items[self.tab]) - 1)
            self.scroll = 0

    def page(self) -> int:
        return max(self.screen.getmaxyx()[0] - 6, 1)

    def ask(self, question: str, action: tuple) -> None:
        self.confirm = (question, action)

    def ask_decision(self, accept: bool) -> None:
        if self.mode() != "ops":
            self.message = ("open a dream and select a change first (→)", "warn")
            return
        record = self.selected()
        op = record["ops"][min(self.detail_index, len(record["ops"]) - 1)]
        if op["status"] != "pending":
            self.message = (f"#{op['id']} is {op['status']}; only pending changes can be decided", "warn")
            return
        verb = "Accept" if accept else "Reject"
        self.ask(f"{verb} #{op['id']} {op['op']} {op['store']}/{op['path']}? (y/n)", ("decide", record["id"], op["id"], accept))

    def perform(self, action: tuple) -> None:
        try:
            if action[0] == "dream":
                started = dream.spawn(self.snap.project, str(self.cwd), "dashboard")
                self.message = ("dream started" if started else "a dream is already running", "ok" if started else "warn")
            else:
                _, dream_id, op_id, accept = action
                record = dream.decide(dream_id, [op_id], accept)
                status = next(op["status"] for op in record["ops"] if op["id"] == op_id)
                self.message = (f"#{op_id} {status}", views.STATUS_STYLE.get(status, ""))
        except Exception as error:
            self.message = (f"failed: {error}", "bad")
        self.load()

    def put(self, y: int, x: int, text: str, style: str, width: int) -> None:
        if width <= 0:
            return
        try:
            self.screen.addnstr(y, x, text.replace("\t", "    "), width, self.attrs.get(style, 0))
        except curses.error:
            pass

    def draw(self) -> None:
        self.screen.erase()
        height, width = self.screen.getmaxyx()
        if height < MIN_SIZE[0] or width < MIN_SIZE[1]:
            self.put(0, 0, f"Make the terminal at least {MIN_SIZE[1]}×{MIN_SIZE[0]}", "warn", width)
            self.screen.refresh()
            return
        self.draw_tabs(width)
        body_h = height - 4
        if self.tab == 0:
            self.draw_detail(2, 1, body_h, width - 2)
        else:
            left_w = min(max(32, width * 2 // 5), width - 30)
            self.draw_list(2, 0, body_h, left_w)
            for y in range(2, 2 + body_h):
                self.put(y, left_w, "│", "dim", 1)
            self.draw_detail(2, left_w + 2, body_h, width - left_w - 3)
        self.draw_footer(height - 1, width)
        self.screen.refresh()

    def draw_tabs(self, width: int) -> None:
        badges = {2: views.pending_count(self.snap), 3: len(self.snap.waiting)}
        x = 0
        self.put(0, x, " Reverie ", "title", width)
        x += 10
        for i, name in enumerate(TABS):
            badge = f" ({badges[i]})" if badges.get(i) else ""
            if i == 2 and self.snap.running is not None:
                badge += " ●"
            label = f" {i + 1} {name}{badge} "
            self.put(0, x, label, "sel" if i == self.tab else "", width - x)
            x += len(label) + 1
        subtitle = f"{self.snap.project.label} · updated {time.strftime('%H:%M:%S')}"
        self.put(1, 0, subtitle, "dim", width)

    def draw_list(self, y: int, x: int, height: int, width: int) -> None:
        items = self.items[self.tab]
        if self.tab == 3:
            self.put(y, x + 1, views.sessions_header(self.snap), "dim", width - 2)
            y, height = y + 1, height - 1
        if not items:
            self.put(y, x + 1, EMPTY[self.tab], "dim", width - 2)
            return
        cursor = self.cursor[self.tab]
        top = self.top[self.tab]
        top = cursor if cursor < top else cursor - height + 1 if cursor >= top + height else top
        self.top[self.tab] = top
        for row, item in enumerate(items[top:top + height]):
            if self.tab == 1:
                text, style = views.node_label(self.snap, item)
            elif self.tab == 2:
                text, style = views.dream_label(item)
            else:
                text, style = views.session_label(item)
            if top + row == cursor:
                style = "sel" if not self.focus_detail else "title"
                text = ("› " if self.focus_detail else "  ") + text
            else:
                text = "  " + text
            self.put(y + row, x, text.ljust(width), style, width)

    def draw_detail(self, y: int, x: int, height: int, width: int) -> None:
        rows, anchor, _ = self.detail(width)
        if anchor is not None:
            if anchor < self.scroll:
                self.scroll = anchor
            elif anchor >= self.scroll + height:
                self.scroll = anchor - height + 1
        self.scroll = min(self.scroll, max(len(rows) - height, 0))
        for row, (text, style) in enumerate(rows[self.scroll:self.scroll + height]):
            self.put(y + row, x, text, style, width)
        if len(rows) > height:
            self.put(y + height - 1, x + width - 6, f" {min(self.scroll + height, len(rows)) * 100 // len(rows):>3}% ", "dim", 6)

    def draw_footer(self, y: int, width: int) -> None:
        if self.confirm:
            self.put(y, 0, self.confirm[0], "warn", width)
        elif self.message[0]:
            self.put(y, 0, self.message[0], self.message[1], width)
        else:
            self.put(y, 0, HINTS[self.mode()], "dim", width)


def run(cwd: Path) -> None:
    os.environ.setdefault("ESCDELAY", "25")
    locale.setlocale(locale.LC_ALL, "")
    curses.wrapper(lambda screen: App(screen, cwd).run())
