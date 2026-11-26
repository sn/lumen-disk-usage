"""Tests for what the pane shows and copies, without a running Lumen.

    python3 -m unittest discover -s tests
"""

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scan  # noqa: E402
import view  # noqa: E402

GB = 1024 * 1024
NOW = 1_000_000_000


def plain(line):
    return re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", line)


def sample():
    docker = scan.Culprit("docker", "Docker", kb=124 * GB, reclaim_kb=76 * GB, state="ok",
                          command=["docker", "system", "prune"], ordered=True)
    docker.items = [
        scan.Item("Images", kb=45 * GB, reclaim_kb=39 * GB, command=["docker", "image", "prune", "-a"],
                  note="264 in all, 11 in use"),
        scan.Item("image node:22", name="node:22", kb=GB, command=["docker", "image", "rm", "node:22"], note="unused"),
    ]
    rust = scan.Culprit("rust-builds", "Rust build output", unit="folders", kb=15 * GB, state="ok")
    rust.items = [
        scan.Item("lumen", path="/home/me/Development/lumen/target", kb=int(9.2 * GB), changed=NOW - 3 * 86400,
                  command=["cargo", "clean", "--manifest-path", "/home/me/Development/lumen/Cargo.toml"]),
        scan.Item("old", path="/home/me/my code/old thing/target", kb=6 * GB, exact=False, changed=NOW - 150 * 86400),
    ]
    home = scan.Culprit("home", "Largest in home", state="idle")
    return [docker, rust, home]


class RowTests(unittest.TestCase):
    def test_rows(self):
        culprits = sample()
        self.assertEqual(len(view.rows(culprits, set())), 3)
        listed = view.rows(culprits, {"rust-builds"})
        self.assertEqual([r.item.label if r.item else r.culprit.id for r in listed],
                         ["docker", "rust-builds", "lumen", "old", "home"])

    def test_shorten_keeps_the_end(self):
        path = "~/Development/lumenshell.dev/lumen/target"
        self.assertEqual(view.shorten(path, 80), path)
        cut = view.shorten(path, 20)
        self.assertEqual(view.width(cut), 20)
        self.assertTrue(cut.startswith("~/Dev") and cut.endswith("lumen/target") and "…" in cut)
        self.assertEqual(view.shorten(path, 1), "…")
        self.assertEqual(view.shorten(path, 0), "")
        self.assertLessEqual(view.width(view.shorten("日本語のフォルダ/とても長い名前", 9)), 9)

    def test_wrap(self):
        self.assertEqual(view.wrap("abcdefghij", 4, 2), ["abcd", "e…ij"])
        self.assertEqual(view.wrap("abc", 4, 2), ["abc"])
        self.assertEqual(view.wrap("", 4), [])


class CopyTests(unittest.TestCase):
    def setUp(self):
        self.docker, self.rust, self.home = sample()

    def test_path_is_quoted(self):
        row = view.Row(self.rust, self.rust.items[1])
        self.assertEqual(view.copy_text(row, "path"), ("'/home/me/my code/old thing/target'", None))

    def test_no_path_gives_the_name(self):
        row = view.Row(self.docker, self.docker.items[1])
        self.assertEqual(view.copy_text(row, "path"), ("node:22", None))
        self.assertEqual(view.copy_text(row, "command"), ("docker image rm node:22", None))

    def test_culprit_rows(self):
        self.assertEqual(view.copy_text(view.Row(self.docker), "command"), ("docker system prune", None))
        text, problem = view.copy_text(view.Row(self.docker), "path")
        self.assertIsNone(text)
        self.assertIn("no path", problem)
        text, problem = view.copy_text(view.Row(self.rust, self.rust.items[1]), "command")
        self.assertIsNone(text)
        self.assertTrue(problem)

    def test_strange_names_are_refused(self):
        item = scan.Item("x", path="/tmp/a\nrm -rf ~", command=["cargo", "clean", "--manifest-path", "/tmp/a\n/Cargo.toml"])
        row = view.Row(self.rust, item)
        typeable = lambda text: "\n" not in text  # noqa: E731
        for kind in ("path", "command"):
            text, problem = view.copy_text(row, kind, typeable=typeable)
            self.assertIsNone(text)
            self.assertIn("Not copied", problem)

        def refusing(word):
            raise ValueError(word)

        self.assertEqual(view.copy_text(row, "path", quote=refusing)[0], None)

    def test_clipboard_command(self):
        have = lambda *names: (lambda n: f"/usr/bin/{n}" if n in names else None)  # noqa: E731
        self.assertEqual(view.clipboard_command({"WAYLAND_DISPLAY": "w"}, have("wl-copy", "xclip"), "linux"), ["wl-copy"])
        self.assertEqual(view.clipboard_command({"DISPLAY": ":0"}, have("wl-copy", "xclip"), "linux"),
                         ["xclip", "-selection", "clipboard"])
        self.assertEqual(view.clipboard_command({"DISPLAY": ":0"}, have("xsel"), "linux"),
                         ["xsel", "--clipboard", "--input"])
        self.assertEqual(view.clipboard_command({}, have("pbcopy"), "darwin"), ["pbcopy"])
        self.assertIsNone(view.clipboard_command({}, have("wl-copy", "xclip"), "linux"))
        self.assertIsNone(view.clipboard_command({"DISPLAY": ":0"}, have(), "linux"))


