### Added

- Leg-host mode for the Studio daemon (`LIONAGI_STUDIO_LEG_HOST=1`): it runs Claude Code agent legs for a caller on another machine, each in a detached worktree checked out at a full commit the caller names, with an allow-listed environment, no MCP servers and the child sandbox enforced, and keeps each leg's final output. Every other write route answers 403. See the remote legs guide.
- `li remote {run,status,output,cancel}`: run an agent leg on a leg host named in `~/.lionagi/remotes.yaml`, whose token is read from its own `LIONAGI_REMOTE_*` variable and sent only to that host.

### Security

- `~/.secrets` is on the child sandbox's default read-deny list.
