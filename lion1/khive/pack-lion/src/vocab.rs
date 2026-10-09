//! The pack's tables (ADR-0025 D1), its two entity types, and its verbs (D4).
//!
//! A row here is not a note: it is not embedded, not indexed for search and not linked. Every
//! table carries the namespace the row was written under, and every read filters by it. Times are
//! microseconds since the epoch, as the store's own tables keep them.

use khive_types::{
    EntityTypeDef, HandlerDef, IdResolutionMode, PackColumnAddition, PackColumnAffinity, ParamDef, VerbCategory,
    Visibility,
};

pub(crate) const PACK_NAME: &str = "lion";

/// Applied idempotently at boot, in order: one statement per file under `sql/`.
pub(crate) static SCHEMA: [&str; 10] = [
    include_str!("../sql/01-sessions.sql"),
    include_str!("../sql/02-sessions-index.sql"),
    include_str!("../sql/03-messages.sql"),
    include_str!("../sql/04-calls.sql"),
    include_str!("../sql/05-calls-index.sql"),
    include_str!("../sql/06-runs.sql"),
    include_str!("../sql/07-runs-index.sql"),
    include_str!("../sql/08-steps.sql"),
    include_str!("../sql/09-steps-index.sql"),
    include_str!("../sql/10-notes.sql"),
];

/// A column added to a table a store made before it existed: the pieces of a carried field (D8).
pub(crate) static SCHEMA_COLUMN_ADDITIONS: [PackColumnAddition; 1] =
    [PackColumnAddition { table: "lion_messages", column: "carried", affinity: PackColumnAffinity::Text }];

/// No entity type yet: a profile and a flow belong on the graph as artifacts, and the record that
/// writes them registers them (ADR-0025 D1, S4). The store's builtin vocabulary already holds
/// `artifact:profile` (a model's), so theirs will carry the pack's name.
pub(crate) const ENTITY_TYPES: [EntityTypeDef; 0] = [];

const fn p(name: &'static str, param_type: &'static str, required: bool, description: &'static str) -> ParamDef {
    ParamDef { name, param_type, required, description, resolution_mode: IdResolutionMode::NotApplicable }
}

const fn verb(name: &'static str, description: &'static str, category: VerbCategory, params: &'static [ParamDef]) -> HandlerDef {
    HandlerDef { name, description, visibility: Visibility::Verb, category, params }
}

