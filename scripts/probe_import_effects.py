"""Observe one import; isolation must be provided by the invoking OS sandbox."""

import argparse
import asyncio
import hashlib
import importlib
import json
import logging
import os
import re
import sys
import threading

try:
    import pwd
except ImportError:  # Platforms without an account database cannot establish the boundary.
    pwd = None


def within(path, root):
    try:
        return os.path.commonpath((os.path.realpath(os.fsdecode(path)), root)) == root
    except (TypeError, ValueError, OSError):
        return False


def open_access(mode, flags):
    flags = flags if isinstance(flags, int) else 0
    writing = bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
    reading = not bool(flags & os.O_WRONLY) or bool(flags & os.O_RDWR)
    if isinstance(mode, str):
        writing = writing or any(c in mode for c in "wax+")
        reading = "r" in mode or "+" in mode
    return "read_write" if reading and writing else "write" if writing else "read"


def checkout_paths(root):
    """Place the checkout and its optional lionhub source before installed packages."""
    paths = [root]
    lionhub = os.path.join(root, "lionhub", "src")
    if os.path.isdir(lionhub):
        paths.append(lionhub)
    return paths


def callsite():
    frame = sys._getframe(1)
    while frame:
        filename = frame.f_code.co_filename
        if filename != __file__ and not filename.startswith("<frozen importlib"):
            return {"file": filename, "line": frame.f_lineno, "function": frame.f_code.co_name}
        frame = frame.f_back
    return None


class Observer:
    def __init__(self, home, roots, limit=2000):
        self.home, self.roots, self.limit = home, roots, limit
        self.events, self.counts, self.dropped = [], {}, 0

    def describe(self, path):
        if not isinstance(path, (str, bytes, os.PathLike)):
            return "<file-descriptor>"
        path = os.path.realpath(os.fsdecode(path))
        for index, root in enumerate(self.roots):
            if within(path, root):
                return f"<read-root-{index}>/" + os.path.relpath(path, root)
        if within(path, self.home):
            return "<account-home>/" + os.path.relpath(path, self.home)
        return path

    def add(self, kind, detail, *, blocked=False):
        self.counts[kind] = self.counts.get(kind, 0) + 1
        # Reserve capacity for blocked effects even when import machinery reads
        # thousands of files before reaching the target module.
        if kind == "filesystem.read" and self.counts[kind] > min(200, self.limit):
            self.dropped += 1
            return
        event = {
            "kind": kind,
            "detail": detail,
            "blocked_by_probe": blocked,
            "callsite": callsite(),
        }
        if len(self.events) < self.limit:
            self.events.append(event)
        else:
            self.dropped += 1

    def audit(self, event, args):
        if event == "open":
            path, mode, flags = args
            kind = open_access(mode, flags)
            name = self.describe(path)
            home = name.startswith("<account-home>/")
            blocked = kind != "read" or home
            self.add("filesystem." + kind, name, blocked=blocked)
            if home:
                self.add("account_home.access", name, blocked=True)
            if blocked:
                raise PermissionError("probe blocked file access")
        elif event in {"os.listdir", "os.scandir", "os.chdir"}:
            name = self.describe(args[0]) if args else "<unknown>"
            home = name.startswith("<account-home>/")
            self.add(
                "filesystem." + ("change_directory" if event == "os.chdir" else "read"),
                name,
                blocked=home,
            )
            if home:
                self.add("account_home.access", name, blocked=True)
                raise PermissionError("probe blocked account home access")
        elif event in {
            "os.remove",
            "os.rename",
            "os.mkdir",
            "os.rmdir",
            "os.chmod",
            "os.chown",
            "os.utime",
            "os.link",
            "os.symlink",
            "os.truncate",
        }:
            paths = [self.describe(a) for a in args[:2] if isinstance(a, (str, bytes, os.PathLike))]
            self.add("filesystem.write", {"operation": event, "paths": paths}, blocked=True)
            for path in paths:
                if path.startswith("<account-home>/"):
                    self.add("account_home.access", path, blocked=True)
            raise PermissionError("probe blocked filesystem mutation")
        elif event.startswith("socket."):
            self.add("network", event, blocked=True)
            raise PermissionError("probe blocked socket operation")
        elif event.startswith("sqlite3.connect"):
            self.add("database.sqlite", event, blocked=True)
            raise PermissionError("probe blocked database connection")
        elif event in {
            "subprocess.Popen",
            "os.system",
            "os.fork",
            "os.forkpty",
            "os.posix_spawn",
            "os.exec",
            "pty.spawn",
        }:
            self.add("subprocess", event, blocked=True)
            raise PermissionError("probe blocked process creation")


