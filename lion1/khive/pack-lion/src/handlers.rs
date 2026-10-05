//! The verbs (ADR-0025 D4), each one query from `sql/`. A write that touches no row is a
//! `NotFound`; a message whose seq is taken is an `InvalidInput`, since the caller's record and the
//! store's history must not disagree about what a seq names.

use khive_runtime::{KhiveRuntime, NamespaceToken, RuntimeError};
use khive_storage::types::{SqlRow, SqlStatement, SqlValue};
use serde::Deserialize;
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};

type Out = Result<Value, RuntimeError>;

fn now() -> i64 {
    chrono::Utc::now().timestamp_micros()
}

fn fresh() -> String {
    uuid::Uuid::new_v4().simple().to_string()
}

fn deser<T: serde::de::DeserializeOwned>(params: Value) -> Result<T, RuntimeError> {
    serde_json::from_value(params).map_err(|e| RuntimeError::InvalidInput(format!("bad params: {e}")))
}

fn text(v: Option<String>) -> SqlValue {
    v.map(SqlValue::Text).unwrap_or(SqlValue::Null)
}

fn int(v: Option<i64>) -> SqlValue {
    v.map(SqlValue::Integer).unwrap_or(SqlValue::Null)
}

fn float(v: Option<f64>) -> SqlValue {
    v.map(SqlValue::Float).unwrap_or(SqlValue::Null)
}

fn stmt(sql: &'static str, params: Vec<SqlValue>) -> SqlStatement {
    SqlStatement { sql: sql.to_string(), params, label: None }
}

async fn execute(rt: &KhiveRuntime, sql: &'static str, params: Vec<SqlValue>) -> Result<u64, RuntimeError> {
    let mut w = rt.sql().writer().await?;
    Ok(w.execute(stmt(sql, params)).await?)
}

async fn rows(rt: &KhiveRuntime, sql: &'static str, params: Vec<SqlValue>) -> Result<Vec<SqlRow>, RuntimeError> {
    let mut r = rt.sql().reader().await?;
    Ok(r.query_all(stmt(sql, params)).await?)
}

/// A write of one row by id: zero rows touched is the id not being there.
async fn one(rt: &KhiveRuntime, sql: &'static str, params: Vec<SqlValue>, what: &str, id: &str) -> Result<(), RuntimeError> {
    match execute(rt, sql, params).await? {
        0 => Err(RuntimeError::NotFound(format!("{what} {id} is not in this namespace"))),
        _ => Ok(()),
    }
}

fn value(v: &SqlValue) -> Value {
    match v {
        SqlValue::Null => Value::Null,
        SqlValue::Bool(b) => json!(b),
        SqlValue::Integer(i) => json!(i),
        SqlValue::Float(f) => json!(f),
        SqlValue::Text(s) => json!(s),
        SqlValue::Blob(b) => json!({"blob_bytes": b.len()}),
        SqlValue::Json(j) => j.clone(),
        SqlValue::Uuid(u) => json!(u.to_string()),
        SqlValue::Timestamp(t) => json!(t.to_rfc3339()),
    }
}

fn row(r: &SqlRow) -> Value {
    Value::Object(r.columns.iter().map(|c| (c.name.clone(), value(&c.value))).collect::<Map<_, _>>())
}

fn ns(token: &NamespaceToken) -> SqlValue {
    SqlValue::Text(token.namespace().as_str().to_string())
}

#[derive(Deserialize)]
struct SessionOpen {
    source: String,
    provider_session_id: Option<String>,
    actor: Option<String>,
    profile: Option<String>,
    transcript: Option<String>,
    id: Option<String>,
}

