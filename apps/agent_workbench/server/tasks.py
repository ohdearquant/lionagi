# SPDX-License-Identifier: Apache-2.0
"""Task catalog for the Agent Workbench environment.

Every task is generated from its id: the same id always produces the same files and the
same expected answer. Expected answers are computed from the generated data when the task
is built and are never written into the agent's workspace.
"""

from __future__ import annotations

import hashlib
import math
import random
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable

__all__ = ("TASKS", "TaskSpec", "BuiltTask", "build_task", "score_answer")


@dataclass(frozen=True)
class TaskSpec:
    task_id: str
    domain: str
    template: str
    seed: int
    split: str = "train"


@dataclass
class BuiltTask:
    spec: TaskSpec
    instruction: str
    files: dict[str, str]
    answer_kind: str  # number | text | list | code | constraints
    expected: Any = None
    decimals: int = 2
    rel_tol: float = 0.0
    abs_tol: float = 0.0
    code_cases: list[tuple[str, tuple, Any]] = field(default_factory=list)
    constraints: list[tuple[str, Callable[[str], bool]]] = field(default_factory=list)


def _seed(task_id: str) -> int:
    return int(hashlib.sha256(task_id.encode()).hexdigest()[:12], 16)


def _csv(header: list[str], rows: list[list[Any]]) -> str:
    return "\n".join([",".join(header)] + [",".join(str(c) for c in r) for r in rows]) + "\n"


NUMBER_FORMAT = "Answer with the number only (no units, symbols or thousands separators)"


# ----------------------------------------------------------------------------- finance


def _fin_npv(rng: random.Random) -> BuiltTask:
    years = rng.randint(4, 7)
    invest = rng.randint(40, 120) * 1000
    flows = [-invest] + [rng.randint(8, 40) * 1000 + rng.randint(0, 999) for _ in range(years)]
    rate = rng.choice([4.5, 6.0, 7.25, 8.0, 9.5, 11.0])
    other = rng.choice([2.0, 3.5, 12.0, 15.0])
    memo = (
        "Project review memo\n"
        f"- Inflation assumption used by marketing: {other}%\n"
        f"- Discount rate approved by finance for this project: {rate}% per year\n"
        "- Cash flows are end-of-year; year 0 is today.\n"
    )
    npv = sum(cf / (1 + rate / 100) ** t for t, cf in enumerate(flows))
    return BuiltTask(
        spec=None,  # type: ignore[arg-type]
        instruction=(
            "Compute the net present value of the project in cashflows.csv at the discount rate "
            f"finance approved (see memo.txt). {NUMBER_FORMAT}, rounded to 2 decimals."
        ),
        files={
            "cashflows.csv": _csv(["year", "amount"], [[t, cf] for t, cf in enumerate(flows)]),
            "memo.txt": memo,
        },
        answer_kind="number",
        expected=round(npv, 2),
        decimals=2,
        abs_tol=0.011,
    )


def _fin_loan(rng: random.Random) -> BuiltTask:
    principal = rng.randint(80, 600) * 1000
    rate = rng.choice([3.75, 4.25, 5.5, 6.125, 6.9, 7.4])
    years = rng.choice([10, 15, 20, 25, 30])
    i = rate / 100 / 12
    n = years * 12
    pay = principal * i / (1 - (1 + i) ** -n)
    terms = (
        f"Borrower: unit {rng.randint(100, 999)}\n"
        f"Principal: {principal}\n"
        f"Annual interest rate (nominal, compounded monthly): {rate}%\n"
        f"Term: {years} years, monthly payments, fully amortizing\n"
        f"Origination fee (paid upfront, not financed): {rng.randint(500, 3000)}\n"
    )
    return BuiltTask(
        spec=None,  # type: ignore[arg-type]
        instruction=f"What is the monthly payment for the loan in loan_terms.txt? {NUMBER_FORMAT}, rounded to 2 decimals.",
        files={"loan_terms.txt": terms},
        answer_kind="number",
        expected=round(pay, 2),
        decimals=2,
        abs_tol=0.011,
    )


def _fin_outstanding(rng: random.Random) -> BuiltTask:
    customers = rng.sample(["Acme", "Birch", "Cobalt", "Dune", "Ember", "Fjord"], 4)
    target = customers[0]
    invoices, payments = [], []
    outstanding = 0.0
    for k in range(14):
        cust = customers[k % 4] if k < 12 else target
        amount = round(rng.randint(200, 5000) + rng.randint(0, 99) / 100, 2)
        inv = f"INV-{1000 + k}"
        invoices.append([inv, cust, f"{amount:.2f}", f"2026-0{1 + k % 8}-{10 + k}"])
        mode = rng.choice(["full", "partial", "none", "split"])
        paid = 0.0
        if mode == "full":
            payments.append([inv, f"{amount:.2f}", "2026-09-01"])
            paid = amount
        elif mode == "partial":
            p = round(amount * rng.choice([0.25, 0.5, 0.6]), 2)
            payments.append([inv, f"{p:.2f}", "2026-09-02"])
            paid = p
        elif mode == "split":
            p1 = round(amount / 3, 2)
            payments.append([inv, f"{p1:.2f}", "2026-08-01"])
            payments.append([inv, f"{p1:.2f}", "2026-09-01"])
            paid = 2 * p1
        if cust == target:
            outstanding += amount - paid
    rng.shuffle(payments)
    return BuiltTask(
        spec=None,  # type: ignore[arg-type]
        instruction=(
            f"What is the total outstanding (unpaid) balance for customer {target}, across all of its "
            "invoices in invoices.csv, after the payments in payments.csv? An invoice can have several "
            f"payments. {NUMBER_FORMAT}, rounded to 2 decimals."
        ),
        files={
            "invoices.csv": _csv(["invoice_id", "customer", "amount", "issued"], invoices),
            "payments.csv": _csv(["invoice_id", "amount", "paid_on"], payments),
        },
        answer_kind="number",
        expected=round(outstanding, 2),
        decimals=2,
        abs_tol=0.011,
    )


