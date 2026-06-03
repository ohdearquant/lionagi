"""Small-sample statistics for binary agentic metrics.

Agentic eval is high-variance: single-run pass@1 swings 2-6pp even at temp 0
(arxiv 2602.07150), and reported 2-3pp "gains" are routinely noise. So every
proportion here ships with a 95% confidence interval, and the report refuses to
rank two configs whose intervals overlap.

We use the **Wilson score interval** for a binomial proportion: dependency-free,
correct at small N (unlike the normal approximation, which gives [0,0] at k=N and
nonsense near the boundaries), and the standard recommendation for exactly this
regime. For ranking stability at small N the literature also favors a Beta
posterior ("Don't Pass@k", arxiv 2510.04265); Wilson's interval coincides with
the Beta/Jeffreys credible interval closely enough for our decision rule
(non-overlap ⇒ a real difference).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

_Z95 = 1.959963984540054  # standard normal 0.975 quantile


@dataclass(frozen=True, slots=True)
class Proportion:
    k: int  # successes
    n: int  # trials
    lo: float  # 95% CI lower
    hi: float  # 95% CI upper

    @property
    def p(self) -> float:
        return self.k / self.n if self.n else float("nan")

    def fmt(self) -> str:
        if not self.n:
            return "N/A"
        return f"{self.p:.2f} [{self.lo:.2f},{self.hi:.2f}]"


def wilson(k: int, n: int, z: float = _Z95) -> Proportion:
    """Wilson score 95% CI for k successes in n Bernoulli trials."""
    if n == 0:
        return Proportion(0, 0, float("nan"), float("nan"))
    p = k / n
    z2 = z * z
    denom = 1 + z2 / n
    center = (p + z2 / (2 * n)) / denom
    half = (z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n))) / denom
    return Proportion(k, n, max(0.0, center - half), min(1.0, center + half))


def disjoint(a: Proportion, b: Proportion) -> bool:
    """True if the two 95% CIs do not overlap — the bar for claiming a real
    difference. Overlapping intervals ⇒ 'not distinguishable at this N'."""
    if not a.n or not b.n:
        return False
    return a.hi < b.lo or b.hi < a.lo


def f1(precision: float, recall: float) -> float:
    if precision + recall == 0:
        return 0.0
    return 2 * precision * recall / (precision + recall)
