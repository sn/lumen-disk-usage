"""Tests for finding and measuring what uses the disk, without a running
Lumen.

    python3 -m unittest discover -s tests
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scan  # noqa: E402

DF = """Filesystem     1024-blocks      Used Available Capacity Mounted on
tmpfs              6541244      3396   6537848       1% /run
efivarfs               128        28        96      23% /sys/firmware/efi/efivars
/dev/nvme1n1p3   960302804 645922816 265525320      71% /
tmpfs             32706204    181100  32525104       1% /dev/shm
/dev/nvme1n1p2      523248     39888    483360       8% /boot/efi
/dev/loop3          100000    100000         0     100% /snap/core/1
overlay          960302804 645922816 265525320      71% /var/lib/docker/overlay2/x/merged
/dev/sda1       1921802520 100000000 1724000000       6% /media/sean/My Passport
/dev/nvme1n1p3   960302804 645922816 265525320      71% /home
"""

DOCKER_SUMMARY = "\n".join(
    [
        '{"Active":"11","Reclaimable":"39.29GB (85%)","Size":"45.79GB","TotalCount":"264","Type":"Images"}',
        '{"Active":"1","Reclaimable":"105.9MB (89%)","Size":"118.4MB","TotalCount":"14","Type":"Containers"}',
        '{"Active":"9","Reclaimable":"18.29GB (64%)","Size":"28.29GB","TotalCount":"47","Type":"Local Volumes"}',
        '{"Active":"0","Reclaimable":"18.39GB","Size":"50.3GB","TotalCount":"1130","Type":"Build Cache"}',
    ]
)

DOCKER_DETAIL = json.dumps(
    {
        "Images": [
            {"Repository": "postgres", "Tag": "16", "ID": "sha256:aaaabbbbccccdddd", "Size": "432MB", "Containers": "1"},
            {"Repository": "<none>", "Tag": "<none>", "ID": "sha256:1234567890abcdef", "Size": "2.1GB", "Containers": "0"},
            {"Repository": "node", "Tag": "22", "ID": "sha256:ffff", "Size": "1.1GB", "Containers": "0"},
        ],
        "Volumes": [
            {"Name": "a" * 64, "Size": "210.4MB", "Links": "0"},
            {"Name": "pgdata", "Size": "3GB", "Links": "1"},
        ],
        "Containers": [],
        "BuildCache": [],
    }
)


def make(root, *paths):
    """Create files (a path ending in / is a folder) under `root`."""
    for p in paths:
        full = os.path.join(root, p)
        if p.endswith("/"):
            os.makedirs(full, exist_ok=True)
        else:
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w") as f:
                f.write("x" * 4096)


class FormatTests(unittest.TestCase):
    def test_human(self):
        self.assertEqual(scan.human(0), "0 KB")
        self.assertEqual(scan.human(512), "512 KB")
        self.assertEqual(scan.human(int(9.2 * 1024 * 1024)), "9.2 GB")
        self.assertEqual(scan.human(124 * 1024 * 1024), "124 GB")
        self.assertEqual(scan.human(int(9.97 * 1024)), "10 MB")
        self.assertEqual(scan.human(3 * 1024**3), "3.0 TB")
        self.assertEqual(scan.human(None), "?")

    def test_ago(self):
        now = 1_000_000_000
        self.assertEqual(scan.ago(None, now), "")
        self.assertEqual(scan.ago(now - 5, now), "now")
        self.assertEqual(scan.ago(now - 4 * 60, now), "4 min")
        self.assertEqual(scan.ago(now - 3 * 3600, now), "3 hr")
        self.assertEqual(scan.ago(now - 86400, now), "1 day")
        self.assertEqual(scan.ago(now - 3 * 86400, now), "3 days")
        self.assertEqual(scan.ago(now - 150 * 86400, now), "5 mon")
        self.assertEqual(scan.ago(now - 800 * 86400, now), "2 yr")
        self.assertEqual(scan.ago(now + 50, now), "now")

    def test_clean_removes_what_could_fool_the_eye(self):
        self.assertEqual(scan.clean("a\nb\x1b[31mc"), "a?b?[31mc")
        self.assertEqual(scan.clean("evil‮txt.exe"), "evil?txt.exe")
        self.assertEqual(scan.clean("zero​width"), "zero?width")
        self.assertEqual(scan.clean("naïve 日本 folder"), "naïve 日本 folder")

    def test_tilde(self):
        self.assertEqual(scan.tilde("/home/me/src/x", "/home/me"), "~/src/x")
        self.assertEqual(scan.tilde("/home/me", "/home/me"), "~")
        self.assertEqual(scan.tilde("/home/meow/x", "/home/me"), "/home/meow/x")

    def test_parse_size(self):
        self.assertEqual(scan.parse_size("0B"), 0)
        self.assertEqual(scan.parse_size("118.4MB"), int(118.4e6 / 1024))
        self.assertEqual(scan.parse_size("39.29GB (85%)"), int(39.29e9 / 1024))
        self.assertEqual(scan.parse_size("1.5kB"), 1)
        self.assertEqual(scan.parse_size("2GiB"), 2 * 1024 * 1024)
        self.assertEqual(scan.parse_size("nonsense"), 0)
        self.assertEqual(scan.parse_size(""), 0)


class DriveTests(unittest.TestCase):
    def test_only_real_drives_each_once(self):
        drives = scan.parse_df(DF)
        self.assertEqual([d.mount for d in drives], ["/", "/media/sean/My Passport"])
        root = drives[0]
        self.assertEqual((root.total_kb, root.used_kb, root.free_kb), (960302804, 645922816, 265525320))
        self.assertEqual(root.percent, 71)

    def test_nonsense_gives_nothing(self):
        self.assertEqual(scan.parse_df(""), [])
        self.assertEqual(scan.parse_df("Filesystem\n/dev/sda1 x y z 1% /\ngarbage"), [])

    def test_percent_of_an_empty_drive(self):
        self.assertEqual(scan.Drive("/", 0, 0, 0).percent, 0)


class WalkTests(unittest.TestCase):
    def test_finds_rust_targets_and_outermost_node_modules(self):
        with tempfile.TemporaryDirectory() as root:
            root = os.path.realpath(root)
            make(
                root,
                "api/Cargo.toml",
                "api/target/debug/x",
                "tagged/target/CACHEDIR.TAG",
                "maven/target/classes/A.class",  # a target/ that isn't Rust's
                "web/node_modules/a/node_modules/b/index.js",
                "web/package.json",
                ".hidden/Cargo.toml",
                ".hidden/target/x",
                "api/target/debug/build/dep/Cargo.toml",
                "api/target/debug/build/dep/target/x",  # inside a found one
            )
            targets, modules = scan.walk_projects([root])
            self.assertEqual(targets, [f"{root}/api/target", f"{root}/tagged/target"])
            self.assertEqual(modules, [f"{root}/web/node_modules"])

    def test_depth_limit(self):
        with tempfile.TemporaryDirectory() as root:
            root = os.path.realpath(root)
            make(root, "a/b/c/d/e/Cargo.toml", "a/b/c/d/e/target/x", "a/b/c/d/e/f/Cargo.toml", "a/b/c/d/e/f/target/x")
            targets, _ = scan.walk_projects([root])
            self.assertEqual(targets, [f"{root}/a/b/c/d/e/target"])  # level 6, not 7

    def test_links_missing_folders_and_repeats(self):
        with tempfile.TemporaryDirectory() as root:
            root = os.path.realpath(root)
            make(root, "p/Cargo.toml", "p/target/x")
            os.symlink(root, os.path.join(root, "p", "loop"))
            targets, modules = scan.walk_projects([root, os.path.join(root, "nope"), root, os.path.join(root, "p")])
            self.assertEqual(targets, [f"{root}/p/target"])
            self.assertEqual(modules, [])


class SizeTests(unittest.TestCase):
    def test_du(self):
        with tempfile.TemporaryDirectory() as root:
            make(root, "a/one", "a/two")
            kb, exact = scan.du_kb(os.path.join(root, "a"))
            self.assertTrue(exact)
            self.assertGreaterEqual(kb, 8)
            self.assertEqual(scan.du_kb(os.path.join(root, "missing")), (None, False))

    @unittest.skipIf(os.geteuid() == 0, "root can read everything")
    def test_unreadable_folder_gives_a_lower_bound(self):
        with tempfile.TemporaryDirectory() as root:
            make(root, "a/one", "a/locked/secret")
            locked = os.path.join(root, "a", "locked")
            os.chmod(locked, 0)
            try:
                kb, exact = scan.du_kb(os.path.join(root, "a"))
            finally:
                os.chmod(locked, 0o700)
            self.assertFalse(exact)
            self.assertGreaterEqual(kb, 4)

    def test_size_items_fills_in_and_reports(self):
        with tempfile.TemporaryDirectory() as root:
            make(root, "a/one", "b/two")
            items = [
                scan.Item("a", path=os.path.join(root, "a")),
                scan.Item("named", name="img", kb=5),
                scan.Item("gone", path=os.path.join(root, "gone")),
            ]
            done = []
            scan.size_items(items, done.append)
            self.assertGreaterEqual(items[0].kb, 4)
            self.assertIsNotNone(items[0].changed)
            self.assertEqual(items[1].kb, 5)
            self.assertEqual((items[2].kb, items[2].exact), (None, False))
            self.assertEqual({i.label for i in done}, {"a", "gone"})

    def test_tally(self):
        c = scan.Culprit("x", "X", items=[scan.Item("a", kb=10), scan.Item("b", kb=5, exact=False)])
        scan.tally(c)
        self.assertEqual((c.kb, c.exact), (15, False))
        c.items.append(scan.Item("c", path="/x"))  # not sized yet
        scan.tally(c)
        self.assertEqual((c.kb, c.exact), (15, False))


class SavedTests(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "scan.json")
            c = scan.Culprit("rust-builds", "Rust build output", unit="folders", kb=7, state="ok")
            c.items = [scan.Item("p", path="/p/target", kb=7, changed=1.5, command=["cargo", "clean"])]
            home = scan.Culprit("home", "Largest in home", state="ok", at=12.0)
            scan.save(path, {"at": 99.0, "culprits": [c], "home": home})
            got = scan.load(path)
            self.assertEqual(got["at"], 99.0)
            self.assertEqual(got["culprits"], [c])
            self.assertEqual(got["home"], home)
            self.assertFalse(os.path.exists(path + ".tmp"))

    def test_bad_files_are_ignored(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "scan.json")
            self.assertIsNone(scan.load(path))
            for text in ["", "{not json", "[]", '{"format": 0, "at": 1, "culprits": []}',
                         '{"format": 1}', '{"format": 1, "at": "x", "culprits": []}',
                         '{"format": 1, "at": 1, "culprits": [{"title": "no id"}]}',
                         '{"format": 1, "at": 1, "culprits": [3]}']:
                with open(path, "w") as f:
                    f.write(text)
                self.assertIsNone(scan.load(path), text)


class DockerTests(unittest.TestCase):
    def test_summary(self):
        items = scan.parse_docker_summary(DOCKER_SUMMARY + "\nnot json\n")
        self.assertEqual([i.label for i in items], ["Images", "Containers", "Local Volumes", "Build Cache"])
        self.assertEqual(items[0].kb, int(45.79e9 / 1024))
        self.assertEqual(items[0].reclaim_kb, int(39.29e9 / 1024))
        self.assertEqual(items[0].command, ["docker", "image", "prune", "-a"])
        self.assertEqual(items[0].note, "264 in all, 11 in use")
        self.assertEqual(items[3].command, ["docker", "builder", "prune"])

    def test_detail(self):
        items = scan.parse_docker_detail(DOCKER_DETAIL)
        self.assertEqual([i.name for i in items], ["1234567890ab", "node:22", "postgres:16", "pgdata", "a" * 64])
        self.assertEqual(items[0].command, ["docker", "image", "rm", "1234567890ab"])
        self.assertEqual(items[0].note, "unused")
        self.assertEqual(items[2].note, "in use")
        self.assertEqual(items[4].label, "volume " + "a" * 12 + "…")
        self.assertEqual(items[4].command, ["docker", "volume", "rm", "a" * 64])
        self.assertEqual(scan.parse_docker_detail("nope"), [])
        self.assertEqual(scan.parse_docker_detail("[]"), [])

    def test_culprit(self):
        def run(args, timeout):
            return 0, DOCKER_DETAIL if "-v" in args else DOCKER_SUMMARY

        c = scan.docker(run)
        self.assertEqual(c.kb, sum(scan.parse_size(s) for s in ("45.79GB", "118.4MB", "28.29GB", "50.3GB")))
        self.assertEqual(c.reclaim_kb, sum(scan.parse_size(s) for s in ("39.29GB", "105.9MB", "18.29GB", "18.39GB")))
        self.assertEqual(len(c.items), 9)
        self.assertEqual(c.command, ["docker", "system", "prune"])

    def test_not_installed(self):
        def run(args, timeout):
            raise FileNotFoundError("docker")

        self.assertIsNone(scan.docker(run))

    def test_not_running_says_so(self):
        c = scan.docker(lambda args, timeout: (1, ""))
        self.assertEqual((c.state, c.note, c.kb, c.items), ("unavailable", "not running", None, []))
        c = scan.docker(lambda args, timeout: (124, ""))  # timed out
        self.assertEqual(c.state, "unavailable")

    def test_summary_without_detail(self):
        c = scan.docker(lambda args, timeout: (1, "") if "-v" in args else (0, DOCKER_SUMMARY))
        self.assertEqual(len(c.items), 4)


class CulpritTests(unittest.TestCase):
    def test_rust_builds(self):
        with tempfile.TemporaryDirectory() as root:
            make(root, "api/Cargo.toml", "api/target/x", "tagged/target/CACHEDIR.TAG")
            c = scan.rust_builds([f"{root}/api/target", f"{root}/tagged/target"])
            self.assertEqual(c.items[0].command, ["cargo", "clean", "--manifest-path", f"{root}/api/Cargo.toml"])
            self.assertIsNone(c.items[1].command)
            self.assertEqual(c.items[0].path, f"{root}/api/target")

    def test_rust_tools(self):
        with tempfile.TemporaryDirectory() as home:
            make(home, ".cargo/registry/x", ".cargo/bin/cargo", ".rustup/toolchains/stable-x86_64/lib/x",
                 "elsewhere/toolchains/nightly/x")
            c = scan.rust_tools(home, {})
            self.assertEqual([i.path for i in c.items],
                             [f"{home}/.cargo/registry", f"{home}/.rustup/toolchains/stable-x86_64"])
            self.assertEqual(c.items[1].command, ["rustup", "toolchain", "uninstall", "stable-x86_64"])
            c = scan.rust_tools(home, {"RUSTUP_HOME": f"{home}/elsewhere", "CARGO_HOME": f"{home}/none"})
            self.assertEqual([i.label for i in c.items], ["nightly"])

    def test_ollama_models_from_manifests(self):
        with tempfile.TemporaryDirectory() as home:
            base = f"{home}/.ollama/models/manifests/registry.ollama.ai"
            for path, sizes in [(f"{base}/library/llama3/8b", [4_000_000_000, 1000]),
                                (f"{base}/someone/tuned/latest", [2048]),
                                (f"{home}/.ollama/models/manifests/hf.co/org/model/Q4", [1024])]:
                os.makedirs(os.path.dirname(path))
                with open(path, "w") as f:
                    json.dump({"config": {"size": 24}, "layers": [{"size": s} for s in sizes]}, f)
            with open(f"{base}/library/llama3/broken", "w") as f:
                f.write("{")
            make(home, ".ollama/models/blobs/sha256-x")
            c = scan.ai_models(home, {}, system_dirs=())
            by_name = {i.name: i for i in c.items}
            self.assertEqual(set(by_name), {"llama3:8b", "someone/tuned:latest", "hf.co/org/model:Q4"})
            self.assertEqual(by_name["llama3:8b"].kb, (4_000_000_000 + 1000 + 24) // 1024)
            self.assertEqual(by_name["llama3:8b"].command, ["ollama", "rm", "llama3:8b"])
            self.assertIsNone(by_name["llama3:8b"].path)

    def test_ollama_blobs_alone_and_hugging_face(self):
        with tempfile.TemporaryDirectory() as home:
            make(home, ".ollama/models/blobs/sha256-x", ".cache/huggingface/hub/models--org--name/blobs/x",
                 ".cache/huggingface/hub/datasets--org--data/x", ".cache/huggingface/hub/version.txt",
                 ".cache/huggingface/hub/.locks/x", ".cache/huggingface/hub/blobs/ab/x", "hf/hub/models--other--one/x",
                 ".lmstudio/models/pub/model-gguf/m.gguf")
            c = scan.ai_models(home, {}, system_dirs=())
            self.assertEqual(
                [(i.label, i.path) for i in c.items],
                [
                    ("ollama blobs", f"{home}/.ollama/models/blobs"),
                    ("huggingface blobs", f"{home}/.cache/huggingface/hub/blobs"),
                    ("org/data", f"{home}/.cache/huggingface/hub/datasets--org--data"),
                    ("org/name", f"{home}/.cache/huggingface/hub/models--org--name"),
                    ("model-gguf", f"{home}/.lmstudio/models/pub/model-gguf"),
                ],
            )
            c = scan.ai_models(home, {"HF_HOME": f"{home}/hf"}, system_dirs=())
            self.assertIn(f"{home}/hf/hub/models--other--one", [i.path for i in c.items])

    def test_package_caches_and_trash(self):
        with tempfile.TemporaryDirectory() as home:
            make(home, ".npm/x", ".cache/pip/x", "go/pkg/mod/x", "Library/Caches/Homebrew/x", ".local/share/Trash/files/x")
            c = scan.package_caches(home, {})
            self.assertEqual({i.label: i.command for i in c.items}, {
                "npm": ["npm", "cache", "clean", "--force"],
                "pip": ["pip", "cache", "purge"],
                "Go modules": ["go", "clean", "-modcache"],
                "Homebrew": ["brew", "cleanup"],
            })
            self.assertEqual([i.path for i in scan.trash(home).items], [f"{home}/.local/share/Trash"])
            self.assertEqual(scan.package_caches(home, {"XDG_CACHE_HOME": f"{home}/nowhere"}).items[1].label, "Go modules")

    def test_largest_in_home(self):
        with tempfile.TemporaryDirectory() as home:
            make(home, ".cache/big/x", ".cache/small/x", ".local/share/app/x", ".local/bin/tool", "src/x", "notes.txt")
            for name, kb in [(".cache/big/blob", 4096), (".local/share/app/blob", 3072), ("src/blob", 2048),
                             ("file.bin", 1500), (".cache/small/blob", 1100)]:
                with open(os.path.join(home, name), "wb") as f:
                    f.write(os.urandom(kb * 1024))
            c = scan.largest_in_home(home, counted_paths=[f"{home}/.cache/big"], keep=4)
            self.assertEqual([os.path.relpath(i.path, home) for i in c.items],
                             [".cache/big", ".local/share/app", "src", "file.bin"])
            self.assertEqual([i.counted for i in c.items], [True, False, False, False])
            self.assertEqual(c.state, "ok")
            self.assertIsNotNone(c.at)


class ScanTests(unittest.TestCase):
    def test_whole_scan(self):
        with tempfile.TemporaryDirectory() as home:
            home = os.path.realpath(home)
            make(home, "code/api/Cargo.toml", "code/web/node_modules/tiny", ".npm/x")
            for name, kb in [("code/api/target/blob", 3000), (".npm/blob", 1500)]:
                os.makedirs(os.path.dirname(os.path.join(home, name)), exist_ok=True)
                with open(os.path.join(home, name), "wb") as f:
                    f.write(os.urandom(kb * 1024))
            seen = []
            got = scan.scan({"project-folders": ["~/code", "", 7]}, home, {}, seen.append,
                            run=lambda args, timeout: (1, ""))
            self.assertEqual([c.id for c in got["culprits"]], ["rust-builds", "package-caches", "docker"])
            rust = got["culprits"][0]
            self.assertEqual((rust.state, rust.exact), ("ok", True))
            self.assertGreaterEqual(rust.kb, 3000)
            self.assertEqual(rust.items[0].path, f"{home}/code/api/target")
            self.assertEqual(got["culprits"][2].state, "unavailable")
            self.assertTrue(seen)  # progress was reported
            self.assertIsNone(got["home"])  # measured only when asked for


if __name__ == "__main__":
    unittest.main()