pub(crate) async fn session_open(rt: &KhiveRuntime, token: &NamespaceToken, params: Value) -> Out {
    let p: SessionOpen = deser(params)?;
    if !matches!(p.source.as_str(), "lion1" | "claude_code" | "codex") {
        return Err(RuntimeError::InvalidInput(format!("source {:?}: lion1, claude_code or codex", p.source)));
    }
    let id = p.id.unwrap_or_else(fresh);
    let started = now();
    execute(
        rt,
        include_str!("../sql/session_open.sql"),
        vec![
            SqlValue::Text(id.clone()),
            ns(token),
            SqlValue::Text(p.source),
            text(p.provider_session_id),
            text(p.actor),
            text(p.profile),
            text(p.transcript),
            SqlValue::Integer(started),
        ],
    )
    .await?;
    Ok(json!({"id": id, "started": started}))
}

#[derive(Deserialize)]
struct ById {
    id: String,
}

pub(crate) async fn session_close(rt: &KhiveRuntime, token: &NamespaceToken, params: Value) -> Out {
    let p: ById = deser(params)?;
    let ended = now();
    one(
        rt,
        include_str!("../sql/session_close.sql"),
        vec![SqlValue::Text(p.id.clone()), ns(token), SqlValue::Integer(ended)],
        "session",
        &p.id,
    )
    .await?;
    let got = rows(rt, include_str!("../sql/get_sessions.sql"), vec![SqlValue::Text(p.id), ns(token)]).await?;
    Ok(got.first().map(row).unwrap_or(Value::Null))
}

#[derive(Deserialize)]
struct MessageAppend {
    session_id: String,
    seq: i64,
    role: String,
    kind: String,
    text: String,
    raw: Option<String>,
    turn: Option<i64>,
    alias: Option<String>,
    parent_seq: Option<i64>,
    directive: Option<String>,
}

pub(crate) async fn message_append(rt: &KhiveRuntime, token: &NamespaceToken, params: Value) -> Out {
    let p: MessageAppend = deser(params)?;
    if !matches!(p.role.as_str(), "system" | "user" | "assistant" | "tool") {
        return Err(RuntimeError::InvalidInput(format!("role {:?}: system, user, assistant or tool", p.role)));
    }
    if rows(rt, include_str!("../sql/get_sessions.sql"), vec![SqlValue::Text(p.session_id.clone()), ns(token)]).await?.is_empty() {
        return Err(RuntimeError::NotFound(format!("session {} is not in this namespace", p.session_id)));
    }
    let taken = rows(
        rt,
        include_str!("../sql/message_taken.sql"),
        vec![SqlValue::Text(p.session_id.clone()), SqlValue::Integer(p.seq), ns(token)],
    )
    .await?;
    if !taken.is_empty() {
        return Err(RuntimeError::InvalidInput(format!("session {} already holds seq {}", p.session_id, p.seq)));
    }
    let hash = format!("{:x}", Sha256::digest(p.text.as_bytes()));
    let created = now();
    execute(
        rt,
        include_str!("../sql/message_append.sql"),
        vec![
            SqlValue::Text(p.session_id.clone()),
            SqlValue::Integer(p.seq),
            ns(token),
            SqlValue::Text(p.role),
            SqlValue::Text(p.kind),
            SqlValue::Integer(p.turn.unwrap_or(0)),
            text(p.alias),
            SqlValue::Text(p.text),
            text(p.raw),
            SqlValue::Text(hash.clone()),
            int(p.parent_seq),
            text(p.directive),
            SqlValue::Integer(created),
        ],
    )
    .await?;
    Ok(json!({"session_id": p.session_id, "seq": p.seq, "content_hash": hash, "created": created}))
}

#[derive(Deserialize)]
struct CallOpen {
    session_id: String,
    turn: i64,
    model: String,
    seq_from: i64,
    seq_to: i64,
    folded: Option<String>,
}

