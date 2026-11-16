"""Regression probes for review 54, through the public CLI only."""

import os
import subprocess

from test_cli import APP, ENV, PYTHON, WarehouseTestCase, order


class Review54Tests(WarehouseTestCase):
    def test_special_database_targets_are_rejected_without_files(self):
        source = self.write_json([order("o1")])
        for target in (":memory:", ":memory:session", ":default:", "md:", "motherduck:"):
            for args in (("load", source, "--db", target), ("summary", "--db", target)):
                with self.subTest(args=args):
                    before = set(self.dir.iterdir())
                    self.assert_error(self.run_app(*args), 3)
                    self.assertEqual(set(self.dir.iterdir()), before)

    def test_stdout_failures_are_controlled_io_errors(self):
        self.load([order("o1")])
        commands = (("summary", "--db", self.db_path()), ("--version",), ("--help",))
        for args in commands:
            for sink_kind in ("pipe", "full"):
                with self.subTest(args=args, sink=sink_kind):
                    if sink_kind == "full":
                        if not os.path.exists("/dev/full"):
                            continue  # macOS: the closed-pipe probe still runs.
                        writer = os.open("/dev/full", os.O_WRONLY)
                    else:
                        reader, writer = os.pipe()
                        os.close(reader)
                    with os.fdopen(writer, "wb") as sink:
                        result = subprocess.run(
                            [PYTHON, str(APP), *args],
                            stdout=sink,
                            stderr=subprocess.PIPE,
                            text=True,
                            encoding="utf-8",
                            env=ENV,
                            cwd=self.dir,
                            timeout=30,
                        )
                    self.assertIsNone(result.stdout)
                    result.stdout = ""  # redirected rather than captured
                    self.assert_error(result, 3)

    def test_unknown_arguments_do_not_echo_raw_content(self):
        secret = "PRIVATE_SENTINEL\nforged\r\x1b\x07"
        for args in ((secret,), ("summary", "--db", self.db_path(), "--" + secret)):
            with self.subTest(args=args):
                result = self.run_app(*args)
                self.assert_error(result, 2)
                self.assertNotIn("PRIVATE_SENTINEL", result.stderr)
                self.assertTrue(all(ord(c) >= 32 and ord(c) != 127 for c in result.stderr[:-1]))
