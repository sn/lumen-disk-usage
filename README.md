# Disk Usage for Lumen

See how full your drives are and what's using the space, with the path of
each thing so you can go and delete it. A plugin for the
[Lumen](https://github.com/lumenshell/lumen) terminal.

Choose **Disk Usage** in the palette. Your drives are at the top. Below them
are the usual culprits, biggest first:

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

Enter expands a culprit into its items, each with its size, how long since
it last changed, and its path. The row you're on is shown in full at the
bottom, with the command that cleans it up where there is one.

It only looks. Nothing is deleted and no command is run for you: copy the
path or the command and run it yourself.

| Key | Does |
|---|---|
| ↑ / ↓ (j / k), PgUp / PgDn, Home / End | Move |
| Enter | Expand or collapse |
| → / ← (l / h) | Expand; collapse, or go from an item to its culprit |
| c | Copy the path, quoted for your shell (a Docker image or Ollama model: its name) |
| x | Copy the cleanup command |
| r | Scan again |
| q | Close |

The last scan is kept, so the tab opens at once and scans again in the
background if that was more than an hour ago. When a drive passes
`warn-percent` full you get one notification, whether or not the tab is
open, and the tab is marked in red while it is.

## What it looks at

| Culprit | What's counted |
|---|---|
| Docker | Images, containers, volumes and build cache as `docker system df` reports them, then the largest images and volumes by name |
| Rust build output | `target` folders beside a `Cargo.toml`, under your project folders |
| Rust toolchains & cache | Cargo's registry and git caches, and each rustup toolchain |
| AI models | Ollama models, the Hugging Face hub cache and LM Studio models |
| node_modules | The outermost `node_modules` of each project |
| Package caches | npm, pip, uv, yarn, pnpm, bun, Go, Gradle, Maven, Playwright, Homebrew, Xcode DerivedData |
| Trash | Your trash folder |
| Largest in home | The 20 largest things in your home folder. Measured only when you expand it, because it reads all of home |

Project folders are searched 6 levels deep, leaving out hidden folders.
A size marked `≥` is at least that: some of the folder couldn't be read.
Sizes are in units of 1024, as `du` and `df -h` give them, so Docker's
figures come out a little lower than Docker's own (which are in units of
1000). If Docker is installed but not answering, its row says "not running".

## Install

Open **Plugins** in Lumen's palette, find it under Browse and press `i`, or:

```sh
lumen plugins install sn/disk-usage
```

It asks for `panes`: to open its tab, draw it and mark it when a drive is
nearly full.

Copying uses `wl-copy`, `xclip` or `xsel` on Linux and `pbcopy` on macOS.
Without one of them, select the path with the mouse.

## Settings

| Setting | Default | What it does |
|---|---|---|
| `project-folders` | `["~"]` | Folders to look in for Rust `target` and `node_modules` folders |
| `warn-percent` | 90 | How full a drive is before it's shown in red and you're told (50 to 99) |
| `notify` | true | Send a notification when a drive passes `warn-percent` |

Change them with `s` in **Plugins**, or in `config.toml`:

```toml
[plugin."sn/disk-usage"]
project-folders = ["~/src", "~/work"]
warn-percent = 85
```

## Development

Standard library only (Python 3.8+). Written and tried on Linux; the macOS
paths are there but untested.

```sh
python3 -m unittest discover -s tests
lumen plugins check .
```

## License

MIT