pub(crate) async fn call_open(rt: &KhiveRuntime, token: &NamespaceToken, params: Value) -> Out {
    let p: CallOpen = deser(params)?;
    if p.seq_to < p.seq_from {
        return Err(RuntimeError::InvalidInput(format!("seq_to {} before seq_from {}", p.seq_to, p.seq_from)));
    }
    let id = fresh();
    let opened = now();
    execute(
        rt,
        include_str!("../sql/call_open.sql"),
        vec![
            SqlValue::Text(id.clone()),
            ns(token),
            SqlValue::Text(p.session_id),
            SqlValue::Integer(p.turn),
            SqlValue::Text(p.model),
            SqlValue::Integer(p.seq_from),
            SqlValue::Integer(p.seq_to),
            text(p.folded),
            SqlValue::Integer(opened),
        ],
    )
    .await?;
    Ok(json!({"id": id, "opened": opened}))
}

#[derive(Deserialize)]
struct CallClose {
    id: String,
    status: String,
    reply_seq: Option<i64>,
    tokens_in: Option<i64>,
    tokens_out: Option<i64>,
    cost: Option<f64>,
    duration_ms: Option<i64>,
    error: Option<String>,
}

pub(crate) async fn call_close(rt: &KhiveRuntime, token: &NamespaceToken, params: Value) -> Out {
    let p: CallClose = deser(params)?;
    if !matches!(p.status.as_str(), "ok" | "refused" | "limit" | "cut" | "failed") {
        return Err(RuntimeError::InvalidInput(format!("status {:?}: ok, refused, limit, cut or failed", p.status)));
    }
    if p.status == "ok" && p.reply_seq.is_none() {
        return Err(RuntimeError::InvalidInput("an ok call names its reply_seq".into()));
    }
    let closed = now();
    one(
        rt,
        include_str!("../sql/call_close.sql"),
        vec![
            SqlValue::Text(p.id.clone()),
            ns(token),
            SqlValue::Text(p.status),
            int(p.reply_seq),
            int(p.tokens_in),
            int(p.tokens_out),
            float(p.cost),
            int(p.duration_ms),
            text(p.error),
            SqlValue::Integer(closed),
        ],
        "call",
        &p.id,
    )
    .await?;
    Ok(json!({"id": p.id, "closed": closed}))
}

#[derive(Deserialize)]
struct Step {
    flow_run_id: String,
    idx: i64,
    name: String,
    inputs_from: Option<Value>,
}

#[derive(Deserialize)]
struct RunOpen {
    profile: String,
    session_id: String,
    id: Option<String>,
    actor: Option<String>,
    bounds: Option<String>,
    trigger: Option<String>,
    continues: Option<String>,
    step: Option<Step>,
}

pub(crate) async fn run_open(rt: &KhiveRuntime, token: &NamespaceToken, params: Value) -> Out {
    let p: RunOpen = deser(params)?;
    let id = p.id.unwrap_or_else(fresh);
    let started = now();
    execute(
        rt,
        include_str!("../sql/run_open.sql"),
        vec![
            SqlValue::Text(id.clone()),
            ns(token),
            text(p.actor),
            SqlValue::Text(p.profile.clone()),
            SqlValue::Text(p.session_id),
            text(p.bounds),
            text(p.trigger),
            text(p.continues),
            SqlValue::Integer(started),
        ],
    )
    .await?;
    if let Some(s) = p.step {
        execute(
            rt,
            include_str!("../sql/step_open.sql"),
            vec![
                SqlValue::Text(s.flow_run_id),
                SqlValue::Integer(s.idx),
                ns(token),
                SqlValue::Text(s.name),
                SqlValue::Text(p.profile),
                SqlValue::Text(id.clone()),
                s.inputs_from.map(|v| SqlValue::Text(v.to_string())).unwrap_or(SqlValue::Null),
            ],
        )
        .await?;
    }
    Ok(json!({"id": id, "started": started}))
}

#[derive(Deserialize)]
struct RunClose {
    id: String,
    outcome: String,
    cost: Option<f64>,
    turns: Option<i64>,
    output: Option<String>,
}