# ----------------------------------------------------------------------------- office


def _off_expenses(rng: random.Random) -> BuiltTask:
    depts = ["Legal", "Marketing", "Operations", "Research", "Sales"]
    cats = ["travel", "software", "catering", "hardware"]
    cat = rng.choice(cats)
    month = rng.randint(3, 9)
    rows, totals = [], {d: 0.0 for d in depts}
    for _ in range(60):
        d, c, m = (
            rng.choice(depts),
            rng.choice(cats),
            rng.choice([month, month, month - 1, month + 1]),
        )
        amt = round(rng.randint(20, 900) + rng.randint(0, 99) / 100, 2)
        rows.append([f"2026-{m:02d}-{rng.randint(1, 28):02d}", d, c, f"{amt:.2f}"])
        if c == cat and m == month:
            totals[d] += amt
    best = max(totals.values())
    if sum(1 for v in totals.values() if abs(v - best) < 1e-9) > 1 or best == 0:
        return _off_expenses(rng)
    winner = max(totals, key=totals.get)
    month_name = datetime(2026, month, 1).strftime("%B")
    return BuiltTask(
        spec=None,  # type: ignore[arg-type]
        instruction=(
            f"In expenses.csv, which department spent the most on {cat} in {month_name} 2026? "
            "Answer with the department name only."
        ),
        files={"expenses.csv": _csv(["date", "department", "category", "amount"], rows)},
        answer_kind="text",
        expected=winner,
    )


def _off_meetings(rng: random.Random) -> BuiltTask:
    people = ["ana", "ben", "chen", "dara", "eli"]
    who = rng.choice(people)
    day = f"2026-11-{rng.randint(2, 27):02d}"
    rows, spans = [], []
    for k in range(16):
        d = day if k < 11 else f"2026-11-{rng.randint(2, 27):02d}"
        start = rng.randint(16, 34) * 15  # minutes from midnight, 04:00 .. 08:30 offset below
        start += 300
        dur = rng.choice([15, 30, 45, 60, 90])
        attendees = rng.sample(people, rng.randint(2, 4))
        if k in (0, 1) and who not in attendees:
            attendees.append(who)
        rows.append(
            [
                d,
                f"{start // 60:02d}:{start % 60:02d}",
                f"{(start + dur) // 60:02d}:{(start + dur) % 60:02d}",
                f"Meeting {k + 1}",
                ";".join(attendees),
            ]
        )
        if d == day and who in attendees:
            spans.append((start, start + dur))
    spans.sort()
    total, cur_s, cur_e = 0, None, None
    for s, e in spans:
        if cur_e is None or s > cur_e:
            if cur_e is not None:
                total += cur_e - cur_s
            cur_s, cur_e = s, e
        else:
            cur_e = max(cur_e, e)
    if cur_e is not None:
        total += cur_e - cur_s
    return BuiltTask(
        spec=None,  # type: ignore[arg-type]
        instruction=(
            f"Using calendar.csv, how many minutes is {who} in meetings on {day}? Time covered by "
            "overlapping meetings counts once. Answer with an integer only."
        ),
        files={"calendar.csv": _csv(["date", "start", "end", "title", "attendees"], rows)},
        answer_kind="number",
        expected=total,
        decimals=0,
        abs_tol=0.0,
    )


def _off_reports(rng: random.Random) -> BuiltTask:
    names = rng.sample(
        [
            "Avery",
            "Blake",
            "Casey",
            "Devon",
            "Emery",
            "Finley",
            "Gray",
            "Harper",
            "Indigo",
            "Jules",
            "Kai",
            "Logan",
            "Morgan",
            "Noel",
            "Oakley",
            "Parker",
            "Quinn",
            "Reese",
        ],
        15,
    )
    managers: dict[str, str | None] = {names[0]: None}
    for k, n in enumerate(names[1:], start=1):
        managers[n] = names[rng.randint(0, min(k - 1, 6))]
    boss = rng.choice(names[1:5])
    under, frontier = set(), [boss]
    while frontier:
        m = frontier.pop()
        for n, mm in managers.items():
            if mm == m and n not in under:
                under.add(n)
                frontier.append(n)
    if len(under) < 2:
        return _off_reports(rng)
    rows = [
        [
            k + 100,
            n,
            rng.choice(["Ops", "Eng", "Fin"]),
            "" if managers[n] is None else names.index(managers[n]) + 100,
        ]
        for k, n in enumerate(names)
    ]
    rng.shuffle(rows)
    return BuiltTask(
        spec=None,  # type: ignore[arg-type]
        instruction=(
            f"employees.csv lists each employee's manager by employee id. List everyone who reports to {boss} "
            "directly or indirectly, as names sorted alphabetically and separated by commas."
        ),
        files={"employees.csv": _csv(["id", "name", "team", "manager_id"], rows)},
        answer_kind="list",
        expected=sorted(under),
    )


# ----------------------------------------------------------------------------- natural science


