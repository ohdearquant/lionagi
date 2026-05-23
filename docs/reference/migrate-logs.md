# Migrating File-Based Logs to SQLite

lionagi v0.23+ stores all session data in `~/.lionagi/state.db` (SQLite). Older versions wrote branch snapshots to `~/.lionagi/runs/` and `~/.lionagi/logs/agents/`. This guide migrates those files into the database and reclaims disk space.

!!! warning "Back up first"
    Copy `~/.lionagi/state.db` before running any migration script. If something goes wrong, restore the backup.

## Check what you have

```bash
# Filesystem size
du -sh ~/.lionagi/runs/ ~/.lionagi/logs/ 2>/dev/null

# Database size
ls -lh ~/.lionagi/state.db
```

## Step 1: Migrate `~/.lionagi/runs/`

Each run directory may contain `branches/*.json` (branch snapshots) and `run.json` (manifest). The script below migrates any branches not already in the database.

```python
"""Migrate ~/.lionagi/runs/ branches into state.db."""
import asyncio
import json
import uuid
from pathlib import Path

from lionagi.state.db import StateDB


async def migrate_runs():
    db = StateDB()
    await db.open()

    runs_root = Path("~/.lionagi/runs").expanduser()
    if not runs_root.exists():
        print("No runs/ directory found — nothing to migrate.")
        await db.close()
        return

    # Collect existing IDs
    conn = db.db
    existing_branches = set()
    async with conn.execute("SELECT id FROM branches") as cur:
        async for row in cur:
            existing_branches.add(row[0])

    existing_sessions = set()
    async with conn.execute("SELECT id FROM sessions") as cur:
        async for row in cur:
            existing_sessions.add(row[0])

    migrated = 0
    for rd in sorted(runs_root.iterdir()):
        if not rd.is_dir():
            continue
        bd = rd / "branches"
        if not bd.exists():
            continue
        for bf in bd.glob("*.json"):
            bid = bf.stem
            if bid in existing_branches:
                continue
            try:
                data = json.loads(bf.read_text())
            except (OSError, json.JSONDecodeError):
                continue

            created_at = data.get("created_at", 0)
            name = data.get("name", bid[:8])
            node_meta = data.get("metadata", {})
            chat_model = data.get("chat_model")
            if chat_model:
                node_meta["chat_model"] = chat_model

            # Create a session for this branch
            session_id = str(uuid.uuid4())
            prog_s = str(uuid.uuid4())
            prog_b = str(uuid.uuid4())

            await conn.execute(
                "INSERT OR IGNORE INTO progressions (id, collection) VALUES (?, ?)",
                (prog_s, "[]"),
            )
            await conn.execute(
                "INSERT OR IGNORE INTO progressions (id, collection) VALUES (?, ?)",
                (prog_b, "[]"),
            )
            await conn.execute(
                """INSERT INTO sessions
                   (id, created_at, updated_at, node_metadata, name,
                    progression_id, status, started_at, ended_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'completed', ?, ?)""",
                (session_id, created_at, created_at, json.dumps(node_meta),
                 name, prog_s, created_at, created_at),
            )
            await conn.execute(
                """INSERT INTO branches
                   (id, created_at, node_metadata, session_id, name, progression_id)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (bid, created_at, json.dumps(node_meta), session_id, name, prog_b),
            )

            # Migrate messages (Pile format: collections + progression)
            msgs_data = data.get("messages", {})
            collections = (
                msgs_data.get("collections", [])
                if isinstance(msgs_data, dict)
                else []
            )
            msg_ids = []
            for msg in collections:
                if not isinstance(msg, dict):
                    continue
                mid = msg.get("id")
                if not mid:
                    continue
                content = msg.get("content", {})
                if isinstance(content, str):
                    content = {"text": content}
                await conn.execute(
                    """INSERT OR IGNORE INTO messages
                       (id, created_at, content, sender, role, lion_class)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (mid, msg.get("created_at", created_at),
                     json.dumps(content), str(msg.get("sender", "")),
                     msg.get("role", "unknown"), msg.get("lion_class", "")),
                )
                msg_ids.append(mid)

            if msg_ids:
                await conn.execute(
                    "UPDATE progressions SET collection = ? WHERE id = ?",
                    (json.dumps(msg_ids), prog_b),
                )

            existing_branches.add(bid)
            migrated += 1

    await conn.commit()
    await db.close()
    print(f"Migrated {migrated} branches from runs/")


asyncio.run(migrate_runs())
```

Run it:

```bash
uv run python migrate_runs.py
```

## Step 2: Migrate `~/.lionagi/logs/agents/`

Older agent invocations wrote branch snapshots to `~/.lionagi/logs/agents/{provider}/{uuid}`. The format is the same as `runs/branches/*.json`.

