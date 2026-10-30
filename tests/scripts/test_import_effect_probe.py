"""Synthetic checks for the observation and refusal logic; no target imports."""

import asyncio
import importlib.machinery
import importlib.util
import logging
import os
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "import_effect_probe", ROOT / "scripts/probe_import_effects.py"
)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class ObserverTests(unittest.TestCase):
    def setUp(self):
        self.observer = probe.Observer("/nonexistent-account", ["/nonexistent-account/source"])

    def test_open_modes_and_flags(self):
        for mode, flags, expected in [
            ("r", os.O_RDONLY, "read"),
            ("w", os.O_WRONLY, "write"),
            ("a", os.O_WRONLY | os.O_APPEND, "write"),
            ("r+", os.O_RDWR, "read_write"),
            (None, os.O_RDWR, "read_write"),
            (None, os.O_RDONLY, "read"),
        ]:
            with self.subTest(mode=mode, flags=flags):
                self.assertEqual(probe.open_access(mode, flags), expected)

    def test_checkout_lionhub_source_precedes_installed_package(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "checkout"
            checkout_package = root / "lionhub" / "src" / "lionhub"
            checkout_package.mkdir(parents=True)
            (checkout_package / "__init__.py").write_text("", encoding="utf-8")
            installed = Path(directory) / "installed"
            installed_package = installed / "lionhub"
            installed_package.mkdir(parents=True)
            (installed_package / "__init__.py").write_text("", encoding="utf-8")
            paths = probe.checkout_paths(str(root)) + [str(installed)]
            self.assertEqual(paths[:2], [str(root), str(root / "lionhub" / "src")])
            spec = importlib.machinery.PathFinder.find_spec("lionhub", paths)
            self.assertEqual(Path(spec.origin), checkout_package / "__init__.py")
            self.assertEqual(probe.checkout_paths(str(installed)), [str(installed)])

    def test_read_under_explicit_root_and_callsite(self):
        self.observer.audit("open", ("/nonexistent-account/source/reader.py", "r", os.O_RDONLY))
        event = self.observer.events[-1]
        self.assertEqual(event["kind"], "filesystem.read")
        self.assertEqual(event["detail"], "<read-root-0>/reader.py")
        self.assertEqual(
            event["callsite"]["function"], "test_read_under_explicit_root_and_callsite"
        )

    def test_real_home_access_is_recorded_and_refused(self):
        with self.assertRaises(PermissionError):
            self.observer.audit("open", ("/nonexistent-account/secret", "r", os.O_RDONLY))
        self.assertEqual(self.observer.counts["account_home.access"], 1)

    def test_write_and_real_home_mutations_are_recorded_and_refused(self):
        with self.assertRaises(PermissionError):
            self.observer.audit("open", ("/tmp/fixture", "w", os.O_WRONLY))
        with self.assertRaises(PermissionError):
            self.observer.audit("os.rename", ("/nonexistent-account/old", "/tmp/new", -1, -1))
        self.assertEqual(self.observer.counts["filesystem.write"], 2)
        self.assertEqual(self.observer.counts["account_home.access"], 1)

    def test_network_database_and_process_attempts_are_refused(self):
        for event, kind in [
            ("socket.__new__", "network"),
            ("socket.connect", "network"),
            ("sqlite3.connect", "database.sqlite"),
            ("subprocess.Popen", "subprocess"),
            ("os.system", "subprocess"),
            ("os.exec", "subprocess"),
        ]:
            with self.subTest(event=event), self.assertRaises(PermissionError):
                self.observer.audit(event, ())
            self.assertIn(kind, self.observer.counts)

    def test_event_limit_reports_truncation(self):
        self.observer.limit = 1
        self.observer.audit("open", ("/tmp/one", "r", os.O_RDONLY))
        self.observer.audit("open", ("/tmp/two", "r", os.O_RDONLY))
        self.assertEqual(self.observer.counts["filesystem.read"], 2)
        self.assertEqual(self.observer.dropped, 1)

    def test_thread_task_and_logger_hooks(self):
        method_names = ("setLevel", "addHandler", "removeHandler", "addFilter", "removeFilter")
        originals = {name: getattr(logging.Logger, name) for name in method_names}
        saved = probe.install_guards(self.observer)
        try:
            with self.assertRaises(PermissionError):
                threading.Thread(target=lambda: None).start()
            with self.assertRaises(PermissionError):
                asyncio.create_task(None)
            with self.assertRaises(PermissionError):
                asyncio.ensure_future(None)
            logger = logging.getLogger("synthetic_probe_test")
            logger.setLevel(logging.WARNING)
            self.assertEqual(self.observer.counts["background.thread"], 1)
            self.assertEqual(self.observer.counts["background.task"], 2)
            self.assertEqual(self.observer.counts["logger.mutation"], 1)
            event = next(e for e in self.observer.events if e["kind"] == "logger.mutation")
            self.assertEqual(event["callsite"]["function"], "test_thread_task_and_logger_hooks")
        finally:
            (
                threading.Thread.start,
                asyncio.create_task,
                asyncio.ensure_future,
                asyncio.BaseEventLoop.create_task,
            ) = saved
            for name, original in originals.items():
                setattr(logging.Logger, name, original)

    def test_global_logger_registry_and_environment_changes(self):
        original = {
            "environment": {"A": "a"},
            "cwd": "/",
            "sys.path": ("a",),
            "sys.meta_path": (1,),
            "loggers": {"old": (0, False, True, ())},
            "registries": {"r": {"x": 1}},
        }
        current = {
            "environment": {"A": "changed"},
            "cwd": "/tmp",
            "sys.path": ("b",),
            "sys.meta_path": (2,),
            "loggers": {"old": (10, False, True, ()), "new": (1, False, True, ())},
            "registries": {"r": {"x": 2, "y": 3}},
        }
        changes = probe.changes(original, current)
        categories = {entry["kind"] for entry in changes}
        self.assertEqual(
            categories,
            {
                "global.cwd",
                "global.sys.path",
                "global.sys.meta_path",
                "global.environment",
                "global.loggers",
                "global.registries",
            },
        )
        registry = next(
            entry["detail"] for entry in changes if entry["kind"] == "global.registries"
        )
        self.assertEqual(registry["added_keys"], ["y"])
        self.assertEqual(registry["changed_keys"], ["x"])


if __name__ == "__main__":
    unittest.main()
