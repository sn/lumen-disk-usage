"""What the Disk Usage pane shows, as lines of text. Nothing here talks to
Lumen, so it can be tested on its own: main.py holds the state and hands it
to `render`.
"""

from __future__ import annotations

import shlex
import shutil
import sys
import time
import unicodedata
from dataclasses import dataclass

from scan import Culprit, Item, ago, clean, human, tilde

DIM, BOLD, OFF = "\x1b[2m", "\x1b[1m", "\x1b[0m"
ACCENT, RED, INVERT = "\x1b[34m", "\x1b[31m", "\x1b[7m"

HINTS = [
    "↑↓ move · enter expand · c copy path · x copy command · r rescan · q close",
    "↑↓ · enter expand · c path · x command · r rescan · q close",
    "enter · c · x · r · q",
]


def width(s: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 0 if unicodedata.combining(c) else 1 for c in s)


def fit(s: str, w: int) -> str:
    """`s` cut to `w` columns with "…", or padded to them."""
    if w <= 0:
        return ""
    if width(s) <= w:
        return s + " " * (w - width(s))
    out, used = "", 0
    for c in s:
        cw = width(c)
        if used + cw > w - 1:
            break
        out, used = out + c, used + cw
    return out + "…" + " " * (w - 1 - used)


def shorten(path: str, w: int) -> str:
    """`path` cut to `w` columns in the middle, keeping its end: the last
    part of a path is what tells folders apart."""
    if w <= 0:
        return ""
    if width(path) <= w:
        return path
    if w == 1:
        return "…"
    tail_w = (w - 1) * 2 // 3
    tail, used = "", 0
    for c in reversed(path):
        if used + width(c) > tail_w:
            break
        tail, used = c + tail, used + width(c)
    head, room = "", w - 1 - used
    for c in path:
        if width(c) > room:
            break
        head, room = head + c, room - width(c)
    return head + "…" + tail


def wrap(text: str, w: int, most: int = 2) -> list:
    """`text` on up to `most` lines of `w` columns, the last cut with "…"."""
    lines = []
    while text and len(lines) < most:
        if width(text) <= w:
            lines.append(text)
            break
        if len(lines) == most - 1:
            lines.append(shorten(text, w))
            break
        cut, used = 0, 0
        for c in text:
            if used + width(c) > w:
                break
            cut, used = cut + 1, used + width(c)
        lines.append(text[:cut])
        text = text[cut:]
    return lines


@dataclass
class Row:
    culprit: Culprit
    item: Item | None = None  # None: the culprit's own row


def rows(culprits, expanded) -> list:
    """The list as shown: each culprit, then its items if it's expanded."""
    out = []
    for c in culprits:
        out.append(Row(c))
        if c.id in expanded:
            out += [Row(c, i) for i in c.items]
    return out


def command_text(words, quote=shlex.quote) -> str:
    return " ".join(quote(w) for w in words)


def copy_text(row: Row, kind: str, quote=shlex.quote, typeable=lambda text: True):
    """(text to copy, None) or (None, why not). `kind` is "path" (a row
    with no path gives its name) or "command"."""
    target = row.item if row.item is not None else row.culprit
    if kind == "command":
        words = target.command
        if not words:
            return None, "Nothing to clean this up with: copy its path instead"
    else:
        one = getattr(target, "path", None) or getattr(target, "name", None)
        if not one:
            return None, "This row has no path: expand it and choose an item"
        words = [one]
    if not all(typeable(w) for w in words):
        return None, "Not copied: the name has control or invisible characters"
    try:
        return command_text(words, quote), None
    except ValueError:
        return None, "Not copied: the name has control or invisible characters"


