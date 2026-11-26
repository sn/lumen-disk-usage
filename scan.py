"""What's using the disk: the drives, and the usual culprits with their
sizes and paths. Nothing here talks to Lumen, and nothing here deletes or
changes anything: it reads folders and asks `df`, `du` and `docker`
read-only questions.
"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field, fields

FORMAT = 1  # of the saved scan; a file with another number is ignored
SMALL_KB = 1024  # items under 1 MB aren't worth a row
DEPTH = 6  # how far below a project folder to look
HOME_ROWS = 20  # how many entries "Largest in home" keeps
DOCKER_IMAGES, DOCKER_VOLUMES = 15, 10  # how many of the largest to list


# Formatting.


def human(kb) -> str:
    """A size in KB as people write it: "512 KB", "9.2 GB", "124 GB"."""
    if kb is None:
        return "?"
    if kb < 1024:
        return f"{int(kb)} KB"
    value = float(kb)
    for unit in ("MB", "GB", "TB"):
        value /= 1024
        if value < 1023.5 or unit == "TB":
            return f"{value:.1f} {unit}" if round(value, 1) < 10 else f"{value:.0f} {unit}"
    return ""  # not reached


def ago(ts, now=None) -> str:
    """How long since `ts`: "now", "4 min", "3 hr", "3 days", "5 mon", "2 yr"."""
    if ts is None:
        return ""
    seconds = max(0, (time.time() if now is None else now) - ts)
    if seconds < 60:
        return "now"
    if seconds < 3600:
        return f"{int(seconds // 60)} min"
    if seconds < 86400:
        return f"{int(seconds // 3600)} hr"
    days = int(seconds // 86400)
    if days < 60:
        return "1 day" if days == 1 else f"{days} days"
    if days < 730:
        return f"{days // 30} mon"
    return f"{days // 365} yr"


def clean(text: str) -> str:
    """`text` safe to draw: control and invisible characters become "?".
    Names come from the disk and from Docker, so they're someone else's text."""
    return "".join("?" if unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp") else c for c in text)


def tilde(path: str, home: str) -> str:
    """`path` with the home folder written as ~."""
    home = home.rstrip("/")
    if home and (path == home or path.startswith(home + "/")):
        return "~" + path[len(home):]
    return path


_SIZE = re.compile(r"([\d.]+)\s*([kKMGT]?)(i?)[bB]")


def parse_size(text: str) -> int:
    """Docker's "45.79GB", "118.4MB", "0B" or "39.29GB (85%)", in KB."""
    m = _SIZE.search(text or "")
    if not m:
        return 0
    base = 1024 if m.group(3) else 1000
    power = " KMGT".index(m.group(2).upper() or " ")
    try:
        return int(float(m.group(1)) * base**power / 1024)
    except ValueError:
        return 0


# Drives.


@dataclass
class Drive:
    mount: str
    total_kb: int
    used_kb: int
    free_kb: int

    @property
    def percent(self) -> int:
        """How full it is, rounded up as `df` does."""
        space = self.used_kb + self.free_kb
        return math.ceil(100 * self.used_kb / space) if space else 0


def parse_df(output: str) -> list:
    """The real drives in `df -kP` output: no pseudo filesystems (tmpfs,
    overlay, efivarfs…), no snap or disk-image loops, each device once, and
    nothing under 1 GB (such as /boot/efi)."""
    drives, seen = [], set()
    for line in output.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 6:
            continue
        device, mount = parts[0], " ".join(parts[5:])
        try:
            total, used, free = int(parts[1]), int(parts[2]), int(parts[3])
        except ValueError:
            continue
        real = device.startswith("/dev/") or ":" in device or device.startswith("//")
        if not real or device.startswith("/dev/loop"):
            continue
        # macOS shows one disk as several volumes; / stands for them.
        if mount.startswith(("/System/Volumes/", "/private/var/vm", "/Library/Developer/")):
            continue
        if device in seen or total < 1024 * 1024:
            continue
        seen.add(device)
        drives.append(Drive(mount, total, used, free))
    return drives