pub(crate) async fn run_close(rt: &KhiveRuntime, token: &NamespaceToken, params: Value) -> Out {
    let p: RunClose = deser(params)?;
    if !matches!(p.outcome.as_str(), "success" | "continue" | "exhausted" | "refused" | "stopped") {
        return Err(RuntimeError::InvalidInput(format!(
            "outcome {:?}: success, continue, exhausted, refused or stopped",
            p.outcome
        )));
    }
    let ended = now();
    one(
        rt,
        include_str!("../sql/run_close.sql"),
        vec![
            SqlValue::Text(p.id.clone()),
            ns(token),
            SqlValue::Text(p.outcome.clone()),
            float(p.cost),
            int(p.turns),
            text(p.output),
            SqlValue::Integer(ended),
        ],
        "run",
        &p.id,
    )
    .await?;
    // a run that is a step closes its step too; a run that is none touches nothing here
    execute(
        rt,
        include_str!("../sql/step_close.sql"),
        vec![SqlValue::Text(p.id.clone()), ns(token), SqlValue::Text(p.outcome)],
    )
    .await?;
    Ok(json!({"id": p.id, "ended": ended}))
}

#[derive(Deserialize)]
struct Get {
    table: String,
    id: String,
}

pub(crate) async fn get(rt: &KhiveRuntime, token: &NamespaceToken, params: Value) -> Out {
    let p: Get = deser(params)?;
    let (sql, many) = match p.table.as_str() {
        "sessions" => (include_str!("../sql/get_sessions.sql"), false),
        "calls" => (include_str!("../sql/get_calls.sql"), false),
        "runs" => (include_str!("../sql/get_runs.sql"), false),
        "messages" => (include_str!("../sql/get_messages.sql"), true),
        "steps" => (include_str!("../sql/get_steps.sql"), true),
        other => return Err(RuntimeError::InvalidInput(format!("table {other:?}: sessions, messages, calls, runs or steps"))),
    };
    let got = rows(rt, sql, vec![SqlValue::Text(p.id.clone()), ns(token)]).await?;
    if many {
        return Ok(json!({"count": got.len(), "rows": got.iter().map(row).collect::<Vec<_>>()}));
    }
    got.first().map(row).ok_or_else(|| RuntimeError::NotFound(format!("{} {} is not in this namespace", p.table, p.id)))
}

#[derive(Deserialize)]
struct Runs {
    limit: Option<i64>,
    actor: Option<String>,
}

pub(crate) async fn runs(rt: &KhiveRuntime, token: &NamespaceToken, params: Value) -> Out {
    let p: Runs = deser(params)?;
    let limit = p.limit.unwrap_or(20);
    if !(1..=200).contains(&limit) {
        return Err(RuntimeError::InvalidInput(format!("limit {limit}: 1 to 200")));
    }
    let got = rows(rt, include_str!("../sql/runs.sql"), vec![ns(token), text(p.actor), SqlValue::Integer(limit)]).await?;
    Ok(json!({"count": got.len(), "rows": got.iter().map(row).collect::<Vec<_>>()}))
}

#[derive(Deserialize)]
struct Sessions {
    limit: Option<i64>,
    source: Option<String>,
    actor: Option<String>,
}

pub(crate) async fn sessions(rt: &KhiveRuntime, token: &NamespaceToken, params: Value) -> Out {
    let p: Sessions = deser(params)?;
    let limit = p.limit.unwrap_or(20);
    if !(1..=200).contains(&limit) {
        return Err(RuntimeError::InvalidInput(format!("limit {limit}: 1 to 200")));
    }
    let got = rows(
        rt,
        include_str!("../sql/sessions.sql"),
        vec![ns(token), text(p.source), text(p.actor), SqlValue::Integer(limit)],
    )
    .await?;
    Ok(json!({"count": got.len(), "rows": got.iter().map(row).collect::<Vec<_>>()}))
}

#[derive(Deserialize)]
struct NotePut {
    profile: String,
    key: String,
    value: String,
    run: String,
    seq: i64,
}

fn note_key(profile: &str, key: &str) -> Result<(), RuntimeError> {
    if profile.is_empty() || key.is_empty() {
        return Err(RuntimeError::InvalidInput("a note has a profile and a key".into()));
    }
    Ok(())
}