def registry_snapshot():
    """Observe the class registry and class-owned adapter registries already loaded."""
    result = {}
    registry_module = sys.modules.get("lionagi._class_registry")
    if registry_module:
        registry = vars(registry_module).get("LION_CLASS_REGISTRY")
        if isinstance(registry, dict):
            result["lionagi._class_registry.LION_CLASS_REGISTRY"] = {
                str(k): id(v) for k, v in registry.items()
            }
    for mod_name, module in tuple(sys.modules.items()):
        if not mod_name.startswith("lionagi.") or module is None:
            continue
        try:
            members = tuple(vars(module).items())
        except TypeError:
            continue
        for cls_name, cls in members:
            if not isinstance(cls, type) or vars(cls).get("__module__") != mod_name:
                continue
            own = vars(cls).get("_adapter_registry")
            try:
                entries = vars(own).get("_reg") if own is not None else None
            except TypeError:
                continue
            if isinstance(entries, dict):
                result[f"{mod_name}.{cls_name}._adapter_registry"] = {
                    str(k): id(v) for k, v in entries.items()
                }
    return result


def logger_snapshot():
    result = {}
    root = logging.getLogger()
    entries = [("root", root), *tuple(logging.root.manager.loggerDict.items())]
    for name, logger in entries:
        if isinstance(logger, logging.Logger):
            result[name] = (
                logger.level,
                logger.disabled,
                logger.propagate,
                tuple(id(h) for h in logger.handlers),
            )
    return result


def snapshot():
    return {
        "environment": dict(os.environ),
        "cwd": os.getcwd(),
        "sys.path": tuple(sys.path),
        "sys.meta_path": tuple(map(id, sys.meta_path)),
        "loggers": logger_snapshot(),
        "registries": registry_snapshot(),
    }


def changes(before, after):
    result = []
    for key in ("cwd", "sys.path", "sys.meta_path"):
        if before[key] != after[key]:
            result.append({"kind": "global." + key, "detail": "changed"})
    env_keys = sorted(
        k
        for k in before["environment"].keys() | after["environment"].keys()
        if before["environment"].get(k) != after["environment"].get(k)
    )
    if env_keys:
        result.append({"kind": "global.environment", "detail": env_keys})
    for kind in ("loggers", "registries"):
        a, b = before[kind], after[kind]
        for name in sorted(a.keys() | b.keys()):
            if a.get(name) != b.get(name):
                detail = {
                    "name": name,
                    "operation": "created"
                    if name not in a
                    else "removed"
                    if name not in b
                    else "changed",
                }
                if kind == "registries":
                    old, new = a.get(name, {}), b.get(name, {})
                    detail["added_keys"] = sorted(new.keys() - old.keys())
                    detail["removed_keys"] = sorted(old.keys() - new.keys())
                    detail["changed_keys"] = sorted(
                        k for k in old.keys() & new.keys() if old[k] != new[k]
                    )
                else:

                    def logger_fields(value):
                        return (
                            None
                            if value is None
                            else {
                                "level": value[0],
                                "disabled": value[1],
                                "propagate": value[2],
                                "handlers": len(value[3]),
                            }
                        )

                    detail["before"] = logger_fields(a.get(name))
                    detail["after"] = logger_fields(b.get(name))
                result.append({"kind": "global." + kind, "detail": detail})
    return result


