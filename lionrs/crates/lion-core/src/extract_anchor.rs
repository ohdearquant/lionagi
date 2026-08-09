// Copyright (C) 2026 HaiyangLi
// SPDX-License-Identifier: Apache-2.0
//! Extraction Anchor Module
//!
//! This module exists ONLY for styx-rustc extraction. It defines an "export anchor"
//! function whose type signature references exactly the types we want extracted to Lean.
//!
//! ## How It Works
//!
//! styx-rustc's `start_from` field makes extraction reachability-driven:
//! - Only types referenced by the anchor function are translated
//! - Derived trait impls that aren't referenced stay out of the translation
//! - No need for hundreds of exclude rules
//!
//! ## Usage
//!
//! ```bash
//! STYX_RUSTC_ARGS='{"output":"lion-core.styx","start_from":"extract_anchor::export_types"}' \
//!     RUSTC_WORKSPACE_WRAPPER=path/to/styx-rustc \
//!     RUSTFLAGS='--cfg extract' \
//!     cargo +nightly-2025-11-23 check
//! ```
//!
//! ## Adding New Types
//!
//! To extract additional types, add them as parameters to `export_types`.
//! The function body doesn't matter - only the signature.
//!
//! ## Avoiding Field Collisions
//!
//! Types with accessor methods (e.g., `fn holder(&self) -> T`) will cause
//! "invalid declaration name" errors because Lean auto-generates field projections.
//! Either:
//! 1. Don't export those types (treat as opaque in manual Lean specs)
//! 2. Rename Rust accessors under `#[cfg(extract)]` to avoid collision
//!
//! ## Current Safe Types
//!
//! - `SecurityLevel` - enum, no problematic derives
//! - `Key` - struct, no Hash derive
//! - `SealedTag` - struct, simple
//! - `Hash32` - struct, simple
//! - `PolicyState` / `PolicyDecisionFn` - enum-backed policy selectors

// Use crate-level re-exports (the submodules are private)
use crate::{
    // Kernel state core (hybrid kernel architecture)
    state::{
        kernel_dispatch, KernelError, KernelRequest, KernelResult, KernelStateCore, PluginMeta,
        ResourceMeta,
    },
    // Actor and message types
    state::{ActorError, ActorRuntime, Message},
    // Thread and scheduler types (Issue #111)
    state::{
        DomainScheduleEntry,
        FaultType,
        IPCBlockReason,
        IdleThreadEntry,
        KernelEntryReason,
        ReadyQueueEntry,
        Registers,
        ReleaseQueueEntry,
        SchedulerAction,
        SchedulerState,
        ThreadState,
        // ThreadTable and ThreadTableEntry excluded - cause Lean universe constraint issues
        // due to Vec<ThreadTableEntry> where ThreadTableEntry contains deeply nested TCB
        TCB,
    },
    // Workflow types (Issue #110: WorkflowDef and WorkflowInstance now extractable)
    state::{
        Edge, NodeState, NodeStateEntry, RetryEntry, WorkflowDef, WorkflowError, WorkflowInstance,
        WorkflowStatus,
    },
    // Memory and plugin error types (NEW - expanded extraction)
    state::{MemoryError, PluginError, PluginLocal, ResourceStatus},
    // State-level types (NEW - expanded extraction)
    state::{ResourceInfo, StateError},
    // Step types (host functions, calls, kernel ops, authorization errors)
    step::{AuthorizationError, Authorized, HostCall, HostFunction, HostResult, KernelOp},
    // Step data types (NEW - expanded extraction)
    step::{PluginInternal, ResourceType, Step},
    // Policy types
    Action,
    // Capability/crypto types
    Blake3Hash,
    CapPayload,
    Capability,
    CapabilityError,
    Hash32,
    Key,
    LogEvent,
    // Runtime types
    MemRegion,
    MsgState,
    PolicyContext,
    PolicyDecision,
    PolicyDecisionFn,
    PolicyError,
    PolicyState,
    // Rights types
    Right,
    Rights,
    RightsError,
    SealedTag,
    // Security types
    SecurityLevel,
    SymbolicTag,
};

