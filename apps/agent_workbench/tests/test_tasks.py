# SPDX-License-Identifier: Apache-2.0
"""Every task's expected answer is re-derived here from the task's files alone."""

import csv
import hashlib
import io
import math
import re
from collections import defaultdict
from datetime import datetime

import pytest

from agent_workbench.server.tasks import TASKS, build_task, score_answer


def rows(text):
    return list(csv.DictReader(io.StringIO(text)))


def _srt_ms(stamp):
    hours, minutes, seconds, millis = (
        int(stamp[:2]),
        int(stamp[3:5]),
        int(stamp[6:8]),
        int(stamp[9:]),
    )
    return ((hours * 60 + minutes) * 60 + seconds) * 1000 + millis


def solve(task):
    f = task.files
    q = task.instruction
    t = task.spec.template
    if t == "npv":
        rate = (
            float(
                re.search(r"approved by finance for this project: ([\d.]+)%", f["memo.txt"]).group(
                    1
                )
            )
            / 100
        )
        return round(
            sum(
                float(r["amount"]) / (1 + rate) ** int(r["year"]) for r in rows(f["cashflows.csv"])
            ),
            2,
        )
    if t == "loan":
        text = f["loan_terms.txt"]
        p = float(re.search(r"Principal: (\d+)", text).group(1))
        i = float(re.search(r"rate .*?: ([\d.]+)%", text).group(1)) / 1200
        n = int(re.search(r"Term: (\d+) years", text).group(1)) * 12
        return round(p * i / (1 - (1 + i) ** -n), 2)
    if t == "outstanding":
        cust = re.search(r"for customer (\w+)", q).group(1)
        paid = defaultdict(float)
        for r in rows(f["payments.csv"]):
            paid[r["invoice_id"]] += float(r["amount"])
        return round(
            sum(
                float(r["amount"]) - paid[r["invoice_id"]]
                for r in rows(f["invoices.csv"])
                if r["customer"] == cust
            ),
            2,
        )
    if t == "expenses":
        cat, month = re.search(r"most on (\w+) in (\w+) 2026", q).groups()
        m = datetime.strptime(month, "%B").month
        tot = defaultdict(float)
        for r in rows(f["expenses.csv"]):
            if r["category"] == cat and int(r["date"][5:7]) == m:
                tot[r["department"]] += float(r["amount"])
        return max(tot, key=tot.get)
    if t == "meetings":
        who, day = re.search(r"how many minutes is (\w+) in meetings on ([\d-]+)", q).groups()
        minutes = set()
        for r in rows(f["calendar.csv"]):
            if r["date"] == day and who in r["attendees"].split(";"):
                s = int(r["start"][:2]) * 60 + int(r["start"][3:])
                e = int(r["end"][:2]) * 60 + int(r["end"][3:])
                minutes.update(range(s, e))
        return len(minutes)
    if t == "reports":
        boss = re.search(r"reports to (\w+)", q).group(1)
        emp = rows(f["employees.csv"])
        by_id = {r["id"]: r["name"] for r in emp}
        kids = defaultdict(list)
        for r in emp:
            if r["manager_id"]:
                kids[by_id[r["manager_id"]]].append(r["name"])
        out, stack = set(), [boss]
        while stack:
            for k in kids[stack.pop()]:
                if k not in out:
                    out.add(k)
                    stack.append(k)
        return sorted(out)
    if t == "gas":
        text = f["lab_notes.txt"]
        v = float(re.search(r"volume: ([\d.]+) L", text).group(1)) / 1000
        p = float(re.search(r"pressure: ([\d.]+) kPa", text).group(1)) * 1000
        tc = float(re.search(r"reading: (-?[\d.]+) degC", text).group(1))
        return round(p * v / (8.314 * (tc + 273.15)), 3)
    if t == "molar":
        masses = {r["symbol"]: float(r["atomic_mass"]) for r in rows(f["atomic_masses.csv"])}
        formula = re.search(r"molar mass of (\S+) in", q).group(1)

        def mass(s):
            total, i = 0.0, 0
            while i < len(s):
                if s[i] == "(":
                    depth, j = 1, i + 1
                    while depth:
                        depth += {"(": 1, ")": -1}.get(s[j], 0)
                        j += 1
                    inner = mass(s[i + 1 : j - 1])
                    m = re.match(r"\d+", s[j:])
                    total += inner * (int(m.group()) if m else 1)
                    i = j + (len(m.group()) if m else 0)
                else:
                    m = re.match(r"([A-Z][a-z]?)(\d*)", s[i:])
                    total += masses[m.group(1)] * int(m.group(2) or 1)
                    i += len(m.group())
            return total

        return round(mass(formula), 2)
    if t == "halflife":
        r = rows(f["decay.csv"])
        t0, a0 = float(r[0]["t_hours"]), float(r[0]["activity_bq"])
        t1, a1 = float(r[-1]["t_hours"]), float(r[-1]["activity_bq"])
        return round((t1 - t0) * math.log(2) / math.log(a0 / a1), 2)
    if t == "divisible":
        v = dict(re.findall(r"(\w) = (\d+)", f["params.txt"]))
        n, a, b, c = (int(v[k]) for k in "Nabc")
        lcm = lambda x, y: x * y // math.gcd(x, y)  # noqa: E731
        cnt = lambda d: n // d  # noqa: E731
        ab = cnt(a) + cnt(b) - cnt(lcm(a, b))
        abc = cnt(lcm(a, c)) + cnt(lcm(b, c)) - cnt(lcm(lcm(a, b), c))
        return ab - abc
    if t == "paths":
        g = f["grid.txt"].split()
        h, w = len(g), len(g[0])
        dp = [[0] * (w + 1) for _ in range(h + 1)]
        for r in range(h):
            for c in range(w):
                if g[r][c] == "#":
                    continue
                dp[r + 1][c + 1] = 1 if r == c == 0 else dp[r][c + 1] + dp[r + 1][c]
        return dp[h][w]
    if t == "powersum":
        p, n, m = map(int, re.search(r"\+ \.\.\. \+ (\d+)\^(\d+)\) mod (\d+)", q).group(2, 1, 3))
        return sum(pow(k, p, m) for k in range(1, n + 1)) % m
    if t == "bruteforce":
        th = int(re.search(r"at least (\d+) failed", q).group(1))
        fails, hit = defaultdict(int), []
        for line in f["auth.log"].splitlines():
            ip = re.search(r"from ([\d.]+)", line).group(1)
            if "Failed password" in line:
                fails[ip] += 1
            elif "Accepted" in line and fails[ip] >= th:
                hit.append(ip)
        assert len(set(hit)) == 1
        return hit[0]
    if t == "firewall":
        rules = []
        for line in f["firewall.rules"].splitlines():
            if line.startswith("#"):
                continue
            act, proto, port, src = line.split()
            rules.append((act, proto, int(port.split("=")[1]), src.split("=")[1]))

        def ip2int(ip):
            return int.from_bytes(bytes(map(int, ip.split("."))), "big")

        def match(ip, cidr):
            net, bits = cidr.split("/")
            bits = int(bits)
            return bits == 0 or (ip2int(ip) >> (32 - bits)) == (ip2int(net) >> (32 - bits))

        allowed = 0
        for r in rows(f["connections.csv"]):
            for act, proto, port, cidr in rules:
                if (
                    proto in ("any", r["proto"])
                    and port in (0, int(r["dst_port"]))
                    and match(r["src_ip"], cidr)
                ):
                    allowed += act == "allow"
                    break
        return allowed
    if t == "ioc":
        bad = {ln for ln in f["ioc_sha256.txt"].split() if not ln.startswith("#") and len(ln) == 64}
        return sorted(
            n
            for n, body in f.items()
            if n.startswith("samples/") and hashlib.sha256(body.encode()).hexdigest() in bad
        )
    if t == "subtitles":
        who = re.search(r"in total are (\w+)'s cues", q).group(1)
        total = 0
        for block in f["subtitles.srt"].strip().split("\n\n"):
            lines = block.strip().splitlines()
            if f"[{who}]" in lines[2]:
                a, b = lines[1].split(" --> ")
                total += _srt_ms(b) - _srt_ms(a)
        return round(total / 1000, 2)
    if t == "edit":
        fps = int(re.search(r"at (\d+) fps", q).group(1))
        frames = sum(
            int(r["out_frame"]) - int(r["in_frame"]) - int(r["crossfade_frames"])
            for r in rows(f["edit_list.csv"])
        )
        return round(frames / fps, 2)
    if t == "sensor":
        th = float(re.search(r"strictly above ([\d.]+) degC", q).group(1))
        k = int(re.search(r"first run of (\d+) consecutive", q).group(1))
        r = rows(f["sensor.csv"])
        for i in range(len(r) - k + 1):
            if all(float(x["temp_c"]) > th for x in r[i : i + k]):
                return r[i]["timestamp"]
    if t == "oee":
        m = re.search(r"of machine (\w+) for", q).group(1)
        r = next(x for x in rows(f["shift_log.csv"]) if x["machine"] == m)
        run = (int(r["planned_min"]) - int(r["downtime_min"])) * 60
        good = int(r["total_count"]) - int(r["reject_count"])
        return (
            round(good * int(r["ideal_cycle_s"]) / (int(r["planned_min"]) * 60) * 100, 1)
            if run
            else 0.0
        )
    if t == "line":
        return math.floor(
            min(
                3600 / int(r["cycle_time_s"]) * int(r["parallel_units"]) * float(r["availability"])
                for r in rows(f["stations.csv"])
            )
            + 1e-9
        )
    raise AssertionError(t)


