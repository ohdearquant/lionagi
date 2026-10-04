"""`lion`: the command line of lion1, one entry with subcommands.

    lion chat [-c | -r <id>] [-v]                  # talk with an actor here; the record is the memory
    lion config [--dir <dir>]                       # chat's defaults: ~/.lion, <dir>/.lion config.toml
    lion agent --dir <agent dir>                    # serve an actor's long-running agent until interrupted
    lion agent --dir <agent dir> --once [--wait 30] # one wake, the record printed
    lion agent --dir <agent dir> --check disk       # one instrument, printed and landed, no mail
    lion agent --dir <agent dir> --ticks            # create the missing reminders only
    lion agent --dir <agent dir> --arm              # landing arm: identity + boundary, landed
    lion agent --dir <agent dir> --stats            # the day's tokens, wakes, mail and idle polls

The runtime is `lion1`, the tools and guidance are `hub`; this package only composes them and
talks to a terminal.
"""

from .main import main, parser

__all__ = ("main", "parser")