def drives() -> list:
    try:
        out = subprocess.run(
            ["df", "-kP"], capture_output=True, text=True, timeout=10, env=_c_env()
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return parse_df(out)


def _c_env() -> dict:
    return {**os.environ, "LC_ALL": "C"}


# What a scan finds.


@dataclass
class Item:
    """One thing using space: a folder (with `path`) or something a tool
    owns (with `name`, like a Docker image). `command` is the words of the
    command that cleans it up, shown and copied but never run."""

    label: str
    path: str | None = None
    name: str | None = None
    kb: int | None = None
    exact: bool = True  # False: `kb` is a lower bound
    reclaim_kb: int | None = None
    changed: float | None = None
    command: list | None = None
    note: str = ""
    counted: bool = False  # another culprit counts it too

    @classmethod
    def from_json(cls, d: dict) -> "Item":
        return cls(**{f.name: d[f.name] for f in fields(cls) if f.name in d})


@dataclass
class Culprit:
    id: str
    title: str
    items: list = field(default_factory=list)
    unit: str = "items"  # what its items are: "folders", "models"…
    kb: int | None = None
    exact: bool = True
    reclaim_kb: int | None = None
    note: str = ""
    command: list | None = None
    state: str = "pending"  # "pending", "ok", "unavailable" or "idle"
    at: float | None = None  # when it was measured, if apart from the scan
    ordered: bool = False  # its items are in a meaningful order: don't sort

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, d: dict) -> "Culprit":
        c = cls(**{f.name: d[f.name] for f in fields(cls) if f.name in d and f.name != "items"})
        c.items = [Item.from_json(i) for i in d.get("items", [])]
        return c


# Finding project folders.


def walk_projects(roots, depth: int = DEPTH):
    """The Rust `target/` folders and outermost `node_modules` folders under
    `roots`, at most `depth` levels down. Skips hidden folders, doesn't look
    inside what it finds, doesn't follow links or cross onto another drive."""
    targets, modules, seen = [], [], set()
    for root in roots:
        root = os.path.abspath(os.path.expanduser(root))
        try:
            device = os.stat(root).st_dev
        except OSError:
            continue
        stack = [(root, 1)]
        while stack:
            folder, level = stack.pop()
            try:
                entries = list(os.scandir(folder))
            except OSError:
                continue
            names = {e.name for e in entries}
            for e in entries:
                try:
                    if not e.is_dir(follow_symlinks=False) or e.stat(follow_symlinks=False).st_dev != device:
                        continue
                except OSError:
                    continue
                if e.path in seen:
                    continue
                if e.name == "node_modules":
                    seen.add(e.path)
                    modules.append(e.path)
                elif e.name == "target" and (
                    "Cargo.toml" in names or os.path.exists(os.path.join(e.path, "CACHEDIR.TAG"))
                ):
                    seen.add(e.path)
                    targets.append(e.path)
                elif e.name.startswith(".") or (e.name == "Library" and level == 1):
                    continue
                elif level < depth:
                    seen.add(e.path)
                    stack.append((e.path, level + 1))
    return sorted(targets), sorted(modules)


# Sizes.


def du_kb(path: str, timeout: float = 120):
    """(size in KB, whether it's exact) from `du`. Not exact when `du`
    couldn't read everything; (None, False) when it gave nothing."""
    try:
        done = subprocess.run(
            ["du", "-skx", "--", path],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=timeout,
            env=_c_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return None, False
    first = done.stdout.split(None, 1)
    if not first or not first[0].isdigit():
        return None, False
    return int(first[0]), done.returncode == 0


def _changed(path: str):
    """When `path` or anything directly in it last changed."""
    try:
        latest = os.lstat(path).st_mtime
        if os.path.isdir(path) and not os.path.islink(path):
            with os.scandir(path) as entries:
                for e in entries:
                    try:
                        latest = max(latest, e.stat(follow_symlinks=False).st_mtime)
                    except OSError:
                        pass
        return latest
    except OSError:
        return None


def size_items(items, on_done=None, workers: int = 4, timeout: float = 120) -> None:
    """Fill in `kb`, `exact` and `changed` for the items that have a path
    and no size yet. `on_done(item)` is called as each finishes, from a
    worker thread."""

    def measure(item):
        item.kb, item.exact = du_kb(item.path, timeout)
        if item.changed is None:
            item.changed = _changed(item.path)
        if on_done:
            on_done(item)

    todo = [i for i in items if i.path and i.kb is None]
    if not todo:
        return
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(measure, todo))


def tally(c: Culprit) -> None:
    """Add up a culprit's items into its own size."""
    if c.id == "docker":
        return  # Docker gave its own totals; its items overlap
    sized = [i for i in c.items if i.kb is not None]
    c.kb = sum(i.kb for i in sized)
    c.exact = len(sized) == len(c.items) and all(i.exact for i in sized)


# The saved scan.