NON_CODE = [tid for tid in sorted(TASKS) if TASKS[tid].domain != "software"]


def test_catalog_shape():
    assert len(TASKS) == 40
    per_domain = defaultdict(int)
    for spec in TASKS.values():
        per_domain[spec.domain] += 1
    assert set(per_domain.values()) == {5} and len(per_domain) == 8


@pytest.mark.parametrize("task_id", NON_CODE)
def test_expected_answer_matches_an_independent_solution(task_id):
    task = build_task(task_id)
    if task.answer_kind == "constraints":
        pytest.skip("generative task, checked separately")
    want = solve(task)
    if isinstance(want, list):
        assert want == task.expected
        answer = ", ".join(want)
    elif isinstance(want, str):
        assert want == task.expected
        answer = want
    else:
        tol = max(task.abs_tol, task.rel_tol * abs(task.expected), 1e-9)
        assert abs(want - task.expected) <= tol, (want, task.expected)
        answer = f"{want:.{task.decimals}f}" if task.decimals else str(int(want))
    assert score_answer(task, answer)[0] == 1.0


@pytest.mark.parametrize("task_id", NON_CODE)
def test_a_wrong_answer_earns_nothing(task_id):
    task = build_task(task_id)
    assert score_answer(task, "0.123456789 is my guess maybe")[0] == 0.0
    assert score_answer(task, "")[0] == 0.0


@pytest.mark.parametrize("task_id", sorted(TASKS))
def test_tasks_are_deterministic(task_id):
    a, b = build_task(task_id), build_task(task_id)
    assert a.files == b.files and a.instruction == b.instruction and a.expected == b.expected


def test_headline_scoring():
    task = next(build_task(t) for t in sorted(TASKS) if TASKS[t].template == "headline")
    assert score_answer(task, "x")[0] < 0.6
    brief = task.files["brief.txt"]
    words = re.findall(r"'(\w+)'", brief)
    good = (" ".join(words)).capitalize()
    assert score_answer(task, good)[0] == 1.0


def test_number_format_partial_credit():
    task = build_task(
        next(
            t
            for t in NON_CODE
            if build_task(t).answer_kind == "number" and build_task(t).decimals == 2
        )
    )
    exact = f"{task.expected:.2f}"
    assert score_answer(task, exact)[0] == 1.0
    assert score_answer(task, f"${exact} USD")[0] == 0.5