pub(crate) static HANDLERS: [HandlerDef; 17] = [
    verb("lion.session_open", "Start a session row: one history a model is shown. Returns its id.", VerbCategory::Commissive, &[
        p("source", "string", true, "Who keeps the history: lion1, claude_code or codex."),
        p("provider_session_id", "string", false, "The CLI's own session id, for a harness session."),
        p("actor", "string", false, "The actor the session belongs to."),
        p("profile", "string", false, "The profile the run wore."),
        p("transcript", "string", false, "The CLI's transcript path, for a harness session."),
        p("id", "string", false, "The id to use; a fresh one when absent."),
    ]),
    verb("lion.session_close", "End a session row: its end time and its message count.", VerbCategory::Commissive, &[
        p("id", "string", true, "The session."),
    ]),
    verb("lion.message_append", "One message row of a session, refused when its seq is taken.", VerbCategory::Commissive, &[
        p("session_id", "string", true, "The session."),
        p("seq", "integer", true, "The entry's sequence number in its record; unique per session."),
        p("role", "string", true, "The provider's chat role: system, user, assistant or tool."),
        p("kind", "string", true, "The runtime's kind (input, system, text, result) or the CLI's own row type."),
        p("text", "string", true, "What the model was shown, or said."),
        p("raw", "string", false, "The entry as stored, when it differs from text."),
        p("turn", "integer", false, "The turn the entry belongs to; 0 when absent."),
        p("alias", "string", false, "The model's own name for the entry."),
        p("parent_seq", "integer", false, "For a result, the seq of the command it answers."),
        p("directive", "string", false, "The view directive the entry carries, as JSON."),
    ]),
    verb("lion.message_append_carried", "A message row whose large fields are blob pieces, each read back and verified first.", VerbCategory::Commissive, &[
        p("session_id", "string", true, "The session."),
        p("seq", "integer", true, "The entry's sequence number in its record; unique per session."),
        p("role", "string", true, "The provider's chat role: system, user, assistant or tool."),
        p("kind", "string", true, "The runtime's kind (input, system, text, result) or the CLI's own row type."),
        p("text", "string", true, "What the model was shown, or said; empty when carried."),
        p("raw", "string", false, "The entry as stored, when it differs from text; absent when carried."),
        p("turn", "integer", false, "The turn the entry belongs to; 0 when absent."),
        p("alias", "string", false, "The model's own name for the entry."),
        p("parent_seq", "integer", false, "For a result, the seq of the command it answers."),
        p("directive", "string", false, "The view directive the entry carries, as JSON; absent when carried."),
        p("carried", "object", true, "Of text, raw and directive, each carried one: {size, pieces} or {size, index}."),
    ]),
    verb("lion.call_open", "A call row at the moment a request goes to a model. Returns its id.", VerbCategory::Commissive, &[
        p("session_id", "string", true, "The session."),
        p("turn", "integer", true, "The run's turn."),
        p("model", "string", true, "The model asked."),
        p("seq_from", "integer", true, "The first message rendered into the request."),
        p("seq_to", "integer", true, "The last message rendered into the request."),
        p("folded", "string", false, "The seqs in that range folded to a placeholder, as JSON."),
    ]),
    verb("lion.call_close", "The answer or the refusal of a call.", VerbCategory::Commissive, &[
        p("id", "string", true, "The call."),
        p("status", "string", true, "ok, refused, limit, cut when the person abandoned the call, or failed when the request raised."),
        p("reply_seq", "integer", false, "The message the answer became."),
        p("tokens_in", "integer", false, "Prompt tokens the provider reported."),
        p("tokens_out", "integer", false, "Completion tokens the provider reported."),
        p("cost", "number", false, "What the provider reported, in dollars."),
        p("duration_ms", "integer", false, "Request to answer."),
        p("error", "string", false, "The refusal, verbatim."),
    ]),
    verb("lion.run_open", "A run row before its first call. With a step, the step row too. Returns its id.", VerbCategory::Commissive, &[
        p("profile", "string", true, "The profile the run wears."),
        p("session_id", "string", true, "The run's session."),
        p("id", "string", false, "The id to use; a fresh one when absent."),
        p("actor", "string", false, "The actor."),
        p("bounds", "string", false, "The job's bounds, as JSON."),
        p("trigger", "string", false, "The inbound ids that started it, as JSON."),
        p("continues", "string", false, "The run this one resumes by replay."),
        p("step", "object", false, "flow_run_id, idx, name, inputs_from: this run as a step of a flow's run."),
    ]),
    verb("lion.run_close", "A run's outcome, and its step's.", VerbCategory::Commissive, &[
        p("id", "string", true, "The run."),
        p("outcome", "string", true, "success, continue, exhausted, refused or stopped."),
        p("cost", "number", false, "Summed over its calls."),
        p("turns", "integer", false, "How many turns it took."),
        p("output", "string", false, "The run's output fields, as JSON."),
    ]),
    verb("lion.get", "One row by id, or a session's messages in seq order.", VerbCategory::Assertive, &[
        p("table", "string", true, "sessions, messages, calls, runs or steps."),
        p("id", "string", true, "The row's id; for messages and steps, the session's or the flow run's."),
    ]),
    verb("lion.messages", "A session's messages after a seq, in order, one page under 2 MiB.", VerbCategory::Assertive, &[
        p("session_id", "string", true, "The session."),
        p("after", "integer", false, "Rows after this seq; all from the first when absent."),
        p("limit", "integer", false, "1 to 1000 rows; 200."),
    ]),
    verb("lion.runs", "Runs newest first, in the caller's namespace.", VerbCategory::Assertive, &[
        p("limit", "integer", false, "1 to 200; 20."),
        p("actor", "string", false, "Only this actor's."),
    ]),
    verb("lion.sessions", "Sessions newest first, in the caller's namespace.", VerbCategory::Assertive, &[
        p("limit", "integer", false, "1 to 200; 20."),
        p("source", "string", false, "Only this source's: lion1, claude_code or codex."),
        p("actor", "string", false, "Only this actor's."),
    ]),
    verb("lion.note_put", "Write a profile's note, or replace it and count its version up.", VerbCategory::Commissive, &[
        p("profile", "string", true, "The profile whose note it is."),
        p("key", "string", true, "The note's name under `note.`."),
        p("value", "string", true, "The value, as JSON."),
        p("run", "string", true, "The run that wrote it."),
        p("seq", "integer", true, "The record sequence of the write."),
    ]),
    verb("lion.note_get", "One note by key; not found when there is none.", VerbCategory::Assertive, &[
        p("profile", "string", true, "The profile."),
        p("key", "string", true, "The key."),
    ]),
    verb("lion.note_list", "A profile's notes in key order, under a key prefix.", VerbCategory::Assertive, &[
        p("profile", "string", true, "The profile."),
        p("prefix", "string", false, "Keys starting with this; all when absent."),
    ]),
    verb("lion.note_find", "A profile's notes whose key or value holds the query, case-folded.", VerbCategory::Assertive, &[
        p("profile", "string", true, "The profile."),
        p("query", "string", true, "A substring; blank is refused."),
    ]),
    verb("lion.note_delete", "Remove a note; the row removed, or not found.", VerbCategory::Commissive, &[
        p("profile", "string", true, "The profile."),
        p("key", "string", true, "The key."),
    ]),
];