```python
"""Migrate ~/.lionagi/logs/agents/ into state.db."""
import asyncio
import json
import uuid
from pathlib import Path

from lionagi.state.db import StateDB


async def migrate_logs():
    db = StateDB()
    await db.open()

    logs_root = Path("~/.lionagi/logs/agents").expanduser()
    if not logs_root.exists():
        print("No logs/agents/ directory found — nothing to migrate.")
        await db.close()
        return

    conn = db.db
    existing_branches = set()
    async with conn.execute("SELECT id FROM branches") as cur:
        async for row in cur:
            existing_branches.add(row[0])

    migrated = 0
    for provider_dir in sorted(logs_root.iterdir()):
        if not provider_dir.is_dir():
            continue
        for f in sorted(provider_dir.iterdir()):
            if not f.is_file() or f.name == ".DS_Store":
                continue
            try:
                data = json.loads(f.read_text())
            except (OSError, json.JSONDecodeError):
                continue

            bid = data.get("id", f.stem)
            if bid in existing_branches:
                continue

            created_at = data.get("created_at", 0)
            name = data.get("name", f"{provider_dir.name}/{bid[:8]}")
            node_meta = data.get("metadata", {})
            chat_model = data.get("chat_model")
            if chat_model:
                node_meta["chat_model"] = chat_model

            session_id = str(uuid.uuid4())
            prog_s = str(uuid.uuid4())
            prog_b = str(uuid.uuid4())

            await conn.execute(
                "INSERT OR IGNORE INTO progressions (id, collection) VALUES (?, ?)",
                (prog_s, "[]"),
            )
            await conn.execute(
                "INSERT OR IGNORE INTO progressions (id, collection) VALUES (?, ?)",
                (prog_b, "[]"),
            )
            await conn.execute(
                """INSERT INTO sessions
                   (id, created_at, updated_at, node_metadata, name,
                    progression_id, status, started_at, ended_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'completed', ?, ?)""",
                (session_id, created_at, created_at, json.dumps(node_meta),
                 name, prog_s, created_at, created_at),
            )
            await conn.execute(
                """INSERT INTO branches
                   (id, created_at, node_metadata, session_id, name, progression_id)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (bid, created_at, json.dumps(node_meta), session_id, name, prog_b),
            )

            msgs_data = data.get("messages", {})
            collections = (
                msgs_data.get("collections", [])
                if isinstance(msgs_data, dict)
                else []
            )
            msg_ids = []
            for msg in collections:
                if not isinstance(msg, dict):
                    continue
                mid = msg.get("id")
                if not mid:
                    continue
                content = msg.get("content", {})
                if isinstance(content, str):
                    content = {"text": content}
                await conn.execute(
                    """INSERT OR IGNORE INTO messages
                       (id, created_at, content, sender, role, lion_class)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (mid, msg.get("created_at", created_at),
                     json.dumps(content), str(msg.get("sender", "")),
                     msg.get("role", "unknown"), msg.get("lion_class", "")),
                )
                msg_ids.append(mid)

            if msg_ids:
                await conn.execute(
                    "UPDATE progressions SET collection = ? WHERE id = ?",
                    (json.dumps(msg_ids), prog_b),
                )

            existing_branches.add(bid)
            migrated += 1

    await conn.commit()
    await db.close()
    print(f"Migrated {migrated} branches from logs/agents/")


asyncio.run(migrate_logs())
```

Run it:

```bash
uv run python migrate_logs.py
```

## Step 3: Verify

After migration, verify every filesystem branch has a matching DB record:

```bash
uv run python -c "
import sqlite3, json
from pathlib import Path

db = sqlite3.connect(str(Path('~/.lionagi/state.db').expanduser()))
db_ids = set(r[0] for r in db.execute('SELECT id FROM branches').fetchall())

for root in ['~/.lionagi/runs', '~/.lionagi/logs/agents']:
    p = Path(root).expanduser()
    if not p.exists():
        continue
    total = missing = 0
    for f in p.rglob('*.json'):
        if f.name == '.DS_Store':
            continue
        bid = f.stem
        # For runs/branches/*.json the stem is the branch ID
        # For logs/agents/{provider}/{uuid} the stem is the branch ID
        total += 1
        if bid not in db_ids:
            # Check if file content has a different ID
            try:
                data = json.loads(f.read_text())
                if data.get('id') in db_ids:
                    continue
            except:
                pass
            missing += 1
            print(f'  MISSING: {f}')
    print(f'{root}: {total - missing}/{total} in DB')

integrity = db.execute('PRAGMA integrity_check').fetchone()[0]
print(f'DB integrity: {integrity}')
db.close()
"
```

Expected output: all branches accounted for, `DB integrity: ok`.

## Step 4: Clean up

Once verification passes, delete the filesystem logs:

```bash
rm -rf ~/.lionagi/runs/
rm -rf ~/.lionagi/logs/
```

## Fixing stuck sessions

Sessions that show `running` long after the process exited can be cleaned up:

```bash
li state doctor   # detect phantom sessions
li state prune    # mark stale sessions as completed
```

Or manually via Studio: **Admin** tab > **Doctor** > **Prune phantoms**.