def install_guards(observer):
    original_thread = threading.Thread.start
    original_create = asyncio.create_task
    original_ensure = asyncio.ensure_future
    original_loop_create = asyncio.BaseEventLoop.create_task

    def no_thread(*args, **kwargs):
        observer.add("background.thread", "Thread.start", blocked=True)
        raise PermissionError("probe blocked background thread")

    def no_task(*args, **kwargs):
        observer.add("background.task", "task creation", blocked=True)
        raise PermissionError("probe blocked background task")

    threading.Thread.start = no_thread
    asyncio.create_task = no_task
    asyncio.ensure_future = no_task
    asyncio.BaseEventLoop.create_task = no_task
    for method in ("setLevel", "addHandler", "removeHandler", "addFilter", "removeFilter"):
        original = getattr(logging.Logger, method)

        def record_logger(self, *args, _original=original, _method=method, **kwargs):
            observer.add("logger.mutation", {"logger": self.name, "method": _method})
            return _original(self, *args, **kwargs)

        setattr(logging.Logger, method, record_logger)
    return original_thread, original_create, original_ensure, original_loop_create


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--modules", required=True, help="one dotted module name per line")
    parser.add_argument(
        "--index", type=int, required=True, help="zero-based module-list line index"
    )
    parser.add_argument("--output", required=True, help="append one result as JSONL")
    parser.add_argument("--source-root", required=True, help="root placed first on sys.path")
    parser.add_argument("--real-home", help="verify the account home derived from the OS")
    parser.add_argument(
        "--read-root",
        action="append",
        default=[],
        help="narrow permitted read path under account home",
    )
    parser.add_argument(
        "--external-sandbox",
        action="store_true",
        help="acknowledge kernel sandbox is configured externally",
    )
    args = parser.parse_args()
    if not args.external_sandbox:
        parser.error("an externally enforced kernel sandbox is required")
    try:
        if pwd is None or not hasattr(os, "getuid"):
            raise OSError("account lookup unavailable")
        account_home = pwd.getpwuid(os.getuid()).pw_dir
        if not account_home or not os.path.isabs(account_home):
            raise ValueError("invalid account home")
        home = os.path.realpath(account_home)
    except (OSError, KeyError, TypeError, ValueError):
        parser.error("cannot determine the real account home")
    scratch = os.path.realpath(os.environ.get("HOME", ""))
    root = os.path.realpath(args.source_root)
    roots = [root, *(os.path.realpath(p) for p in args.read_root)]
    if home == os.path.sep or not os.path.isdir(home):
        parser.error("real account home must be an absolute existing directory")
    if args.real_home is not None and (
        not os.path.isabs(args.real_home) or os.path.realpath(args.real_home) != home
    ):
        parser.error("--real-home does not match the account home")
    if (
        not os.environ.get("HOME")
        or not os.path.isdir(scratch)
        or within(scratch, home)
        or within(home, scratch)
    ):
        parser.error("HOME must be disjoint from the account home")
    if (
        not os.environ.get("TMPDIR")
        or not os.path.isdir(os.environ["TMPDIR"])
        or within(os.path.realpath(os.environ["TMPDIR"]), home)
        or within(home, os.path.realpath(os.environ["TMPDIR"]))
    ):
        parser.error("TMPDIR must be a scratch directory")
    if any(not os.path.isdir(r) or within(home, r) for r in roots):
        parser.error("read roots must exist and must not contain the account home")
    list_bytes = open(args.modules, "rb").read()
    lines = list_bytes.decode("utf-8").splitlines()
    if args.index < 0 or args.index >= len(lines):
        parser.error("index outside module list")
    module = lines[args.index]
    if not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", module, re.ASCII):
        parser.error("invalid module name")
    output = open(args.output, "a", encoding="utf-8")
    import_paths = checkout_paths(root)
    sys.path[:0] = import_paths
    observer = Observer(home, roots)
    install_guards(observer)
    before = snapshot()
    sys.addaudithook(observer.audit)
    exception = None
    try:
        importlib.import_module(module)
    except BaseException as exc:
        traceback = exc.__traceback__
        location = None
        while traceback:
            frame = traceback.tb_frame
            if frame.f_code.co_filename != __file__ and not frame.f_code.co_filename.startswith(
                "<frozen importlib"
            ):
                location = {
                    "file": frame.f_code.co_filename,
                    "line": traceback.tb_lineno,
                    "function": frame.f_code.co_name,
                }
            traceback = traceback.tb_next
        exception = {"type": type(exc).__name__, "callsite": location}
    after = snapshot()
    result = {
        "module": module,
        "list_index": args.index,
        "module_list_sha256": hashlib.sha256(list_bytes).hexdigest(),
        "python": sys.version.split()[0],
        "status": "import_error" if exception else "complete",
        "exception": exception,
        "events": observer.events,
        "event_counts": observer.counts,
        "events_dropped": observer.dropped,
        "state_changes": changes(before, after),
        "external_sandbox": "asserted by caller, not verified by probe",
        "read_roots": roots,
        "checkout_import_paths": import_paths,
        "loaded_lionhub_origin": vars(sys.modules["lionhub"]).get("__file__")
        if sys.modules.get("lionhub")
        else None,
        "limits": [
            "Python audit events and selected in-memory state only; native operations may be unobserved.",
            "Blocked operations can stop imports early and hide later effects.",
            "Preloading stdlib guards and snapshots can alter import behavior.",
            "Only SQLite database opens and named class/adapter registries are observed.",
        ],
    }
    output.write(json.dumps(result, sort_keys=True) + "\n")
    output.close()
    return 1 if exception is not None else 0


if __name__ == "__main__":
    raise SystemExit(main())
