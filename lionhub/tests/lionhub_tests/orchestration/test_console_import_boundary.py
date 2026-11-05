# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Console and mirror settings stay independent of orchestration activation."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[4]


@pytest.mark.parametrize(
    ("module_name", "relative_source", "extra_blocked"),
    [
        (
            "lionhub.orchestration.console",
            "lionhub/src/lionhub/orchestration/console.py",
            (),
        ),
        (
            "lionhub.serve.mirror_settings",
            "lionhub/src/lionhub/serve/mirror_settings.py",
            ("lionagi.cli._mirror_service", "lionagi.studio"),
        ),
    ],
    ids=["console", "mirror-settings"],
)
def test_shared_cli_values_do_not_activate_unrelated_features(
    module_name: str, relative_source: str, extra_blocked: tuple[str, ...], tmp_path: Path
) -> None:
    environment = {
        name: value
        for name, value in os.environ.items()
        if name in {"PATH", "SYSTEMROOT", "WINDIR", "LANG", "LC_ALL", "LC_CTYPE"}
    }
    for name, relative in {
        "HOME": "home",
        "USERPROFILE": "home",
        "LIONAGI_HOME": "lionagi",
        "TMPDIR": "tmp",
        "XDG_CONFIG_HOME": "config",
        "XDG_CACHE_HOME": "cache",
        "XDG_DATA_HOME": "data",
    }.items():
        directory = tmp_path / relative
        directory.mkdir(exist_ok=True)
        environment[name] = str(directory)
    environment["LIONAGI_STUDIO_MIRROR_PREVIEW_CHARS"] = "37"
    program = f"""
import importlib, importlib.abc, sys
from pathlib import Path

root = Path({str(_ROOT)!r}).resolve()
sys.path[:0] = [str(root), str(root / 'lionhub/src')]
blocked = ('lionhub.orchestration.patterns', 'lionagi.agent') + {extra_blocked!r}

def matches(name):
    return any(name == prefix or name.startswith(prefix + '.') for prefix in blocked)

assert not [name for name in sys.modules if name.partition('.')[0] in ('lionagi', 'lionhub')]
external_attempts = []
def refuse_external_action(event, args):
    if event in {{'socket.connect', 'socket.getaddrinfo', 'subprocess.Popen',
                 'os.system', 'os.posix_spawn', 'os.fork', 'os.forkpty', 'os.exec'}}:
        external_attempts.append(event)
        raise AssertionError('unexpected import side effect: ' + event)
sys.addaudithook(refuse_external_action)

class RefuseActivation(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if matches(fullname):
            raise AssertionError('unexpected feature import: ' + fullname)
        return None

sys.meta_path.insert(0, RefuseActivation())
import {module_name}
module = sys.modules[{module_name!r}]
assert Path(module.__file__).resolve() == root / {relative_source!r}
assert not [name for name in sys.modules if matches(name)]
assert not external_attempts
package_roots = (root / 'lionagi', root / 'lionhub/src/lionhub')
for name, loaded in tuple(sys.modules.items()):
    if loaded is None or name.partition('.')[0] not in ('lionagi', 'lionhub'):
        continue
    source = vars(loaded).get('__file__')
    if source is not None:
        assert any(Path(source).resolve().is_relative_to(path) for path in package_roots), (name, source)
    for path in vars(loaded).get('__path__', ()):
        assert any(Path(path).resolve().is_relative_to(root_path) for root_path in package_roots)
if {module_name!r} == 'lionhub.serve.mirror_settings':
    assert module.MIRROR_PREVIEW_CHARS == 37
"""
    process = subprocess.run(  # noqa: S603 -- this interpreter and a fixed test program
        [sys.executable, "-I", "-c", program],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert process.returncode == 0, process.stdout[-2000:] + process.stderr[-2000:]
    assert process.stdout == ""
