"""Disk Usage: how full your drives are, and what's using the space.

Choose Disk Usage in the palette. The drives are at the top; below them are
the usual culprits, biggest first: Docker, Rust build output and toolchains,
AI models, node_modules, package caches and Trash. Enter expands one into
its items, each with its size and path. The row you're on is shown in full
at the bottom, with the command that cleans it up where there is one.

Keys: ↑/↓ (or j/k) move, Enter or → expands, ← collapses, c copies the path,
x copies the cleanup command, r scans again, q closes.

It only looks: nothing is deleted and no command is run for you. The last
scan is kept in the plugin's data folder, so it opens at once. You're told
when a drive passes `warn-percent` full, whether or not the tab is open.
"""

import asyncio
import os
import subprocess
import time

import scan
import view
from lumen_plugin import LumenError, Plugin, quote, typeable

app = Plugin()

HOME = os.path.expanduser("~")
SAVED = os.path.join(app.data_dir, "scan.json")
STALE = 3600  # a saved scan older than this is redone when the tab opens
CHECK_EVERY = 300  # seconds between looks at how full the drives are


def setting(name, default):
    value = app.settings.get(name)
    return default if value is None else value


class Screen:
    """The tab: what's shown in it and what its keys do."""

    def __init__(self, pane):
        self.pane = pane
        self.drives = scan.drives()
        self.saved = scan.load(SAVED)  # the last finished scan
        self.live = None  # culprits of a scan in progress
        self.home = self.saved["home"] if self.saved else None
        if self.home and self.home.state != "ok":
            self.home = None  # saved partway through measuring
        self.expanded = set()
        self.selected = 0
        self.top = 0
        self.scanning = False
        self.measuring_home = False
        self.flash = ""  # said once, in place of the status
        self.redraw_due = False

    # What's shown.
    def culprits(self):
        if self.saved:
            found = self.saved["culprits"]
        else:
            found = self.live or []
        home = self.home or scan.Culprit("home", "Largest in home", unit="entries", state="idle")
        return list(found) + ([home] if found or self.saved else [])

    def listed(self):
        return view.rows(self.culprits(), self.expanded)

    def status(self):
        if self.flash:
            return self.flash
        if self.scanning:
            return "scanning…"
        if self.saved:
            return f"scanned {scan.ago(self.saved['at'])} ago".replace("now ago", "just now")
        return ""

    async def show(self):
        if self.pane.closed:
            return
        lines, self.top = view.render(
            cols=self.pane.cols,
            rows_=self.pane.rows,
            drives=self.drives,
            culprits=self.culprits(),
            expanded=self.expanded,
            selected=self.selected,
            top=self.top,
            status=self.status(),
            home=HOME,
            warn=setting("warn-percent", 90),
            quote=quote,
        )
        try:
            await self.pane.draw(lines)
        except LumenError:
            pass

    def show_soon(self):
        """Redraw shortly: sizes arrive in bursts, from other threads."""
        if self.redraw_due:
            return
        self.redraw_due = True

        async def later():
            await asyncio.sleep(0.2)
            self.redraw_due = False
            await self.show()

        asyncio.ensure_future(later())

    # Scanning.
    async def rescan(self):
        if self.scanning:
            return
        self.scanning = True
        self.drives = scan.drives()
        await self.show()
        loop = asyncio.get_running_loop()

        def progress(culprits):
            def apply():
                self.live = culprits
                self.show_soon()

            loop.call_soon_threadsafe(apply)

        try:
            settings = dict(app.settings)
            done = await loop.run_in_executor(None, lambda: scan.scan(settings, HOME, os.environ, progress))
            done["home"] = self.home
            self.saved = done
            scan.save(SAVED, done)
        except Exception as e:  # a scan that fails mustn't take the tab with it
            app.log("scan failed:", repr(e))
            self.flash = "The scan failed: see Plugin Logs"
        finally:
            self.scanning, self.live = False, None
        self.drives = scan.drives()
        await update_badge(self.drives)
        await self.show()

    async def measure_home(self):
        if self.measuring_home:
            return
        self.measuring_home = True
        self.home = scan.Culprit("home", "Largest in home", unit="entries", state="pending")
        await self.show()
        counted = [i.path for c in (self.saved or {}).get("culprits", []) for i in c.items if i.path]
        loop = asyncio.get_running_loop()
        try:
            self.home = await loop.run_in_executor(None, lambda: scan.largest_in_home(HOME, counted))
            if self.saved:
                self.saved["home"] = self.home
                scan.save(SAVED, self.saved)
        except Exception as e:
            app.log("measuring home failed:", repr(e))
            self.home, self.flash = None, "Couldn't measure home: see Plugin Logs"
        finally:
            self.measuring_home = False
        await self.show()

    # Keys.
    def copy(self, kind):
        listed = self.listed()
        if not listed:
            return
        text, problem = view.copy_text(listed[self.selected], kind, quote, typeable)
        if problem:
            self.flash = problem
            return
        program = view.clipboard_command(os.environ)
        if not program:
            self.flash = "No clipboard program found (wl-copy, xclip, xsel or pbcopy)"
            return
        try:
            subprocess.run(
                program,
                input=text.encode(),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=True,
            )
            self.flash = "Copied the command" if kind == "command" else "Copied"
        except (OSError, subprocess.SubprocessError) as e:
            app.log("couldn't copy:", repr(e))
            self.flash = f"Couldn't copy with {program[0]}"

    async def key(self, name):
        self.flash = ""
        listed = self.listed()
        self.selected = max(0, min(self.selected, len(listed) - 1))
        row = listed[self.selected] if listed else None
        page = max(1, self.pane.rows - 10)
        if name in ("down", "j"):
            self.selected = min(len(listed) - 1, self.selected + 1)
        elif name in ("up", "k"):
            self.selected = max(0, self.selected - 1)
        elif name == "pagedown":
            self.selected = min(len(listed) - 1, self.selected + page)
        elif name == "pageup":
            self.selected = max(0, self.selected - page)
        elif name == "home":
            self.selected = 0
        elif name == "end":
            self.selected = len(listed) - 1
        elif name in ("enter", "right", "l") and row and row.item is None:
            c = row.culprit
            if c.id in self.expanded and name == "enter":
                self.expanded.discard(c.id)
            elif c.id == "home" and c.state == "idle":
                self.expanded.add(c.id)
                asyncio.ensure_future(self.measure_home())
            elif c.items or c.id == "home":
                self.expanded.add(c.id)
        elif name in ("left", "h") and row:
            if row.item is None:
                self.expanded.discard(row.culprit.id)
            else:
                self.selected = next(n for n, r in enumerate(listed) if r.culprit is row.culprit)
        elif name == "c":
            self.copy("path")
        elif name == "x":
            self.copy("command")
        elif name == "r":
            if "home" in self.expanded and self.home and self.home.state == "ok":
                asyncio.ensure_future(self.measure_home())
            asyncio.ensure_future(self.rescan())
            return
        else:
            return
        self.selected = max(0, self.selected)
        await self.show()