def _sci_gas(rng: random.Random) -> BuiltTask:
    p_kpa = rng.choice([98.6, 101.3, 120.0, 150.5, 202.6, 250.0])
    v_l = rng.choice([2.5, 5.0, 7.5, 10.0, 12.4, 22.4])
    t_c = rng.choice([-10.0, 0.0, 18.5, 25.0, 37.0, 60.0])
    n = (p_kpa * 1000) * (v_l / 1000) / (8.314 * (t_c + 273.15))
    notes = (
        "Lab notebook, sample B\n"
        f"Vessel volume: {v_l} L\n"
        f"Gauge reading converted to absolute pressure: {p_kpa} kPa\n"
        f"Gas temperature at reading: {t_c} degC\n"
        "Treat the gas as ideal. R = 8.314 J/(mol K).\n"
    )
    return BuiltTask(
        spec=None,  # type: ignore[arg-type]
        instruction=f"How many moles of gas are in the vessel described in lab_notes.txt? {NUMBER_FORMAT}, rounded to 3 decimals.",
        files={"lab_notes.txt": notes},
        answer_kind="number",
        expected=round(n, 3),
        decimals=3,
        rel_tol=0.003,
    )


_MASSES = {
    "H": 1.008,
    "C": 12.011,
    "N": 14.007,
    "O": 15.999,
    "Na": 22.990,
    "Mg": 24.305,
    "Al": 26.982,
    "S": 32.06,
    "Cl": 35.45,
    "K": 39.098,
    "Ca": 40.078,
    "Fe": 55.845,
    "Cu": 63.546,
    "P": 30.974,
}


def _formula_mass(formula: str) -> float:
    tokens = re.findall(r"[A-Z][a-z]?|\d+|\(|\)", formula)
    stack: list[dict[str, int]] = [{}]
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t == "(":
            stack.append({})
        elif t == ")":
            group = stack.pop()
            mult = 1
            if i + 1 < len(tokens) and tokens[i + 1].isdigit():
                mult = int(tokens[i + 1])
                i += 1
            for el, c in group.items():
                stack[-1][el] = stack[-1].get(el, 0) + c * mult
        else:
            mult = 1
            if i + 1 < len(tokens) and tokens[i + 1].isdigit():
                mult = int(tokens[i + 1])
                i += 1
            stack[-1][t] = stack[-1].get(t, 0) + mult
        i += 1
    return sum(_MASSES[el] * c for el, c in stack[0].items())


def _sci_molar(rng: random.Random) -> BuiltTask:
    formula = rng.choice(
        [
            "Ca(OH)2",
            "Al2(SO4)3",
            "Mg3(PO4)2",
            "Fe2(SO4)3",
            "Cu(NO3)2",
            "K3PO4",
            "Na2CO3",
            "(NH4)2SO4",
        ]
    )
    table = _csv(["symbol", "atomic_mass"], [[el, m] for el, m in sorted(_MASSES.items())])
    return BuiltTask(
        spec=None,  # type: ignore[arg-type]
        instruction=(
            f"Using the atomic masses in atomic_masses.csv, what is the molar mass of {formula} in g/mol? "
            f"{NUMBER_FORMAT}, rounded to 2 decimals."
        ),
        files={"atomic_masses.csv": table},
        answer_kind="number",
        expected=round(_formula_mass(formula), 2),
        decimals=2,
        abs_tol=0.011,
    )


def _sci_halflife(rng: random.Random) -> BuiltTask:
    half = rng.choice([1.7, 3.2, 5.0, 6.6, 8.1, 12.3])
    a0 = rng.randint(4000, 9000)
    rows = []
    for k in range(10):
        t = k * rng.choice([0.5, 1.0, 1.5])
        rows.append([t, round(a0 * 0.5 ** (t / half), 1)])
    rows = sorted({r[0]: r for r in rows}.values())
    return BuiltTask(
        spec=None,  # type: ignore[arg-type]
        instruction=(
            "decay.csv holds activity measurements of one isotope over time (hours). What is its half-life "
            f"in hours? {NUMBER_FORMAT}, rounded to 2 decimals."
        ),
        files={"decay.csv": _csv(["t_hours", "activity_bq"], rows)},
        answer_kind="number",
        expected=round(half, 2),
        decimals=2,
        rel_tol=0.01,
    )


# ----------------------------------------------------------------------------- math


def _math_divisible(rng: random.Random) -> BuiltTask:
    n = rng.randint(10_000, 900_000)
    a, b = rng.sample([3, 4, 6, 7, 9, 11, 13], 2)
    c = rng.choice([5, 8, 10, 15])
    count = sum(1 for k in range(1, n + 1) if (k % a == 0 or k % b == 0) and k % c != 0)
    params = f"N = {n}\na = {a}\nb = {b}\nc = {c}\n"
    return BuiltTask(
        spec=None,  # type: ignore[arg-type]
        instruction=(
            "With the values in params.txt, how many integers k with 1 <= k <= N are divisible by a or by b, "
            "but not by c? Answer with an integer only."
        ),
        files={"params.txt": params},
        answer_kind="number",
        expected=count,
        decimals=0,
    )


