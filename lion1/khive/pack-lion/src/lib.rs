//! `lion`: this runtime's sessions, messages, calls and runs as tables of its own in the store
//! (ADR-0025). The store is taken at one pinned revision and not changed; the pack owns its tables
//! (`sql/`) and serves its verbs. Select it with
//! `KHIVE_PACKS=...,lion`.

mod handlers;
pub mod vocab;

use async_trait::async_trait;
use serde_json::Value;

use khive_runtime::pack::PackRuntime;
use khive_runtime::{KhiveRuntime, NamespaceToken, RuntimeError, SchemaPlan, VerbRegistry};
use khive_types::{EdgeEndpointRule, EntityTypeDef, HandlerDef, Pack, PackColumnAddition, PackSchemaPlan};

use crate::vocab::{ENTITY_TYPES, HANDLERS, PACK_NAME, SCHEMA, SCHEMA_COLUMN_ADDITIONS};

pub struct LionPack {
    runtime: KhiveRuntime,
}

impl LionPack {
    pub fn new(runtime: KhiveRuntime) -> Self {
        Self { runtime }
    }
}

impl Pack for LionPack {
    const NAME: &'static str = PACK_NAME;
    const NOTE_KINDS: &'static [&'static str] = &[];
    const ENTITY_KINDS: &'static [&'static str] = &[];
    const HANDLERS: &'static [HandlerDef] = &HANDLERS;
    const ENTITY_TYPES: &'static [EntityTypeDef] = &ENTITY_TYPES;
    const REQUIRES: &'static [&'static str] = &["kg"];
    const SCHEMA_PLAN: Option<PackSchemaPlan> = Some(PackSchemaPlan { pack: PACK_NAME, statements: &SCHEMA });
    const SCHEMA_COLUMN_ADDITIONS: &'static [PackColumnAddition] = &SCHEMA_COLUMN_ADDITIONS;
}

struct LionPackFactory;

impl khive_runtime::PackFactory for LionPackFactory {
    fn name(&self) -> &'static str {
        PACK_NAME
    }

    fn requires(&self) -> &'static [&'static str] {
        <LionPack as Pack>::REQUIRES
    }

    fn create(&self, runtime: KhiveRuntime) -> Box<dyn PackRuntime> {
        Box::new(LionPack::new(runtime))
    }
}

inventory::submit! { khive_runtime::PackRegistration(&LionPackFactory) }

#[async_trait]
impl PackRuntime for LionPack {
    fn name(&self) -> &str {
        PACK_NAME
    }

    fn note_kinds(&self) -> &'static [&'static str] {
        <LionPack as Pack>::NOTE_KINDS
    }

    fn entity_kinds(&self) -> &'static [&'static str] {
        <LionPack as Pack>::ENTITY_KINDS
    }

    fn handlers(&self) -> &'static [HandlerDef] {
        <LionPack as Pack>::HANDLERS
    }

    fn edge_rules(&self) -> &'static [EdgeEndpointRule] {
        &[]
    }

    fn entity_types(&self) -> &'static [EntityTypeDef] {
        <LionPack as Pack>::ENTITY_TYPES
    }

    fn requires(&self) -> &'static [&'static str] {
        <LionPack as Pack>::REQUIRES
    }

    fn schema_plan(&self) -> SchemaPlan {
        SchemaPlan { pack: PACK_NAME, statements: &SCHEMA }
    }

    fn schema_column_additions(&self) -> &'static [PackColumnAddition] {
        <LionPack as Pack>::SCHEMA_COLUMN_ADDITIONS
    }

    async fn dispatch(&self, verb: &str, params: Value, _registry: &VerbRegistry, token: &NamespaceToken) -> Result<Value, RuntimeError> {
        let rt = &self.runtime;
        match verb {
            "lion.session_open" => handlers::session_open(rt, token, params).await,
            "lion.session_close" => handlers::session_close(rt, token, params).await,
            "lion.take_begin" => handlers::take_begin(rt, token, params).await,
            "lion.message_append" => handlers::message_append(rt, token, params).await,
            "lion.message_append_carried" => handlers::message_append_carried(rt, token, params).await,
            "lion.call_open" => handlers::call_open(rt, token, params).await,
            "lion.call_close" => handlers::call_close(rt, token, params).await,
            "lion.run_open" => handlers::run_open(rt, token, params).await,
            "lion.run_close" => handlers::run_close(rt, token, params).await,
            "lion.get" => handlers::get(rt, token, params).await,
            "lion.messages" => handlers::messages(rt, token, params).await,
            "lion.runs" => handlers::runs(rt, token, params).await,
            "lion.sessions" => handlers::sessions(rt, token, params).await,
            "lion.note_put" => handlers::note_put(rt, token, params).await,
            "lion.note_get" => handlers::note_get(rt, token, params).await,
            "lion.note_list" => handlers::note_list(rt, token, params).await,
            "lion.note_find" => handlers::note_find(rt, token, params).await,
            "lion.note_delete" => handlers::note_delete(rt, token, params).await,
            _ => Err(RuntimeError::UnknownVerb(format!("{PACK_NAME} does not serve {verb:?}"))),
        }
    }
}