def save(path: str, scan: dict) -> None:
    data = {
        "format": FORMAT,
        "at": scan.get("at"),
        "culprits": [c.to_json() for c in scan.get("culprits", [])],
        "home": scan["home"].to_json() if scan.get("home") else None,
    }
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def load(path: str):
    """The saved scan, or None if there isn't one worth using."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or data.get("format") != FORMAT:
            return None
        return {
            "at": float(data["at"]),
            "culprits": [Culprit.from_json(c) for c in data["culprits"]],
            "home": Culprit.from_json(data["home"]) if data.get("home") else None,
        }
    except (OSError, ValueError, TypeError, KeyError):
        return None


# The culprits.


def _run(args, timeout):
    """(exit code, output). Raises FileNotFoundError if the program isn't
    installed."""
    try:
        done = subprocess.run(
            args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=timeout, env=_c_env()
        )
    except subprocess.TimeoutExpired:
        return 124, ""
    return done.returncode, done.stdout


_DOCKER_TYPES = {
    "Images": ["docker", "image", "prune", "-a"],
    "Containers": ["docker", "container", "prune"],
    "Local Volumes": ["docker", "volume", "prune"],
    "Build Cache": ["docker", "builder", "prune"],
}


def parse_docker_summary(text: str) -> list:
    """`docker system df --format json`: one row per type."""
    items = []
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict) or "Type" not in row:
            continue
        kind = str(row["Type"])
        note = f"{row.get('TotalCount', '?')} in all, {row.get('Active', '?')} in use"
        items.append(
            Item(
                label=kind,
                name=None,
                kb=parse_size(str(row.get("Size", ""))),
                reclaim_kb=parse_size(str(row.get("Reclaimable", ""))),
                command=_DOCKER_TYPES.get(kind),
                note=note,
            )
        )
    return items


def parse_docker_detail(text: str) -> list:
    """`docker system df -v --format json`: the largest images and volumes."""
    try:
        data = json.loads(text)
    except ValueError:
        return []
    if not isinstance(data, dict):
        return []
    images, volumes = [], []
    for row in data.get("Images") or []:
        repo, tag = str(row.get("Repository", "")), str(row.get("Tag", ""))
        short = str(row.get("ID", "")).replace("sha256:", "")[:12]
        name = short if "<none>" in (repo, tag) or not repo else f"{repo}:{tag}"
        if not name:
            continue
        images.append(
            Item(
                label=f"image {name}",
                name=name,
                kb=parse_size(str(row.get("Size", ""))),
                command=["docker", "image", "rm", name],
                note="unused" if str(row.get("Containers", "")) == "0" else "in use",
            )
        )
    for row in data.get("Volumes") or []:
        name = str(row.get("Name", ""))
        if not name:
            continue
        shown = name[:12] + "…" if re.fullmatch(r"[0-9a-f]{64}", name) else name
        volumes.append(
            Item(
                label=f"volume {shown}",
                name=name,
                kb=parse_size(str(row.get("Size", ""))),
                command=["docker", "volume", "rm", name],
                note="unused" if str(row.get("Links", "")) == "0" else "in use",
            )
        )
    images.sort(key=lambda i: -i.kb)
    volumes.sort(key=lambda i: -i.kb)
    return images[:DOCKER_IMAGES] + volumes[:DOCKER_VOLUMES]


def docker(run=_run):
    """Docker's images, containers, volumes and build cache, as Docker
    reports them. None when Docker isn't installed."""
    c = Culprit("docker", "Docker", command=["docker", "system", "prune"], ordered=True)
    try:
        code, out = run(["docker", "system", "df", "--format", "json"], 30)
    except OSError:
        return None
    types = parse_docker_summary(out) if code == 0 else []
    if not types:
        c.state, c.note = "unavailable", "not running"
        return c
    try:
        code, out = run(["docker", "system", "df", "-v", "--format", "json"], 90)
    except OSError:
        code, out = 1, ""
    c.items = types + (parse_docker_detail(out) if code == 0 else [])
    c.kb = sum(i.kb or 0 for i in types)
    c.reclaim_kb = sum(i.reclaim_kb or 0 for i in types)
    return c


def rust_builds(targets) -> Culprit:
    c = Culprit("rust-builds", "Rust build output", unit="folders")
    for path in targets:
        manifest = os.path.join(os.path.dirname(path), "Cargo.toml")
        command = ["cargo", "clean", "--manifest-path", manifest] if os.path.exists(manifest) else None
        c.items.append(Item(label=os.path.basename(os.path.dirname(path)), path=path, command=command))
    return c


def _subdirs(path: str) -> list:
    try:
        with os.scandir(path) as entries:
            return sorted(e.path for e in entries if e.is_dir(follow_symlinks=False))
    except OSError:
        return []


