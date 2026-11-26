# Disk Usage for Lumen

Your disk is full again. This shows you what to delete.

A plugin for the [Lumen](https://github.com/lumenshell/lumen) terminal. One
tab tells you how full each drive is and where the space went: Docker, Rust
builds, AI models, `node_modules`, package caches. Every item comes with its
path, so you can go and remove it.

```
 Disk Usage                                              scanned 4 min ago
 /  █████████████████████░░░░░░░░░  616 GB of 915 GB · 253 GB free   71%

 ▸ Docker                       116 GB   71 GB reclaimable
 ▸ Package caches                47 GB   9 caches
 ▸ node_modules                  11 GB   39 folders
 ▾ Rust build output            3.2 GB   4 folders
       1.5 GB    23 hr  ~/src/lumen/target
       889 MB  37 days  ~/src/editor/target
 ▸ Largest in home                       enter to measure (this one takes a while)

 /home/you/src/lumen/target
 cargo clean --manifest-path /home/you/src/lumen/Cargo.toml
 ↑↓ move · enter expand · c copy path · x copy command · r rescan · q close
```

It only looks. It never deletes anything and never runs a command for you.
You copy the path or the cleanup command and decide what goes.

## Install

Open **Plugins** in Lumen's palette, find it under Browse and press `i`, or:

```sh
lumen plugins install sn/disk-usage
```

It asks for one permission, `panes`: to open its tab, draw it and mark it
when a drive is nearly full.

## Using it

Choose **Disk Usage** in the palette.

Your drives are at the top. Below them are the usual culprits, biggest
first. Press Enter on one to see what's inside: each item with its size, how
long since it last changed, and where it is. A `target` folder nobody has
touched in five months is easy to spot.

The row you're on is written out in full at the bottom of the tab: the whole
path, and the command that cleans it up if there is one. Press `c` to copy
the path, already quoted for your shell. Press `x` to copy the command.

| Key | Does |
|---|---|
| ↑ / ↓ (j / k), PgUp / PgDn, Home / End | Move |
| Enter | Expand or collapse |
| → / ← (l / h) | Expand; collapse, or go from an item to its culprit |
| c | Copy the path (for a Docker image or Ollama model, its name) |
| x | Copy the cleanup command |
| r | Scan again |
| q | Close |

The tab opens at once, showing the last scan and how old it is. If that scan
is more than an hour old, a new one runs in the background while you read.
On a 916 GB drive with 39 `node_modules` folders, a scan took 4 to 14
seconds.

## Before the disk fills up

When a drive passes 90% full you get one notification, whether or not the
tab is open. You aren't told again until the drive has dropped back below.
While the tab is open, it's marked in red with the percentage.

## What it looks at

| Culprit | What's counted |
|---|---|
| Docker | Images, containers, volumes and build cache as `docker system df` reports them, then the 15 largest images and 10 largest volumes by name |
| Rust build output | `target` folders that belong to a Cargo project, under your project folders |
| Rust toolchains & cache | Cargo's registry and git caches, and each rustup toolchain |
| AI models | Ollama models, the Hugging Face hub cache and LM Studio models |
| node_modules | The outermost `node_modules` of each project |
| Package caches | npm, pip, uv, yarn, pnpm, bun, Go, Gradle, Maven, Playwright, Homebrew, Xcode DerivedData |
| Trash | Your trash folder |
| Largest in home | The 20 largest things in your home folder |

Docker matters most here. Its data sits in a folder only root can read, so a
plain folder scanner never shows it to you. This asks Docker itself.

**Largest in home** catches whatever isn't on the list. It reads all of
home, which is slow, so it's measured only when you expand it.

A few things to know when reading the numbers:

- Project folders are searched 6 levels deep. Hidden folders are skipped.
- `≥` in front of a size means at least that much. Part of the folder
  couldn't be read.
- Sizes are in units of 1024, like `du` and `df -h`. Docker counts in units
  of 1000, so its own figures run a little higher than the ones here.
- If Docker is installed but not answering, its row says "not running"
  instead of showing zero.
- Anything under 1 MB is left out.

## Settings

| Setting | Default | What it does |
|---|---|---|
| `project-folders` | `["~"]` | Where to look for Rust `target` and `node_modules` folders |
| `warn-percent` | 90 | How full a drive is before it turns red and you're told (50 to 99) |
| `notify` | true | Send the notification when a drive passes `warn-percent` |

Change them with `s` in **Plugins**, or in `config.toml`:

```toml
[plugin."sn/disk-usage"]
project-folders = ["~/src", "~/work"]
warn-percent = 85
```

## Copying

Copying uses `wl-copy`, `xclip` or `xsel` on Linux and `pbcopy` on macOS. If
you have none of them, the path is still on screen: select it with the
mouse.

A path with control or invisible characters in it is never copied. Such a
name can make a command look different from what it does.

## Development

Python 3.8 or newer, standard library only. Written and tried on Linux. The
macOS paths are there but untested.

```sh
python3 -m unittest discover -s tests
lumen plugins check .
```

## License

MIT
