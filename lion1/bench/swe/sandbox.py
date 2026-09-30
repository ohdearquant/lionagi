"""A SWE-bench instance as a box, and its grading in the same box.

The boxes are the hub's (`hub.tools.box`): a docker container (`DockerSandbox`, amd64 under
emulation on an arm64 host, one at a time is what it can bear) or a Daytona sandbox
(`DaytonaSandbox`, native x86_64, many in parallel, the provider's memory quota is the cap).
`testbed` makes either from the instance's eval image, which already holds the repository at the
base commit under /testbed with its environment installed: the box is the privilege boundary and the
host only reads the diff out.

`grade` scores a patch the official way inside the same box: clean tree, apply the patch with the
harness's three attempts, run the instance's own eval script, parse the log with the harness's
parser. No second container, no image round trip.
"""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path

from hub.tools import Box, DaytonaSandbox, DockerSandbox

WORKDIR = "/testbed"
PRELUDE = "source /opt/miniconda3/bin/activate testbed 2>/dev/null"  # the eval image's environment


def testbed(
    image: str, where: str = "local", *, cpu: int = 2, memory: int = 2, label: str = "lionagi-swe"
) -> Box:
    """A box over the instance's eval image, not yet started: docker for `local`, else Daytona."""
    if where == "local":
        return DockerSandbox(image, workdir=WORKDIR, prelude=PRELUDE)
    return DaytonaSandbox(image, workdir=WORKDIR, prelude=PRELUDE, cpu=cpu, memory=memory, label=label)


# --- grading in the box ---------------------------------------------------------------------------

_APPLY = (
    "git apply --verbose /tmp/model.patch",
    "git apply --verbose --reject /tmp/model.patch",
    "patch --batch --fuzz=5 -p1 -i /tmp/model.patch",
)


async def grade(
    box: Box,
    row: dict,
    patch: str,
    model_name: str,
    log_path: Path,
    *,
    eval_timeout: float = 1800.0,
    base: str | None = None,
) -> dict:
    """The official verdict for `patch` on this instance, computed in the box it was written in.

    Mirrors swebench.harness.run_evaluation.run_instance: clean tree at the base commit, the
    harness's three apply attempts, the instance's eval script, the harness's log parser and
    report. Returns that report's row for the instance plus a `status` naming any early stop.

    The reset is to `base`, the commit the model was handed (the row's `base_commit` when the
    caller read none; HEAD when the row names none either): index and tree both, so an edit the
    model staged or committed is gone before the patch goes on. A checkout from the index would keep
    a staged edit, and the patch, which holds the same edit, would then fail to apply.
    """
    from swebench.harness.grading import get_eval_report
    from swebench.harness.utils import make_test_spec

    iid = row["instance_id"]
    base = shlex.quote(base or row.get("base_commit") or "HEAD")
    rc, out = await box.run(f"git -c core.fileMode=false reset -q --hard {base} && git clean -fdq")
    if rc != 0:  # the model's own edits, tests included, would still be in the tree the eval runs on
        log_path.write_text(out)
        return {"resolved": False, "status": "RESET_FAIL", "error": f"reset exited {rc}: {out}"}
    await box.write("/tmp/model.patch", patch)
    applied = False
    for cmd in _APPLY:
        rc, out = await box.run(cmd, timeout=300)
        if rc == 0:
            applied = True
            break
    if not applied:
        log_path.write_text(out)
        return {"resolved": False, "status": "APPLY_PATCH_FAIL", "patch_successfully_applied": False}
    await box.write("/eval.sh", row["eval_script"])
    rc, out = await box.run("/bin/bash /eval.sh", timeout=eval_timeout)
    if rc == 124:
        out += f"\n\nTimeout error: {eval_timeout:.0f} seconds exceeded."
    log_path.write_text(out)
    report = get_eval_report(
        test_spec=make_test_spec(row),
        prediction={"instance_id": iid, "model_name_or_path": model_name, "model_patch": patch},
        test_log_path=str(log_path),
        include_tests_status=True,
    )
    verdict = dict(report[iid])
    verdict["status"] = "TIMEOUT" if rc == 124 else "GRADED"
    (log_path.with_suffix(".report.json")).write_text(json.dumps(report, indent=1))
    return verdict