def _math_paths(rng: random.Random) -> BuiltTask:
    h, w = rng.randint(7, 10), rng.randint(7, 10)
    grid = [["." for _ in range(w)] for _ in range(h)]
    for _ in range(rng.randint(8, 16)):
        r, c = rng.randrange(h), rng.randrange(w)
        if (r, c) not in ((0, 0), (h - 1, w - 1)):
            grid[r][c] = "#"
    ways = [[0] * w for _ in range(h)]
    for r in range(h):
        for c in range(w):
            if grid[r][c] == "#":
                continue
            if r == 0 and c == 0:
                ways[r][c] = 1
                continue
            ways[r][c] = (ways[r - 1][c] if r else 0) + (ways[r][c - 1] if c else 0)
    if ways[h - 1][w - 1] == 0:
        return _math_paths(rng)
    return BuiltTask(
        spec=None,  # type: ignore[arg-type]
        instruction=(
            "grid.txt is a map where '.' is open and '#' is blocked. Moving only right or down one cell at a "
            "time, how many distinct paths go from the top-left cell to the bottom-right cell? Answer with an "
            "integer only."
        ),
        files={"grid.txt": "\n".join("".join(row) for row in grid) + "\n"},
        answer_kind="number",
        expected=ways[h - 1][w - 1],
        decimals=0,
    )


def _math_powersum(rng: random.Random) -> BuiltTask:
    n = rng.randint(2_000, 60_000)
    p = rng.randint(3, 9)
    m = rng.choice([1_000_003, 998_244_353, 65_537, 10_007])
    value = sum(pow(k, p, m) for k in range(1, n + 1)) % m
    return BuiltTask(
        spec=None,  # type: ignore[arg-type]
        instruction=(
            f"Compute (1^{p} + 2^{p} + ... + {n}^{p}) mod {m}. Answer with an integer only."
        ),
        files={
            "README.txt": "No input files for this task: everything you need is in the task text.\n"
        },
        answer_kind="number",
        expected=value,
        decimals=0,
    )


# ----------------------------------------------------------------------------- cybersecurity


def _ip(rng: random.Random) -> str:
    return f"10.{rng.randint(0, 255)}.{rng.randint(0, 255)}.{rng.randint(1, 254)}"


def _cyber_bruteforce(rng: random.Random) -> BuiltTask:
    ips = list({_ip(rng) for _ in range(8)})
    culprit = ips[0]
    threshold = rng.randint(4, 7)
    base = datetime(2026, 9, rng.randint(1, 28), 1, 0, 0)
    events: list[tuple[datetime, str]] = []
    t = base
    for ip in ips[1:]:
        for _ in range(rng.randint(1, threshold + 3)):
            t += timedelta(seconds=rng.randint(5, 300))
            events.append(
                (
                    t,
                    f"sshd[{rng.randint(100, 999)}]: Failed password for admin from {ip} port {rng.randint(30000, 60000)}",
                )
            )
    noisy = ips[1]
    t += timedelta(seconds=30)
    events.append(
        (t, f"sshd[{rng.randint(100, 999)}]: Accepted publickey for deploy from {noisy} port 40000")
    )
    for _ in range(threshold + rng.randint(0, 4)):
        t += timedelta(seconds=rng.randint(2, 20))
        events.append(
            (
                t,
                f"sshd[{rng.randint(100, 999)}]: Failed password for root from {culprit} port {rng.randint(30000, 60000)}",
            )
        )
    t += timedelta(seconds=11)
    events.append(
        (t, f"sshd[{rng.randint(100, 999)}]: Accepted password for root from {culprit} port 51234")
    )
    # every other IP stays under the threshold before any success it has
    fails: dict[str, int] = {}
    hits = set()
    for _, line in sorted(events):
        ip = line.rsplit(" from ", 1)[1].split()[0]
        if "Failed" in line:
            fails[ip] = fails.get(ip, 0) + 1
        elif fails.get(ip, 0) >= threshold:
            hits.add(ip)
    if hits != {culprit}:
        return _cyber_bruteforce(rng)
    log = (
        "\n".join(f"{ts.strftime('%Y-%m-%dT%H:%M:%S')} host1 {line}" for ts, line in sorted(events))
        + "\n"
    )
    return BuiltTask(
        spec=None,  # type: ignore[arg-type]
        instruction=(
            f"auth.log is an SSH log. Which source IP had at least {threshold} failed password attempts and "
            "then a successful login? Answer with the IP address only."
        ),
        files={"auth.log": log},
        answer_kind="text",
        expected=culprit,
    )


def _cyber_firewall(rng: random.Random) -> BuiltTask:
    rules = [
        ("deny", "tcp", 23, "0.0.0.0/0"),
        ("allow", "tcp", 22, "10.0.0.0/8"),
        ("deny", "tcp", 22, "0.0.0.0/0"),
        ("allow", "tcp", 443, "0.0.0.0/0"),
        ("allow", "tcp", 80, "192.168.0.0/16"),
        ("allow", "udp", 53, "10.1.0.0/16"),
        ("deny", "udp", 53, "0.0.0.0/0"),
        ("allow", "tcp", 5432, "10.2.3.0/24"),
    ]
    rng.shuffle(rules)
    rules.append(("deny", "any", 0, "0.0.0.0/0"))

    def in_cidr(ip: str, cidr: str) -> bool:
        net, bits = cidr.split("/")
        to_int = lambda s: int.from_bytes(bytes(int(x) for x in s.split(".")), "big")  # noqa: E731
        mask = (0xFFFFFFFF << (32 - int(bits))) & 0xFFFFFFFF if int(bits) else 0
        return to_int(ip) & mask == to_int(net) & mask

    def decide(proto: str, port: int, src: str) -> str:
        for act, p, prt, cidr in rules:
            if (p == "any" or p == proto) and (prt == 0 or prt == port) and in_cidr(src, cidr):
                return act
        return "deny"

    queries, allowed = [], 0
    for _ in range(14):
        proto = rng.choice(["tcp", "tcp", "udp"])
        port = rng.choice([22, 23, 53, 80, 443, 5432, 8080])
        src = rng.choice(
            [
                f"10.1.{rng.randint(0, 9)}.{rng.randint(1, 254)}",
                f"10.2.3.{rng.randint(1, 254)}",
                f"192.168.{rng.randint(0, 9)}.{rng.randint(1, 254)}",
                f"172.16.0.{rng.randint(1, 254)}",
            ]
        )
        queries.append([proto, port, src])
        allowed += decide(proto, port, src) == "allow"
    rule_text = (
        "# first matching rule wins; port 0 means any port\n"
        + "\n".join(f"{a} {p} port={prt} src={c}" for a, p, prt, c in rules)
        + "\n"
    )
    return BuiltTask(
        spec=None,  # type: ignore[arg-type]
        instruction=(
            "firewall.rules is an ordered rule list (first match wins). How many of the connection attempts in "
            "connections.csv does it allow? Answer with an integer only."
        ),
        files={
            "firewall.rules": rule_text,
            "connections.csv": _csv(["proto", "dst_port", "src_ip"], queries),
        },
        answer_kind="number",
        expected=allowed,
        decimals=0,
    )


