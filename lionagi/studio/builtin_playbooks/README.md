# Built-in playbook templates (bundled)

These files are byte-identical copies of `examples/playbooks/*.playbook.yaml`,
duplicated here so they ship inside the installed `lionagi` wheel (see the
`artifacts` entry in `pyproject.toml`). `examples/playbooks/` lives outside the
`lionagi/` package tree and is not packaged, so the Studio backend cannot read
it at runtime on a real (non-editable, no repo checkout) install — this
directory is the one Studio's `/api/playbooks/builtin/` endpoints actually
read from.

`tests/apps_studio_server/test_playbooks_builtin_api.py` asserts this
directory's contents match `examples/playbooks/` byte-for-byte, so any edit to
one without the other fails CI. Keep them in sync.