/// Export anchor function for styx-rustc extraction.
///
/// The types in this signature are exactly what gets translated to Lean.
/// The function body is irrelevant - styx-rustc only cares about reachability.
///
/// ## Types NOT exported (require manual Lean specs):
/// - `State` - complex state machine (KernelState uses BTreeMap, MetaState uses BTreeMap)
/// - `PluginState` - blocked by LinearMemory (BTreeMap<MemAddr, u8>)
/// - `StepError` - uses String type (styx cannot extract alloc::string::String)
#[allow(dead_code)]
#[allow(clippy::needless_pass_by_value)]
pub fn export_types(
    // Core security type
    _security_level: SecurityLevel,
    // Cryptographic primitives
    _key: Key,
    _sealed_tag: SealedTag,
    _hash32: Hash32,
    _blake3: Blake3Hash,
    _symbolic: SymbolicTag,
    // Capability types
    _cap_payload: CapPayload,
    _capability: Capability,
    _log_event: LogEvent,
    _cap_error: CapabilityError,
    // Rights types
    _right: Right,
    _rights: Rights,
    _rights_error: RightsError,
    // Runtime types
    _mem_region: MemRegion,
    _msg_state: MsgState,
    // Policy types
    _action: Action,
    _policy_ctx: PolicyContext,
    _policy_decision: PolicyDecision,
    _policy_decision_fn: PolicyDecisionFn,
    _policy_state: PolicyState,
    // Kernel state core (hybrid kernel architecture)
    _kernel_state_core: KernelStateCore,
    _plugin_meta: PluginMeta,
    _resource_meta: ResourceMeta,
    _kernel_request: KernelRequest,
    _kernel_error: KernelError,
    // Actor and message types (NEW)
    _message: Message,
    _actor_error: ActorError,
    // Step types (NEW)
    _host_function: HostFunction,
    _host_call: HostCall,
    _host_result: HostResult,
    // Workflow types (WorkflowDef + WorkflowInstance newly extractable - Issue #110)
    _workflow_status: WorkflowStatus,
    _node_state: NodeState,
    _edge: Edge,
    _node_state_entry: NodeStateEntry,
    _retry_entry: RetryEntry,
    _workflow_def: WorkflowDef,
    _workflow_instance: WorkflowInstance,
    // Policy error (NEW)
    _policy_error: PolicyError,
    // Kernel operations (NEW - Issue #108)
    _kernel_op: KernelOp,
    // Authorization errors (NEW - Issue #109)
    _auth_error: AuthorizationError,
    // Actor runtime (NEW - Issue #112)
    _actor_runtime: ActorRuntime,
    // Thread/scheduler types (NEW - Issue #111)
    _thread_state: ThreadState,
    _ipc_block_reason: IPCBlockReason,
    _fault_type: FaultType,
    _registers: Registers,
    _tcb: TCB,
    _domain_schedule_entry: DomainScheduleEntry,
    _scheduler_action: SchedulerAction,
    _kernel_entry_reason: KernelEntryReason,
    _ready_queue_entry: ReadyQueueEntry,
    _release_queue_entry: ReleaseQueueEntry,
    _idle_thread_entry: IdleThreadEntry,
    _scheduler_state: SchedulerState,
    // Error types (NEW - expanded extraction)
    _memory_error: MemoryError,
    _plugin_error: PluginError,
    _state_error: StateError,
    _workflow_error: WorkflowError,
    // State data types (NEW - expanded extraction)
    _resource_status: ResourceStatus,
    _plugin_local: PluginLocal,
    _resource_info: ResourceInfo,
    // Step data types (NEW - expanded extraction)
    _resource_type: ResourceType,
    _plugin_internal: PluginInternal,
    _authorized: Authorized,
    _step: Step,
) {
    // Body intentionally empty - only signature matters for extraction
}

/// Function export anchor for Rights operations.
///
/// Unlike `export_types` which only exports type definitions via signature,
/// this function CALLS methods to trigger their extraction to Lean.
#[allow(dead_code)]
pub fn export_rights_functions(
    rights: Rights,
    right: Right,
    other_rights: Rights,
) -> (bool, bool, bool, Rights) {
    // Call methods to trigger extraction
    let is_empty = rights.is_empty();
    let contains = rights.contains(right);
    let is_subset = rights.is_subset_of(&other_rights);
    let combined = rights.combine(&other_rights);

    (is_empty, contains, is_subset, combined)
}

/// Function export anchor for Right enum operations.
#[allow(dead_code)]
pub fn export_right_functions(right: Right) -> (u8, Option<Right>) {
    let to_u8 = right.to_u8();
    let from_u8 = Right::from_u8(to_u8);
    (to_u8, from_u8)
}

/// Function export anchor for SecurityLevel operations.
#[allow(dead_code)]
pub fn export_security_level_functions(level: SecurityLevel) -> u8 {
    level.to_u8()
}

/// Function export anchor for Capability operations.
///
/// NOTE: Accessor methods (id, holder, target, rights, parent, epoch, signature)
/// are NOT exported because they collide with Lean's auto-generated field projections.
/// Use field access directly in Lean proofs.
#[allow(dead_code)]
pub fn export_capability_functions(cap: Capability, right: Right) -> (CapPayload, bool) {
    // Extract payload (pure construction)
    let payload = cap.payload();

    // Check if capability has a specific right
    let has_right = cap.has_right(right);

    (payload, has_right)
}

/// Function export anchor for Hash32 operations.
#[allow(dead_code)]
pub fn export_hash_functions(hash: Hash32) -> bool {
    hash.is_zero()
}

/// Function export anchor for Key operations.
#[allow(dead_code)]
pub fn export_key_functions(key: Key) -> bool {
    key.is_empty()
}

/// Function export anchor for kernel dispatch operations.
///
/// This is THE entry point for all kernel mutations. Extracting this
/// enables full verification of the kernel's security-critical paths.
///
/// ## Functions Extracted
/// - `kernel_dispatch` - Entry point
/// - `dispatch_cap_delegate` - Capability creation
/// - `dispatch_cap_revoke` - Revocation
/// - `dispatch_set_plugin_level` - Security labels
/// - `dispatch_set_resource_level` - Security labels
/// - `dispatch_tick` - Time advancement
/// - `dispatch_rotate_key` - Key rotation
#[allow(dead_code)]
pub fn export_kernel_dispatch_functions(core: KernelStateCore, req: KernelRequest) -> KernelResult {
    kernel_dispatch(core, req)
}