def _cyber_ioc(rng: random.Random) -> BuiltTask:
    files, names = {}, []
    for k in range(9):
        name = f"samples/{rng.choice(['report', 'invoice', 'setup', 'update', 'notes'])}_{k}.{rng.choice(['txt', 'js', 'ps1', 'cfg'])}"
        body = "".join(
            rng.choice("abcdefghijklmnopqrstuvwxyz0123456789 \n")
            for _ in range(rng.randint(80, 300))
        )
        files[name] = body
        names.append(name)
    bad = rng.sample(names, rng.randint(2, 3))
    iocs = [hashlib.sha256(files[n].encode()).hexdigest() for n in bad]
    iocs += [hashlib.sha256(str(rng.random()).encode()).hexdigest() for _ in range(4)]
    rng.shuffle(iocs)
    files["ioc_sha256.txt"] = "# known-bad SHA-256 digests\n" + "\n".join(iocs) + "\n"
    return BuiltTask(
        spec=None,  # type: ignore[arg-type]
        instruction=(
            "Which files under samples/ have a SHA-256 digest listed in ioc_sha256.txt? Answer with their "
            "paths as shown by list_files (for example samples/a.txt), sorted alphabetically and separated by commas."
        ),
        files=files,
        answer_kind="list",
        expected=sorted(bad),
    )


# ----------------------------------------------------------------------------- media


def _ts(sec: float) -> str:
    ms = int(round(sec * 1000))
    return f"{ms // 3_600_000:02d}:{ms // 60_000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def _media_srt(rng: random.Random) -> BuiltTask:
    speakers = ["MAYA", "OREN", "PIA"]
    who = rng.choice(speakers)
    t, blocks, total = 1.0, [], 0.0
    for k in range(18):
        dur = round(rng.uniform(0.8, 4.5), 3)
        sp = rng.choice(speakers)
        blocks.append(f"{k + 1}\n{_ts(t)} --> {_ts(t + dur)}\n[{sp}] line {k + 1}\n")
        if sp == who:
            total += round(dur * 1000) / 1000
        t += dur + round(rng.uniform(0.1, 1.5), 3)
    return BuiltTask(
        spec=None,  # type: ignore[arg-type]
        instruction=(
            f"subtitles.srt tags each cue with its speaker. For how many seconds in total are {who}'s cues on "
            f"screen? {NUMBER_FORMAT}, rounded to 2 decimals."
        ),
        files={"subtitles.srt": "\n".join(blocks)},
        answer_kind="number",
        expected=round(total, 2),
        decimals=2,
        abs_tol=0.011,
    )


def _media_edl(rng: random.Random) -> BuiltTask:
    fps = rng.choice([24, 25, 30])
    rows, total_frames = [], 0
    for k in range(rng.randint(5, 8)):
        start = rng.randint(0, 400) * fps + rng.randint(0, fps - 1)
        length = rng.randint(2, 20) * fps + rng.randint(0, fps - 1)
        xfade = 0 if k == 0 else rng.choice([0, 0, 6, 12])
        rows.append([f"clip{k + 1}", start, start + length, xfade])
        total_frames += length - xfade
    return BuiltTask(
        spec=None,  # type: ignore[arg-type]
        instruction=(
            f"edit_list.csv lists clips in timeline order, with in/out points in frames at {fps} fps (out is "
            "exclusive). Each clip overlaps the previous one by its crossfade_frames. What is the finished "
            f"program's running time in seconds? {NUMBER_FORMAT}, rounded to 2 decimals."
        ),
        files={"edit_list.csv": _csv(["clip", "in_frame", "out_frame", "crossfade_frames"], rows)},
        answer_kind="number",
        expected=round(total_frames / fps, 2),
        decimals=2,
        abs_tol=0.011,
    )


