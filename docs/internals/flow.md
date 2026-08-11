# Flow op-budget scheduling bound

`critical_path_depth` and `max_sequential_depth` in
`lionagi/cli/orchestrate/flow.py` compute the divisor used to split a
flow's total time budget across ops.

## critical_path_depth

Longest dependency chain in the plan, in ops. This is how many ops the
*dependencies* force to run one after another, which is not the op count:
ops with no path between them are free to run at the same time.

It is a lower bound on the flow's wall clock, not the bound. A concurrency
cap serializes ops this function calls parallel, and under `--max-concurrent
1` it says 1 for a plan that runs strictly in sequence.

What actually sizes a budget is `max_sequential_depth`, which takes this
together with what the cap forces. This function is exact only when nothing
caps concurrency, which is the common case and why it is still worth
computing directly.

## max_sequential_depth

The most ops that can end up running one after another. This number divides
the flow's budget, so its error has a direction: counting too few hands
every op more time than the flow can afford and the flow overruns its
deadline, while counting too many only means an op is told it has slightly
less time than it might have had. That asymmetry is what makes this an
upper bound rather than an estimate.

It has to be a bound, because the quantity it describes depends on how long
each op runs and a budget is computed before any of them have. An earlier
version simulated the schedule directly — a queue of ready ops, admitting
`max_concurrent` at a time, counting passes. That models one schedule, the
one where every op takes about as long as every other, and the executor
runs a great many. Give four ops a cap of two and let the second one run
six times longer than the rest, and the other three serialize behind it in
a chain of three where the simulation counted two. Unequal durations are
the normal case, not the corner, so the equal-duration schedule is the
wrong thing to be exact about.

Two things force ops into sequence and the bound is the worse of them.
Dependencies force a chain that no amount of capacity can shorten. Capacity
forces the rest: any run of ops executing strictly one after another can
contain at most one op from a set that started together, so it is bounded
by one plus however many ops are not in that first admitted batch. Neither
can exceed the everything-serializes case of one op at a time.

Unbounded capacity (`conc >= num_ops`) admits every ready op, so each pass
clears one level of the dependency graph and the pass count is exactly the
longest chain — this is the common case and is taken directly, as one
linear scan. Otherwise the bound is the worse of the two forcing factors
above: `max(critical_path_depth(dep_indices), num_ops - conc + 1)`, capped
at `num_ops`, the everything-serializes case.