class RenderTests(unittest.TestCase):
    def draw(self, cols=100, rows=24, **kw):
        args = dict(
            cols=cols, rows_=rows, culprits=sample(), expanded={"rust-builds"}, selected=0, top=0,
            drives=[scan.Drive("/", 916 * GB, 616 * GB, 253 * GB)], status="scanned 4 min ago",
            home="/home/me", now=NOW,
        )
        args.update(kw)
        lines, top = view.render(**args)
        return [plain(line) for line in lines], lines, top

    def test_fills_the_pane_exactly_at_any_size(self):
        for cols, rows in [(100, 24), (60, 12), (40, 8), (20, 6), (200, 60)]:
            text, _, _ = self.draw(cols, rows)
            self.assertEqual(len(text), rows, (cols, rows))
            for line in text:
                self.assertLessEqual(view.width(line), cols, (cols, rows, line))

    def test_what_it_says(self):
        text, _, _ = self.draw()
        whole = "\n".join(text)
        self.assertIn("scanned 4 min ago", text[0])
        self.assertIn("616 GB of 916 GB · 253 GB free", text[1])
        self.assertIn("71%", text[1])
        self.assertRegex(whole, r"▸ Docker\s+124 GB\s+76 GB reclaimable")
        self.assertRegex(whole, r"▾ Rust build output\s+15 GB\s+2 folders")
        self.assertRegex(whole, r"9\.2 GB\s+3 days\s+~/Development/lumen/target")
        self.assertRegex(whole, r"≥6\.0 GB\s+5 mon\s+~/my code/old thing/target")
        self.assertIn("enter to measure", whole)
        self.assertIn("docker system prune", text[-3])  # the selected row's command
        self.assertIn("c copy path", text[-1])

    def test_selected_item_is_shown_in_full(self):
        text, _, _ = self.draw(selected=2)
        self.assertEqual(text[-3].strip(), "/home/me/Development/lumen/target")
        self.assertEqual(text[-2].strip(), "cargo clean --manifest-path /home/me/Development/lumen/Cargo.toml")
        text, _, _ = self.draw(selected=3, cols=30)
        self.assertEqual("".join(line.strip() for line in text[-3:-1]), "/home/me/my code/old thing/target")

    def test_full_drive_is_red(self):
        _, raw, _ = self.draw(drives=[scan.Drive("/", 100 * GB, 95 * GB, 5 * GB)])
        self.assertIn(view.RED, raw[1])
        _, raw, _ = self.draw()
        self.assertNotIn(view.RED, raw[1])

    def test_scrolls_to_the_selection(self):
        many = scan.Culprit("node-modules", "node_modules", unit="folders", kb=GB, state="ok")
        many.items = [scan.Item(str(n), path=f"/p/{n}/node_modules", kb=1024) for n in range(50)]
        text, _, top = self.draw(rows=14, culprits=[many], expanded={"node-modules"}, selected=40)
        self.assertGreater(top, 0)
        self.assertIn("/p/39/node_modules", "\n".join(text))
        self.assertEqual(text[-3].strip(), "/p/39/node_modules")
        _, _, top = self.draw(rows=14, culprits=[many], expanded={"node-modules"}, selected=0, top=top)
        self.assertEqual(top, 0)

    def test_states(self):
        down = scan.Culprit("docker", "Docker", state="unavailable", note="not running")
        busy = scan.Culprit("rust-builds", "Rust build output", unit="folders", kb=GB, state="pending")
        busy.items = [scan.Item("a", path="/a/target", kb=GB), scan.Item("b", path="/b/target")]
        text, _, _ = self.draw(culprits=[down, busy], expanded={"rust-builds"})
        whole = "\n".join(text)
        self.assertRegex(whole, r"Docker\s+—\s+not running")
        self.assertRegex(whole, r"Rust build output\s+1\.0 GB…")
        self.assertRegex(whole, r"…\s+/b/target")
        text, _, _ = self.draw(culprits=[], status="scanning…")
        self.assertIn("Looking for what is using the space", "\n".join(text))

    def test_strange_names_are_drawn_safely(self):
        c = scan.Culprit("node-modules", "node_modules", kb=GB, state="ok")
        c.items = [scan.Item("x", path="/p/evil\x1b[2J\n/node_modules", kb=GB)]
        _, raw, _ = self.draw(culprits=[c], expanded={"node-modules"}, selected=1)
        self.assertNotIn("\x1b[2J", "".join(raw))
        self.assertNotIn("\n", "".join(raw))


if __name__ == "__main__":
    unittest.main()