async fn note_row(rt: &KhiveRuntime, token: &NamespaceToken, profile: &str, key: &str) -> Result<Option<Value>, RuntimeError> {
    let got = rows(
        rt,
        include_str!("../sql/note_get.sql"),
        vec![ns(token), SqlValue::Text(profile.to_string()), SqlValue::Text(key.to_string())],
    )
    .await?;
    Ok(got.first().map(row))
}

pub(crate) async fn note_put(rt: &KhiveRuntime, token: &NamespaceToken, params: Value) -> Out {
    let p: NotePut = deser(params)?;
    note_key(&p.profile, &p.key)?;
    serde_json::from_str::<Value>(&p.value)
        .map_err(|e| RuntimeError::InvalidInput(format!("value is not JSON: {e}")))?;
    execute(
        rt,
        include_str!("../sql/note_put.sql"),
        vec![
            ns(token),
            SqlValue::Text(p.profile.clone()),
            SqlValue::Text(p.key.clone()),
            SqlValue::Text(p.value),
            SqlValue::Text(p.run),
            SqlValue::Integer(p.seq),
            SqlValue::Integer(now()),
        ],
    )
    .await?;
    note_row(rt, token, &p.profile, &p.key).await?.ok_or_else(|| RuntimeError::Internal("the note just written is not there".into()))
}

#[derive(Deserialize)]
struct NoteKey {
    profile: String,
    key: String,
}

pub(crate) async fn note_get(rt: &KhiveRuntime, token: &NamespaceToken, params: Value) -> Out {
    let p: NoteKey = deser(params)?;
    note_key(&p.profile, &p.key)?;
    note_row(rt, token, &p.profile, &p.key)
        .await?
        .ok_or_else(|| RuntimeError::NotFound(format!("note {} of {} is not in this namespace", p.key, p.profile)))
}

#[derive(Deserialize)]
struct NoteList {
    profile: String,
    prefix: Option<String>,
}

pub(crate) async fn note_list(rt: &KhiveRuntime, token: &NamespaceToken, params: Value) -> Out {
    let p: NoteList = deser(params)?;
    let got = rows(
        rt,
        include_str!("../sql/note_list.sql"),
        vec![ns(token), SqlValue::Text(p.profile), SqlValue::Text(p.prefix.unwrap_or_default())],
    )
    .await?;
    Ok(json!({"count": got.len(), "rows": got.iter().map(row).collect::<Vec<_>>()}))
}

#[derive(Deserialize)]
struct NoteFind {
    profile: String,
    query: String,
}

pub(crate) async fn note_find(rt: &KhiveRuntime, token: &NamespaceToken, params: Value) -> Out {
    let p: NoteFind = deser(params)?;
    if p.query.trim().is_empty() {
        return Err(RuntimeError::InvalidInput("note_find: a blank query would match every note; list instead".into()));
    }
    // SQLite's lower() folds ASCII only; the query is folded the same way so the two sides agree
    let got = rows(
        rt,
        include_str!("../sql/note_find.sql"),
        vec![ns(token), SqlValue::Text(p.profile), SqlValue::Text(p.query.to_ascii_lowercase())],
    )
    .await?;
    Ok(json!({"count": got.len(), "rows": got.iter().map(row).collect::<Vec<_>>()}))
}

pub(crate) async fn note_delete(rt: &KhiveRuntime, token: &NamespaceToken, params: Value) -> Out {
    let p: NoteKey = deser(params)?;
    note_key(&p.profile, &p.key)?;
    let was = note_row(rt, token, &p.profile, &p.key)
        .await?
        .ok_or_else(|| RuntimeError::NotFound(format!("note {} of {} is not in this namespace", p.key, p.profile)))?;
    execute(
        rt,
        include_str!("../sql/note_delete.sql"),
        vec![ns(token), SqlValue::Text(p.profile), SqlValue::Text(p.key)],
    )
    .await?;
    Ok(was)
}