def clipboard_command(env, which=shutil.which, platform=sys.platform):
    """The program that takes text on stdin and puts it on the clipboard."""
    if platform == "darwin":
        choices = [["pbcopy"]]
    else:
        choices = []
        if env.get("WAYLAND_DISPLAY"):
            choices.append(["wl-copy"])
        if env.get("DISPLAY"):
            choices += [["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]]
    return next((c for c in choices if which(c[0])), None)


def _size(kb, exact, pending) -> str:
    if kb is None:
        return "…" if pending else "?"
    return ("" if exact else "≥") + human(kb)


def bar(percent: int, w: int) -> str:
    full = max(0, min(w, round(w * percent / 100)))
    return "█" * full + "░" * (w - full)


def drive_lines(drives, cols: int, warn: int) -> list:
    out = []
    name_w = min(max([width(d.mount) for d in drives] + [1]), max(8, cols // 4))
    for d in drives:
        text = f"{human(d.used_kb)} of {human(d.total_kb)} · {human(d.free_kb)} free"
        tail = f"  {text}  {d.percent:>3}%"
        bar_w = min(30, cols - name_w - width(tail) - 4)
        if bar_w < 6:
            tail, bar_w = f"  {human(d.free_kb)} free  {d.percent:>3}%", max(0, min(30, cols - name_w - 22))
        colour = RED if d.percent >= warn else ACCENT
        if name_w + 2 + bar_w + width(tail) + 1 > cols:  # too narrow for a bar
            line = fit(f" {clean(d.mount)}  {d.percent}%  {human(d.free_kb)} free", cols)
            out.append(f"{RED}{line}{OFF}" if d.percent >= warn else line)
            continue
        out.append(f" {fit(clean(d.mount), name_w)}  {colour}{bar(d.percent, bar_w)}{OFF}{colour if d.percent >= warn else ''}{tail}{OFF}")
    return out


def culprit_line(c: Culprit, open_: bool, cols: int, now: float) -> str:
    pending = c.state == "pending"
    mark = "▾" if open_ else "▸" if c.items or c.state == "idle" else " "
    if c.state == "unavailable":
        size, note = "—", c.note
    elif c.state == "idle":
        size, note = "", "enter to measure (this one takes a while)"
    elif c.id == "home":
        size, note = "", "measuring…" if pending else f"measured {ago(c.at, now)} ago".replace("now ago", "just now")
    else:
        size = _size(c.kb, c.exact, pending) + ("…" if pending and c.kb is not None else "")
        if c.reclaim_kb is not None:
            note = f"{human(c.reclaim_kb)} reclaimable"
        else:
            note = c.note or f"{len(c.items)} {c.unit}"
    title_w = max(12, min(26, cols - 34))
    return fit(f" {mark} {fit(c.title, title_w)}{size:>9}   {note}", cols)


def item_line(c: Culprit, i: Item, cols: int, home: str, now: float) -> str:
    size = _size(i.kb, i.exact, c.state == "pending")
    age = f"{ago(i.changed, now):>8}" if cols >= 60 else ""
    lead = f"    {size:>9} {age}  "
    room = cols - width(lead)
    if i.path:
        text = shorten(tilde(clean(i.path), home), room)
    else:
        extra = []
        if i.reclaim_kb is not None:
            extra.append(f"{human(i.reclaim_kb)} reclaimable")
        if i.note:
            extra.append(i.note)
        tail = "   " + " · ".join(extra) if extra else ""
        text = shorten(clean(i.label), max(8, room - width(tail))) + tail
    return fit(lead + text, cols)


def detail_lines(row, cols: int, quote=shlex.quote) -> list:
    """The selected row in full: its path (or name), then its command."""
    if row is None:
        return ["", ""]
    target = row.item if row.item is not None else row.culprit
    one = getattr(target, "path", None) or getattr(target, "name", None)
    out = []
    if one:
        out += [" " + line for line in wrap(clean(one), cols - 2, 1 if target.command else 2)]
    if target.command:
        try:
            text = command_text(target.command, quote)
        except ValueError:
            text = clean(" ".join(target.command))
        out += [f"{DIM} {line}{OFF}" for line in wrap(clean(text), cols - 2, 2 - len(out))]
    return (out + ["", ""])[:2]


def render(
    *,
    cols: int,
    rows_: int,
    drives,
    culprits,
    expanded,
    selected: int,
    top: int,
    status: str = "",
    home: str = "",
    warn: int = 90,
    now=None,
    quote=shlex.quote,
):
    """(lines, top): the whole pane, and where the list is scrolled to so
    that the selected row shows."""
    now = time.time() if now is None else now
    title = " Disk Usage"
    gap = cols - width(title) - width(status) - 1
    head = f"{BOLD}{title}{OFF}" + (f"{' ' * gap}{DIM}{status}{OFF}" if gap > 0 else "")
    out = [head] + drive_lines(drives, cols, warn) + [""]
    listed = rows(culprits, expanded)
    body = max(1, rows_ - len(out) - 4)
    selected = max(0, min(selected, len(listed) - 1))
    if selected < top:
        top = selected
    elif selected >= top + body:
        top = selected - body + 1
    top = max(0, min(top, max(0, len(listed) - body)))
    if not listed:
        out.append(f"{DIM} {status and 'Looking for what is using the space…' or 'Nothing found yet. Press r to scan.'}{OFF}")
    for n, row in enumerate(listed[top:top + body], start=top):
        if row.item is None:
            line = culprit_line(row.culprit, row.culprit.id in expanded, cols, now)
            style = BOLD
        else:
            line = item_line(row.culprit, row.item, cols, home, now)
            style = DIM if row.item.counted else ""
        if n == selected:
            line = f"{INVERT}{line}{OFF}"
        elif style:
            line = f"{style}{line}{OFF}"
        out.append(line)
    out = out[: max(1, rows_ - 4)]
    out += [""] * (rows_ - 3 - len(out))
    out += detail_lines(listed[selected] if listed else None, cols, quote)
    hint = next((h for h in HINTS if width(h) <= cols - 2), "")
    return out + [f"{DIM} {hint}{OFF}"], top