screen = None
warned = set()  # the drives we've already said are nearly full


def is_open():
    return screen is not None and not screen.pane.closed


async def update_badge(drives):
    """Mark the tab while a drive is over `warn-percent`."""
    if not is_open():
        return
    warn = setting("warn-percent", 90)
    fullest = max((d.percent for d in drives), default=0)
    try:
        if fullest >= warn:
            await screen.pane.set_tab_style(badge=f"{fullest}%", color="red")
        else:
            await screen.pane.set_tab_style(badge=None, color=None)
    except LumenError:
        pass


async def check_drives():
    """Say so, once, when a drive passes `warn-percent`; again only after
    it has dropped back below."""
    drives = scan.drives()
    warn = setting("warn-percent", 90)
    for d in drives:
        if d.percent < warn:
            warned.discard(d.mount)
        elif d.mount not in warned:
            warned.add(d.mount)
            if setting("notify", True):
                await app.notify(
                    "Disk nearly full",
                    f"{scan.clean(d.mount)} is {d.percent}% full, with {scan.human(d.free_kb)} free",
                )
    if is_open():
        screen.drives = drives
        await update_badge(drives)
        await screen.show()


@app.on_start
async def watch():
    while True:
        try:
            await check_drives()
        except LumenError as e:
            app.log("couldn't check the drives:", repr(e))
        await asyncio.sleep(CHECK_EVERY)


@app.command("Disk Usage", id="open")
async def open_screen(pane):
    global screen
    if is_open():
        await screen.pane.focus()
        return
    tab = await app.new_tab(plugin_pane=True)
    s = screen = Screen(tab)
    await tab.set_title("Disk Usage")

    @tab.on_key
    async def key(name):
        if name == "q":
            await tab.close()
            return
        await s.key(name)

    tab.on_resize(s.show)
    await update_badge(s.drives)
    if not s.saved or time.time() - s.saved["at"] > STALE:
        asyncio.ensure_future(s.rescan())


@app.on("settings-changed")
async def settings_changed(event):
    await check_drives()


app.run()
