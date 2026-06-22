## Install the studio backend

Den renders a small local backend that runs on `localhost`. Install it once:

```bash
pip install 'lionagi[studio]'
```

If your workspace already uses a `.venv` or a `uv` project, Den auto-detects it and installs the backend on first run, so you can skip this step.

Nothing leaves your machine. Den only reads from the local backend over `127.0.0.1`.