def _media_headline(rng: random.Random) -> BuiltTask:
    topic = rng.choice(
        [
            ("solar", "farm", "village", ["cheap", "miracle"]),
            ("river", "cleanup", "volunteers", ["shocking", "disaster"]),
            ("library", "robot", "students", ["amazing", "epic"]),
            ("bakery", "bread", "night", ["best", "secret"]),
        ]
    )
    w1, w2, w3, banned = topic
    max_len = rng.choice([48, 55, 60])
    brief = (
        "Headline brief\n"
        f"- At most {max_len} characters, counting spaces.\n"
        f"- Must contain the words '{w1}', '{w2}' and '{w3}' (any letter case).\n"
        f"- Must not contain the words: {', '.join(banned)}.\n"
        "- No exclamation marks, and no final period.\n"
        "- Sentence case: only the first letter of the headline is a capital letter.\n"
    )

    def has(word: str) -> Callable[[str], bool]:
        return lambda s: re.search(rf"\b{re.escape(word)}\b", s, re.I) is not None

    constraints: list[tuple[str, Callable[[str], bool]]] = [
        ("length", lambda s: 0 < len(s) <= max_len),
        (w1, has(w1)),
        (w2, has(w2)),
        (w3, has(w3)),
        ("banned", lambda s: not any(re.search(rf"\b{b}\b", s, re.I) for b in banned)),
        ("punctuation", lambda s: "!" not in s and not s.endswith(".")),
        ("case", lambda s: len(s) > 0 and s[0].isupper() and not any(ch.isupper() for ch in s[1:])),
    ]
    return BuiltTask(
        spec=None,  # type: ignore[arg-type]
        instruction="Write one headline that meets every rule in brief.txt. Submit the headline text only.",
        files={"brief.txt": brief},
        answer_kind="constraints",
        constraints=constraints,
    )


# ----------------------------------------------------------------------------- industrial


def _ind_sensor(rng: random.Random) -> BuiltTask:
    threshold = rng.choice([78.0, 80.0, 82.5, 85.0])
    k_needed = rng.choice([3, 4])
    t0 = datetime(2026, 10, rng.randint(1, 28), 6, 0, 0)
    temps, rows = [], []
    level = threshold - rng.uniform(6, 10)
    for _ in range(48):
        level += rng.uniform(-1.2, 1.5)
        temps.append(round(level, 1))
    # plant a short excursion (k-1 readings) and a qualifying run later
    a = rng.randint(8, 16)
    for j in range(k_needed - 1):
        temps[a + j] = round(threshold + rng.uniform(0.2, 2.0), 1)
    temps[a + k_needed - 1] = round(threshold - 0.5, 1)
    b = rng.randint(24, 40)
    for j in range(k_needed + 1):
        temps[b + j] = round(threshold + rng.uniform(0.3, 3.0), 1)
    answer = None
    run = 0
    for i, v in enumerate(temps):
        run = run + 1 if v > threshold else 0
        if run == k_needed:
            answer = i - k_needed + 1
            break
    if answer is None:
        return _ind_sensor(rng)
    for i, v in enumerate(temps):
        rows.append([(t0 + timedelta(minutes=5 * i)).strftime("%Y-%m-%dT%H:%M"), v])
    return BuiltTask(
        spec=None,  # type: ignore[arg-type]
        instruction=(
            f"sensor.csv holds bearing temperatures every 5 minutes. An alarm starts at the first reading of the "
            f"first run of {k_needed} consecutive readings strictly above {threshold} degC. Answer with that "
            "reading's timestamp exactly as written in the file."
        ),
        files={"sensor.csv": _csv(["timestamp", "temp_c"], rows)},
        answer_kind="text",
        expected=rows[answer][0],
    )