def rust_tools(home: str, env) -> Culprit:
    c = Culprit("rust-tools", "Rust toolchains & cache", unit="folders")
    cargo = env.get("CARGO_HOME") or os.path.join(home, ".cargo")
    rustup = env.get("RUSTUP_HOME") or os.path.join(home, ".rustup")
    for name in ("registry", "git"):
        path = os.path.join(cargo, name)
        if os.path.isdir(path):
            c.items.append(Item(label=f"cargo {name}", path=path))
    for path in _subdirs(os.path.join(rustup, "toolchains")):
        name = os.path.basename(path)
        c.items.append(Item(label=name, path=path, command=["rustup", "toolchain", "uninstall", name]))
    return c


def _ollama(models: str) -> list:
    """The models in one Ollama models folder, sized from their manifests."""
    items = []
    manifests = os.path.join(models, "manifests")
    for folder, _, files in os.walk(manifests):
        for tag in files:
            path = os.path.join(folder, tag)
            try:
                with open(path, encoding="utf-8") as f:
                    manifest = json.load(f)
                layers = list(manifest.get("layers") or []) + [manifest.get("config") or {}]
                size = sum(int(layer.get("size") or 0) for layer in layers)
            except (OSError, ValueError, TypeError, AttributeError):
                continue
            parts = os.path.relpath(folder, manifests).split(os.sep)
            if parts[0] == "registry.ollama.ai":
                parts = parts[2:] if parts[1:2] == ["library"] else parts[1:]
            name = "/".join(parts) + ":" + tag
            items.append(
                Item(
                    label=f"ollama {name}",
                    name=name,
                    kb=size // 1024,
                    changed=_changed(path),
                    command=["ollama", "rm", name],
                )
            )
    blobs = os.path.join(models, "blobs")
    if not items and os.path.isdir(blobs):
        items.append(Item(label="ollama blobs", path=blobs))
    return items


def ai_models(home: str, env, system_dirs=("/usr/share/ollama/.ollama/models",)) -> Culprit:
    c = Culprit("ai-models", "AI models", unit="models")
    seen = set()
    for models in [env.get("OLLAMA_MODELS"), os.path.join(home, ".ollama", "models"), *system_dirs]:
        if models and os.path.isdir(models) and os.path.realpath(models) not in seen:
            seen.add(os.path.realpath(models))
            c.items += _ollama(models)
    hf = env.get("HF_HOME") or os.path.join(env.get("XDG_CACHE_HOME") or os.path.join(home, ".cache"), "huggingface")
    hub = env.get("HF_HUB_CACHE") or os.path.join(hf, "hub")
    for path in _subdirs(hub):
        kind, _, rest = os.path.basename(path).partition("--")
        if kind in ("models", "datasets", "spaces") and rest:
            c.items.append(Item(label=rest.replace("--", "/"), path=path))
        elif not kind.startswith("."):
            # Newer hubs keep the files of every model together, in blobs/.
            c.items.append(Item(label=f"huggingface {kind}", path=path))
    for studio in (os.path.join(home, ".lmstudio", "models"), os.path.join(home, ".cache", "lm-studio", "models")):
        for publisher in _subdirs(studio):
            for path in _subdirs(publisher):
                c.items.append(Item(label=os.path.basename(path), path=path))
    return c


def node_modules(found) -> Culprit:
    c = Culprit("node-modules", "node_modules", unit="folders")
    c.items = [Item(label=os.path.basename(os.path.dirname(p)), path=p) for p in found]
    return c


def package_caches(home: str, env) -> Culprit:
    c = Culprit("package-caches", "Package caches", unit="caches")
    cache = env.get("XDG_CACHE_HOME") or os.path.join(home, ".cache")
    mac = os.path.join(home, "Library", "Caches")
    go = env.get("GOPATH") or os.path.join(home, "go")
    known = [
        ("npm", [os.path.join(home, ".npm")], ["npm", "cache", "clean", "--force"]),
        ("pip", [os.path.join(cache, "pip"), os.path.join(mac, "pip")], ["pip", "cache", "purge"]),
        ("uv", [os.path.join(cache, "uv"), os.path.join(mac, "uv")], ["uv", "cache", "clean"]),
        ("yarn", [os.path.join(cache, "yarn"), os.path.join(mac, "Yarn")], ["yarn", "cache", "clean"]),
        (
            "pnpm store",
            [os.path.join(home, ".local", "share", "pnpm", "store"), os.path.join(home, "Library", "pnpm", "store")],
            ["pnpm", "store", "prune"],
        ),
        ("bun", [os.path.join(home, ".bun", "install", "cache")], ["bun", "pm", "cache", "rm"]),
        ("Go build cache", [os.path.join(cache, "go-build"), os.path.join(mac, "go-build")], ["go", "clean", "-cache"]),
        ("Go modules", [os.path.join(go, "pkg", "mod")], ["go", "clean", "-modcache"]),
        ("Gradle", [os.path.join(home, ".gradle", "caches")], None),
        ("Maven", [os.path.join(home, ".m2", "repository")], None),
        ("Playwright browsers", [os.path.join(cache, "ms-playwright"), os.path.join(mac, "ms-playwright")], None),
        ("Homebrew", [os.path.join(mac, "Homebrew")], ["brew", "cleanup"]),
        ("Xcode DerivedData", [os.path.join(home, "Library", "Developer", "Xcode", "DerivedData")], None),
    ]
    for label, paths, command in known:
        for path in paths:
            if os.path.isdir(path):
                c.items.append(Item(label=label, path=path, command=command))
    return c


