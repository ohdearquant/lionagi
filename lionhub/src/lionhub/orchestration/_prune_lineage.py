# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""SQL for preserving the retained lineage of pruned sessions."""


def _prune_temp_table(dialect: str, name: str) -> str:
    if dialect == "sqlite":
        return f"temp.{name}"
    if dialect == "postgresql":
        return f"pg_temp.{name}"
    raise ValueError(f"Session prune does not support {dialect!r}")


def _non_array_progression_sql(dialect: str, collection: str = "p.collection") -> str:
    if dialect == "sqlite":
        return f"json_type({collection}) <> 'array'"
    if dialect == "postgresql":
        return f"jsonb_typeof(CAST({collection} AS jsonb)) <> 'array'"
    raise ValueError(f"Session prune does not support {dialect!r}")


def _progression_members(dialect: str, collection: str = "p.collection") -> str:
    """The join that expands immediate collection members, under the alias members."""
    if dialect == "sqlite":
        return f"json_each({collection}) AS members"
    if dialect == "postgresql":
        # Match the SQLite json_each values used for TEXT ID comparisons:
        # booleans become 1/0, JSON null becomes SQL NULL, and strings are unquoted.
        # Numbers use jsonb's numeric text. Decimal and exponent forms can differ.
        # For arrays and objects, strip whitespace outside quoted strings to match
        # SQLite's compact JSON. Validate with jsonb and expand the original json
        # so every immediate value from a duplicate object key contributes.
        return (
            "LATERAL ("  # noqa: S608 -- collection is an internal SQL column
            " SELECT CASE json_typeof(value)"
            "   WHEN 'boolean' THEN CASE WHEN value::text = 'true' THEN '1' ELSE '0' END"
            "   WHEN 'null' THEN NULL"
            "   WHEN 'string' THEN value #>> '{}'"
            "   WHEN 'number' THEN CAST(value AS jsonb) #>> '{}'"
            "   ELSE regexp_replace(value::text, '(\"([^\"\\\\]|\\\\.)*\")|[[:space:]]+', '\\1', 'g')"
            "   END AS value"
            f" FROM (SELECT CAST({collection} AS json) AS data,"
            f"              jsonb_typeof(CAST({collection} AS jsonb)) AS shape) AS collection_data"
            " CROSS JOIN LATERAL ("
            "  SELECT value FROM json_array_elements("
            "    CASE shape WHEN 'array' THEN data ELSE '[]'::json END)"
            "  UNION ALL"
            "  SELECT value FROM json_each(CASE shape WHEN 'object' THEN data ELSE '{}'::json END)"
            "  UNION ALL SELECT data WHERE shape NOT IN ('array', 'object')"
            " ) AS expanded"
            ") AS members"
        )
    raise ValueError(f"Session prune does not support {dialect!r}")


def _retained_message_ids_sql(
    dialect: str,
    *,
    excluded_progressions_sql: str | None = None,
    deferred_direct_sql: str | None = None,
) -> str:
    excluded = (
        f" AND NOT EXISTS (SELECT 1 FROM ({excluded_progressions_sql}) AS excluded"  # noqa: S608
        " WHERE excluded.id = p.id)"
        if excluded_progressions_sql is not None
        else ""
    )
    deferred = f" UNION ALL {deferred_direct_sql}" if deferred_direct_sql is not None else ""
    # The message key is TEXT. SQLite's compound comparisons do not coerce
    # numeric members to text, so both prune paths cast before comparing IDs.
    return (
        f"SELECT CAST(members.value AS TEXT) FROM progressions p"  # noqa: S608
        f" CROSS JOIN {_progression_members(dialect)}"
        f" WHERE members.value IS NOT NULL{excluded}"
        " UNION ALL SELECT first_msg_id FROM sessions WHERE first_msg_id IS NOT NULL"
        " UNION ALL SELECT last_msg_id FROM sessions WHERE last_msg_id IS NOT NULL"
        " UNION ALL SELECT system_msg_id FROM branches WHERE system_msg_id IS NOT NULL"
        f"{deferred}"
    )


def _prune_lineage_refs_sql(dialect: str, victim_ids_sql: str) -> str:
    """Return progression and direct message references for selected session IDs.

    A non-array progression protects every reference in its owner's lineage.
    Group shared references across victims so any protected owner wins.
    """
    non_array = _non_array_progression_sql(dialect)
    return (
        f"WITH victims(id) AS ({victim_ids_sql}),"  # noqa: S608 -- internal victim query
        " owners(owner_id, progression_id, first_id, last_id, system_id) AS ("
        "  SELECT s.id, s.progression_id, s.first_msg_id, s.last_msg_id, CAST(NULL AS TEXT)"
        "  FROM sessions s JOIN victims v ON v.id = s.id"
        "  UNION ALL"
        "  SELECT b.session_id, b.progression_id, NULL, NULL, b.system_msg_id"
        "  FROM branches b JOIN victims v ON v.id = b.session_id"
        "), shapes AS ("
        f"  SELECT p.id, CASE WHEN {non_array} THEN 1 ELSE 0 END AS non_array"
        "  FROM progressions p"
        "  JOIN (SELECT DISTINCT progression_id FROM owners) wanted ON wanted.progression_id = p.id"
        "), lineages AS ("
        "  SELECT owners.*, COALESCE(shapes.non_array, 0) AS non_array,"
        "         MAX(COALESCE(shapes.non_array, 0)) OVER (PARTITION BY owner_id) AS deferred"
        "  FROM owners LEFT JOIN shapes ON shapes.id = owners.progression_id"
        "), refs(kind, id, deferred, non_array) AS ("
        "  SELECT 'progression', progression_id, deferred, non_array FROM lineages"
        "  UNION ALL SELECT 'message', first_id, deferred, 0 FROM lineages"
        "  UNION ALL SELECT 'message', last_id, deferred, 0 FROM lineages"
        "  UNION ALL SELECT 'message', system_id, deferred, 0 FROM lineages"
        ") SELECT kind, id, MAX(deferred) AS deferred, MAX(non_array) AS non_array"
        " FROM refs WHERE id IS NOT NULL GROUP BY kind, id"
    )
