# jobs.py internals reference

Design rationale too long to carry as inline docstrings in `lionagi/mcp/jobs.py`.
See `docs/internals/mcp.md` for the run-lifecycle, locking, and kill-safety
contracts; this file holds the remaining essay-length notes.

## reservation-rollback-contract

`_discard_reservation` gives a reserved run directory back, along with what a
submission put in it.

A submission that fails partway through writing has already left files
behind, so removing only an empty directory would give the reservation back
for some failures and not others. The files a submission writes into its own
reservation are named in `_RESERVATION_CONTENTS`, and only those: they are
addressed as fixed names under the reservation directory, never through a
path a caller handed in. A caller may name an MCP config that lives anywhere
at all, and that file is theirs — it is not part of this reservation
whatever it points at, and nothing here can be talked into deleting it.

`rmdir` refuses a directory with anything in it, and that refusal stays the
safety here rather than becoming a check taken beforehand: whatever this is
asked to remove, a directory holding a run's state survives it — anything
not on the short list above stops the removal. A removal that fails for any
other reason leaves a directory nobody claimed, which is worth less than the
error that sent us here.

The function returns whether the directory is actually gone afterward. When
it is not — the one case it suppresses rather than raises for — a marker is
left in what remains of it, so a directory found later under the jobs root
with no job record reads as a giveback that could not run rather than as one
that succeeded; both leave the same absence of a job otherwise, and nothing
else tells them apart.

`_discard_reservation_and_warn` wraps this: its own marker only helps an
operator who later goes looking under the jobs root, so every caller that
discards a reservation on an error path must act on the boolean immediately
rather than let a `False` disappear along with the exception it rode in on.
The boolean says nothing about whether the marker write itself landed — that
write is best-effort and suppresses its own `OSError` — so the warn wrapper
checks the marker's actual presence afterward rather than assuming it from
the directory surviving, so an operator reading the warning is never sent
looking for a file that was never written.

## reap-notice-delivery-ownership

`_deliver_reap_notice` attempts the terminal notice a reaped run's own
process never got to send.

The dead child was the owner of both the end and its delivery, so an
observer that publishes the end and stops there leaves a notice-only caller
asleep forever — the terminality would be repaired and the wake-up would
not. The winner of the transition therefore attempts the same configured
delivery the hook would have, through the hook's own resolution, so a
per-run override and the project/global settings mean here exactly what they
mean there — there is only one place a notifier is configured.

Delivery is best-effort and after the fact. The end is already durable when
this runs, so nothing here can change how the run came out: a refusal, a
non-zero exit, or a timeout is recorded as a delivery failure. Before
delivery starts, a write-ahead outcome records that the attempt's result is
unknown, so a crash before the final result remains machine-readable.

The exception guard is total for the same reason: this is called from a read
path, and a notifier that comes apart in a way the hook does not classify
must not turn a status read of an already-ended run into a failed call. What
can be lost is the final delivery result, while the attempted state stays
durable.