def trash(home: str) -> Culprit:
    c = Culprit("trash", "Trash", unit="folders")
    for path in (os.path.join(home, ".local", "share", "Trash"), os.path.join(home, ".Trash")):
        if os.path.isdir(path):
            c.items.append(Item(label="Trash", path=path))
    return c


# Folders that only hold other things' folders: list what's in them instead.
_CONTAINERS = (".cache", ".local", ".local/share", ".var", ".var/app", "Library")


def largest_in_home(home: str, counted_paths=(), on_done=None, keep: int = HOME_ROWS) -> Culprit:
    """The largest entries directly in the home folder, with the folders in
    _CONTAINERS replaced by their own children. Measures all of home, so
    it's slow; an entry another culprit counts is marked `counted`."""
    c = Culprit("home", "Largest in home", unit="entries", state="ok", at=time.time())
    expand = {os.path.join(home, *name.split("/")) for name in _CONTAINERS}
    items, stack = [], [home]
    while stack:
        try:
            entries = list(os.scandir(stack.pop()))
        except OSError:
            continue
        for e in entries:
            try:
                st = e.stat(follow_symlinks=False)
                if e.is_dir(follow_symlinks=False):
                    if e.path in expand:
                        stack.append(e.path)
                    else:
                        items.append(Item(label=e.name, path=e.path))
                elif e.is_file(follow_symlinks=False):
                    items.append(
                        Item(label=e.name, path=e.path, kb=st.st_blocks * 512 // 1024, changed=st.st_mtime)
                    )
            except OSError:
                continue
    size_items(items, on_done, timeout=600)
    counted = [p.rstrip("/") for p in counted_paths if p]
    for item in items:
        item.counted = any(item.path == p or item.path.startswith(p + "/") for p in counted)
    items.sort(key=lambda i: -(i.kb or 0))
    c.items = [i for i in items[:keep] if (i.kb or 0) >= SMALL_KB]
    c.exact = all(i.exact for i in c.items)
    return c


# The whole scan.


def scan(settings: dict, home: str, env, on_progress=None, run=_run) -> dict:
    """Measure every culprit but "Largest in home". `on_progress(culprits)`
    is called, from this and from worker threads, as sizes arrive."""
    roots = settings.get("project-folders") or ["~"]
    roots = [home + r[1:] if r == "~" or r.startswith("~/") else r for r in roots if isinstance(r, str) and r.strip()]
    targets, modules = walk_projects(roots)
    found = [
        docker(run),
        rust_builds(targets),
        rust_tools(home, env),
        ai_models(home, env),
        node_modules(modules),
        package_caches(home, env),
        trash(home),
    ]
    culprits = [c for c in found if c is not None and (c.items or c.state == "unavailable")]
    owner = {id(i): c for c in culprits for i in c.items}
    for c in culprits:
        tally(c)

    def progress(item=None):
        if item is not None:
            tally(owner[id(item)])
        if on_progress:
            on_progress(culprits)

    progress()
    size_items([i for c in culprits for i in c.items], progress)
    for c in culprits:
        if c.state == "unavailable":
            continue
        c.items = [i for i in c.items if i.kb is None or i.kb >= SMALL_KB or (i.reclaim_kb or 0) >= SMALL_KB]
        if not c.ordered:
            c.items.sort(key=lambda i: -(i.kb or 0))
        tally(c)
        c.state = "ok"
    culprits = [c for c in culprits if c.items or c.state == "unavailable"]
    culprits.sort(key=lambda c: -(c.kb or 0))
    return {"at": time.time(), "culprits": culprits, "home": None}
