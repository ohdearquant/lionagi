# `lionagi/cli/_runs.py` internals

## Why `_reopen_session_for_resume` reopens instead of just re-closing

A session's closing transition only announces itself when the status
actually changes. A resume adopts a session an earlier leg already took
terminal, so writing that same terminal status at the end is not a change:
the leg finishes without announcing anything, the completion notice never
arrives, and the job record never closes. Reopening first restores the
invariant the rest of the system reads off this column, which is that a
session marked terminal is not currently executing.

Reopening is the only sanctioned exit from a terminal status, so it carries
an override. That is not a formality to satisfy the guard: it is what makes
each reopening attributable. Declaring a terminal-to-running edge in the
session policy would have satisfied the guard too, and would have permitted
terminal exit for every writer in the system, while finality is exactly what
the reapers, the teardown guard and `li wait` all rest on.