def _ind_oee(rng: random.Random) -> BuiltTask:
    rows, target = [], None
    machines = rng.sample(["M1", "M2", "M3", "M4", "M5"], 4)
    for m in machines:
        planned = rng.choice([420, 450, 480])
        down = rng.randint(10, 90)
        cycle = rng.choice([20, 24, 30, 36, 45])
        run_s = (planned - down) * 60
        total = int(run_s / cycle * rng.uniform(0.7, 0.97))
        rejects = rng.randint(0, max(1, total // 20))
        rows.append([m, planned, down, cycle, total, rejects])
    target = rows[rng.randrange(len(rows))]
    m, planned, down, cycle, total, rejects = target
    availability = (planned - down) / planned
    performance = cycle * total / ((planned - down) * 60)
    quality = (total - rejects) / total
    oee = availability * performance * quality * 100
    return BuiltTask(
        spec=None,  # type: ignore[arg-type]
        instruction=(
            f"Using shift_log.csv, what is the OEE (availability x performance x quality) of machine {m} for the "
            f"shift, as a percentage? {NUMBER_FORMAT}, rounded to 1 decimal."
        ),
        files={
            "shift_log.csv": _csv(
                [
                    "machine",
                    "planned_min",
                    "downtime_min",
                    "ideal_cycle_s",
                    "total_count",
                    "reject_count",
                ],
                rows,
            ),
            "definitions.txt": (
                "availability = run time / planned time, where run time = planned - downtime\n"
                "performance = ideal cycle time x total count / run time\n"
                "quality = good count / total count, where good = total - rejects\n"
            ),
        },
        answer_kind="number",
        expected=round(oee, 1),
        decimals=1,
        abs_tol=0.051,
    )


def _ind_line(rng: random.Random) -> BuiltTask:
    rows, rates = [], []
    for k in range(rng.randint(5, 7)):
        cycle = rng.randint(20, 95)
        units = rng.choice([1, 1, 2, 3])
        avail = rng.choice([1.0, 0.95, 0.9, 0.85])
        rows.append([f"S{k + 1}", cycle, units, avail])
        rates.append(3600 / cycle * units * avail)
    return BuiltTask(
        spec=None,  # type: ignore[arg-type]
        instruction=(
            "stations.csv describes a serial production line: cycle time per part, identical parallel units, and "
            "availability (fraction of time up). Parts flow through every station. What is the line's maximum "
            "throughput in parts per hour, rounded down to a whole number? Answer with an integer only."
        ),
        files={
            "stations.csv": _csv(
                ["station", "cycle_time_s", "parallel_units", "availability"], rows
            )
        },
        answer_kind="number",
        expected=math.floor(min(rates) + 1e-9),
        decimals=0,
    )


# ----------------------------------------------------------------------------- software engineering

_SWE = {
    "merge_intervals": (
        '''def merge_intervals(intervals):
    """Merge overlapping or touching [start, end] intervals; return them sorted by start."""
    result = []
    for start, end in intervals:
        if result and start < result[-1][1]:
            result[-1][1] = max(result[-1][1], end)
        else:
            result.append([start, end])
    return result
''',
        [
            ((([1, 3], [2, 6], [8, 10]),), [[1, 6], [8, 10]]),
            ((([5, 7], [1, 2]),), [[1, 2], [5, 7]]),
        ],
        [
            ((([1, 3], [2, 6], [8, 10]),), [[1, 6], [8, 10]]),
            ((([5, 7], [1, 2]),), [[1, 2], [5, 7]]),
            ((([1, 4], [4, 5]),), [[1, 5]]),
            ((([6, 8], [1, 9], [2, 4]),), [[1, 9]]),
            (((),), []),
            ((([3, 3],),), [[3, 3]]),
            ((([1, 2], [3, 4], [2, 3]),), [[1, 4]]),
        ],
    ),
    "parse_duration": (
        '''def parse_duration(text):
    """Parse durations like '1h30m', '45s', '2h5s' or '90m' into total seconds."""
    import re
    total = 0
    for value, unit in re.findall(r"(\\d+)([hm])", text):
        total += int(value) * {"h": 3600, "m": 60}[unit]
    return total
''',
        [(("1h30m",), 5400), (("90m",), 5400)],
        [
            (("1h30m",), 5400),
            (("90m",), 5400),
            (("45s",), 45),
            (("2h5s",), 7205),
            (("1h1m1s",), 3661),
            (("0s",), 0),
            (("10m30s",), 630),
        ],
    ),
    "rolling_mean": (
        '''def rolling_mean(values, window):
    """Mean of each full window of `window` consecutive values, in order."""
    out = []
    for i in range(len(values) - window):
        chunk = values[i:i + window]
        out.append(sum(chunk) / window)
    return out
''',
        [(([1, 2, 3, 4], 2), [1.5, 2.5, 3.5])],
        [
            (([1, 2, 3, 4], 2), [1.5, 2.5, 3.5]),
            (([5], 1), [5.0]),
            (([1, 2, 3], 3), [2.0]),
            (([1, 2], 3), []),
            (([2, 4, 6, 8, 10], 2), [3.0, 5.0, 7.0, 9.0]),
            (([0, 0, 3], 1), [0.0, 0.0, 3.0]),
        ],
    ),
    "slugify": (
        '''def slugify(title):
    """Lowercase ASCII slug: words joined by single hyphens, no leading or trailing hyphen."""
    import re
    slug = title.lower()
    slug = re.sub(r"[^a-z0-9]", "-", slug)
    return slug
''',
        [(("Hello World",), "hello-world")],
        [
            (("Hello World",), "hello-world"),
            (("  Rust & Python: 2026! ",), "rust-python-2026"),
            (("a--b",), "a-b"),
            (("Already-slug",), "already-slug"),
            (("!!!",), ""),
            (("Mixed   Spaces Here",), "mixed-spaces-here"),
        ],
    ),
    "top_words": (
        '''def top_words(text, k):
    """The k most frequent lowercase words; ties broken alphabetically. Words are runs of letters."""
    import re
    from collections import Counter
    counts = Counter(re.findall(r"[a-z]+", text))
    return [w for w, _ in counts.most_common(k)]
''',
        [(("b a b c", 1), ["b"])],
        [
            (("b a b c", 1), ["b"]),
            (("The cat and the hat", 1), ["the"]),
            (("c b a", 2), ["a", "b"]),
            (("x y x y z", 2), ["x", "y"]),
            (("Dog dog CAT cat cat bird", 2), ["cat", "dog"]),
            (("", 3), []),
        ],
    ),
}


def _swe(name: str) -> Callable[[random.Random], BuiltTask]:
    source, visible, hidden = _SWE[name]

    def build(rng: random.Random) -> BuiltTask:
        tests = ["from solution import " + name, "", "CASES = ["]
        for args, want in visible:
            tests.append(f"    ({args!r}, {want!r}),")
        tests += [
            "]",
            "",
            "failed = 0",
            "for args, want in CASES:",
            f"    got = {name}(*args)",
            "    status = 'ok' if got == want else 'FAIL'",
            "    failed += status == 'FAIL'",
            f"    print(status, '{name}', args, '->', repr(got), '(expected', repr(want) + ')')",
            "print('passed', len(CASES) - failed, 'of', len(CASES))",
            "",
        ]
        return BuiltTask(
            spec=None,  # type: ignore[arg-type]
            instruction=(
                f"solution.py has a bug in {name}: it does not do what its docstring says. Fix it with write_file "
                "(keep the function name and signature). run_tests runs the examples in test_solution.py; the "
                "grader also runs further cases from the docstring's behaviour. Submit when you are done; the "
                "answer text is ignored for this task."
            ),
            files={"solution.py": source, "test_solution.py": "\n".join(tests)},
            answer_kind="code",
            expected=name,
            code_cases=[(name, args, want) for args, want in hidden],
        )

    return build


# ----------------------------------------------------------------------------- catalog

_TEMPLATES: dict[str, list[tuple[str, Callable[[random.Random], BuiltTask]]]] = {
    "finance": [("npv", _fin_npv), ("loan", _fin_loan), ("outstanding", _fin_outstanding)],
    "office": [("expenses", _off_expenses), ("meetings", _off_meetings), ("reports", _off_reports)],
    "science": [("gas", _sci_gas), ("molar", _sci_molar), ("halflife", _sci_halflife)],
    "math": [("divisible", _math_divisible), ("paths", _math_paths), ("powersum", _math_powersum)],
    "security": [
        ("bruteforce", _cyber_bruteforce),
        ("firewall", _cyber_firewall),
        ("ioc", _cyber_ioc),
    ],
    "media": [("subtitles", _media_srt), ("edit", _media_edl), ("headline", _media_headline)],
    "industrial": [("sensor", _ind_sensor), ("oee", _ind_oee), ("line", _ind_line)],
    "software": [(n, _swe(n)) for n in _SWE],
}


def _catalog() -> list[TaskSpec]:
    specs = []
    for domain, templates in _TEMPLATES.items():
        for k in range(5):
            template = templates[k % len(templates)][0]
            task_id = f"{domain}-{template}-{k + 1}"
            specs.append(
                TaskSpec(task_id=task_id, domain=domain, template=template, seed=_seed(task_id))
            )
    return specs


TASKS: dict[str, TaskSpec] = {s.task_id: s for s in _catalog()}


def build_task(task_id: str) -> BuiltTask:
    spec = TASKS[task_id]
    builder = dict(_TEMPLATES[spec.domain])[spec.template]
    # A seeded generator makes every reset of a task identical; nothing here is secret.
    task = builder(random.Random(spec.seed))  # noqa: S311
    task.spec = spec
    return task


# ----------------------------------------------------------------------------- scoring

_NUMBER_RE = re.compile(r"[-+]?\d+(?:\.\d+)?")


def _score_number(task: BuiltTask, answer: str) -> tuple[float, str]:
    text = answer.strip()
    if _NUMBER_RE.fullmatch(text):
        value, exact_format = float(text), True
    else:
        found = _NUMBER_RE.findall(text.replace(",", ""))
        if len(found) != 1:
            return 0.0, "the answer is not a single number"
        value, exact_format = float(found[0]), False
    tol = max(task.abs_tol, task.rel_tol * abs(float(task.expected)), 1e-9)
    if abs(value - float(task.expected)) > tol:
        return 0.0, "the value is wrong"
    if not exact_format:
        return 0.5, "the value is right but the answer has extra characters"
    return 1.0, "correct"


def _score_text(task: BuiltTask, answer: str) -> tuple[float, str]:
    text = answer.strip().strip("'\"`").strip()
    want = str(task.expected)
    if text.lower() == want.lower():
        return 1.0, "correct"
    if re.search(rf"(?<![\w.:-]){re.escape(want)}(?![\w.:-])", text, re.I):
        return 0.5, "the right answer is there, with extra text"
    return 0.0, "the answer is wrong"


def _score_list(task: BuiltTask, answer: str) -> tuple[float, str]:
    got = [p.strip().strip("'\"`") for p in answer.split(",") if p.strip()]
    want = list(task.expected)
    if got == want:
        return 1.0, "correct"
    if set(got) == set(want):
        return 0.8, "right items, wrong order or duplicates"
    inter, union = len(set(got) & set(want)), len(set(got) | set(want))
    return (0.5 * inter / union if union else 0.0), "some items are wrong or missing"


_FREE_RULES = frozenset({"length", "banned", "punctuation", "case"})


def _score_constraints(task: BuiltTask, answer: str) -> tuple[float, str]:
    text = answer.strip()
    failed = [name for name, ok in task.constraints if not ok(text)]
    if not failed:
        return 1.0, "every rule is met"
    # Rules such as "no exclamation mark" hold for almost any text, so they earn credit only once
    # the headline contains every required word.
    required = [name for name, _ in task.constraints if name not in _FREE_RULES]
    missing = [name for name in required if name in failed]
    if missing:
        return 0.2 * (len(required) - len(missing)) / len(
            required
        ), f"{len(failed)} rule(s) not met"
    return 0.6 * (len(task.constraints) - len(failed)) / len(
        task.constraints
    ), f"{len(failed)} rule(s) not met"


def score_answer(
    task: BuiltTask, answer: str, code_runner: Callable[[BuiltTask], float] | None = None
) -> tuple[float, str]:
    """Correctness in [0, 1] and a short reason. Code tasks are scored by ``code_runner``."""
    if task.answer_kind == "code":
        if code_runner is None:
            return 0.0, "no code runner"
        frac = code_runner(task)
        return frac, f"{round(frac * 100)}% of the grader's cases pass"
    if not isinstance(answer, str) or not answer.strip():
        return 0.0, "empty answer"
    if task.answer_kind == "number":
        return _score_number(task, answer)
    if task.answer_kind == "text":
        return _score_text(task, answer)
    if task.answer_kind == "list":
        return _score_list(task, answer)
    if task.answer_kind == "constraints":
        return _score_constraints(task, answer)
    raise ValueError(task.answer_kind)