CODE_HOSTS = (  # where a published fix would come from; an unaided run cannot reach them
    "github.com",
    "api.github.com",
    "codeload.github.com",
    "gist.github.com",
    "raw.githubusercontent.com",
    "patch-diff.githubusercontent.com",
    "objects.githubusercontent.com",
    "gitlab.com",
    "bitbucket.org",
    # package indexes: `pip download django==<next release>` is the fix too (on the hard set with the
    # code hosts closed: 8 loop runs and 4 codex runs asked PyPI for a later release)
    "pypi.org",
    "files.pythonhosted.org",
    "pypi.python.org",
    "test.pypi.org",
    "anaconda.org",
    "conda.anaconda.org",
    "repo.anaconda.com",
)
_MARK = "# lion-unaided"  # every line the closure adds ends with this; reopening deletes them
PROBED = ("github.com", "pypi.org")  # one code host and one index, proved from inside


LOOPBACK = ("127.0.0.1", "::1")


async def _probe(box: Box, host: str) -> tuple[list[str], str, str]:
    """(every address the name resolves to, the HTTP code, curl's rc) for `host`, read from inside
    the box. `getent ahosts` lists both families: `getent hosts` preferred the AAAA answer from DNS
    over the v4 line in /etc/hosts, so pypi.org read as open under a v4-only closure."""
    probe = f"curl -sS -m 8 -o /dev/null -w '%{{http_code}}' https://{host}/ 2>/dev/null; echo \" curl=$?\""
    _, out = await box.run(f"getent ahosts {host}; echo; {probe}")
    head, _, fetch = out.strip().rpartition("\n")
    addresses = [ln.split()[0] for ln in head.splitlines() if ln.strip()]
    # no curl in the box prints ` curl=127` alone: the code is empty, never the rc read as one
    code, _, curl_rc = fetch.rpartition("curl=")
    return addresses, code.strip(), curl_rc.strip()


async def close_code_hosts(box: Box) -> str:
    """Point the code hosts and the package indexes at localhost in the box's /etc/hosts, then prove
    it from inside: each probed name must resolve to us and its HTTPS fetch must fail (an answer,
    through a proxy say, is aided). Model calls leave from the host (LNDL) or go to openrouter.ai
    (codex), which stays open; only the answer's sources close. `open_code_hosts` undoes it before
    the grader runs, whose eval script may install."""
    lines = "".join(f"{ip} {h} {_MARK}\n" for h in CODE_HOSTS for ip in LOOPBACK)  # both families
    rc, out = await box.run(
        f"printf {shlex.quote(lines)} >> /etc/hosts && tail -n {2 * len(CODE_HOSTS)} /etc/hosts"
    )
    if rc != 0 or "pypi.org" not in out:
        raise RuntimeError(f"could not close the code hosts (rc {rc}): {out[-300:]}")
    said = []
    for host in PROBED:
        resolved, code, curl_rc = await _probe(box, host)
        # the control: every address of the name, v4 and v6, must be ours now
        if not resolved or any(a not in LOOPBACK for a in resolved):
            raise RuntimeError(f"{host} still resolves elsewhere: {' '.join(resolved)[-200:] or '(nothing)'}")
        if not (re.fullmatch(r"[0-9]{3}", code) and re.fullmatch(r"[0-9]+", curl_rc)):
            # no fetch was tried (curl missing, the probe cut short): nothing proves the host closed
            raise RuntimeError(f"{host} was not fetched from the box (HTTP {code!r}, curl rc {curl_rc!r})")
        if curl_rc == "0" or code[:1] in "123":
            raise RuntimeError(f"{host} is reachable from the box (HTTP {code}, curl rc {curl_rc}): aided")
        said.append(f"{host} HTTP {code or '?'} curl rc {curl_rc or '?'}")
    return "code hosts and package indexes closed; " + ", ".join(said)


async def open_code_hosts(box: Box) -> None:
    """Remove the closure's lines from /etc/hosts: the grader's eval script runs after the model and
    may install from an index; what the model could not reach is already on the record."""
    # rewritten in place: /etc/hosts is a bind mount in the box, so `sed -i` (rename over it) fails
    # with "Device or resource busy", and finished runs lost their grading to it
    mark = shlex.quote(_MARK)
    rc, out = await box.run(
        f"grep -v -F {mark} /etc/hosts > /tmp/lion-hosts; cat /tmp/lion-hosts > /etc/hosts"
        f" && rm -f /tmp/lion-hosts && ! grep -q -F {mark} /etc/hosts"
    )
    if rc != 0:
        raise RuntimeError(f"could not reopen the code hosts (rc {rc}): {out[-300:]}")
