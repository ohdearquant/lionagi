// Copyright (C) 2026 HaiyangLi
// SPDX-License-Identifier: Apache-2.0
//! Lion Step Host Call (EROS-style 6 operations)
//!
//! Corresponds to: Lion/Step/HostCall/Core.lean
//!
//! Host function calls that cross the trust boundary between
//! untrusted plugin code and the trusted kernel.
//!
//! EROS consolidation (13 -> 6):
//! - CapCall: Synchronous capability invocation (was cap_invoke)
//! - CapSend: Async message send (was ipc_send; delegation is user-space)
//! - KernelAlloc: Resource allocation (was mem_alloc, resource_create)
//! - MemFree: Memory deallocation (keep separate for safety)
//! - CapRevoke: Capability revocation
//! - Declassify: Security level change

use crate::state::{Message, State};
use crate::types::{Action, Capability, MemAddr, PluginId, Right, Rights, SecurityLevel, Size};

use super::{HostCallPrecondition, StepError};

/// All host functions that cross the trust boundary (EROS-style 6 operations)
///
/// Corresponds to Lean: `inductive HostFunctionId`
///
/// EROS consolidation (13 -> 6):
/// - CapInvoke -> CapCall (synchronous capability invocation)
/// - CapDelegate, CapAccept -> User-space protocol over CapSend
/// - IpcSend -> CapSend (async message send)
/// - IpcReceive -> Removed (mailbox read via CapCall)
/// - MemAlloc, ResourceCreate, ResourceAccess -> KernelAlloc
/// - MemFree -> MemFree (keep separate for safety)
/// - CapRevoke -> CapRevoke (kernel operation)
/// - WorkflowStart, WorkflowStep -> Removed (user-space library)
/// - Declassify -> Declassify (IFC operation)
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum HostFunction {
    /// Synchronous capability invocation (replaces cap_invoke)
    ///
    /// Corresponds to Lean: `| cap_call`
    CapCall,

    /// Async message send (replaces ipc_send; delegation is user-space protocol)
    ///
    /// Corresponds to Lean: `| cap_send`
    CapSend,

    /// Kernel resource allocation (replaces mem_alloc, resource_create)
    ///
    /// Corresponds to Lean: `| kernel_alloc`
    KernelAlloc,

    /// Memory deallocation (keep separate for safety)
    ///
    /// Corresponds to Lean: `| mem_free`
    MemFree,

    /// Capability revocation
    ///
    /// Corresponds to Lean: `| cap_revoke`
    CapRevoke,

    /// Controlled information declassification (IFC operation)
    ///
    /// Corresponds to Lean: `| declassify`
    Declassify,

    /// Create new thread (requires ThreadControl capability)
    ///
    /// Corresponds to Lean: `| thread_create`
    ThreadCreate,

    /// Configure thread bindings (requires ThreadControl capability)
    ///
    /// Corresponds to Lean: `| thread_configure`
    ThreadConfigure,

    /// Resume suspended thread (requires ThreadControl capability)
    ///
    /// Corresponds to Lean: `| thread_resume`
    ThreadResume,

    /// Suspend running thread (requires ThreadControl capability)
    ///
    /// Corresponds to Lean: `| thread_suspend`
    ThreadSuspend,

    /// Terminate thread (requires ThreadControl capability)
    ///
    /// Corresponds to Lean: `| thread_destroy`
    ThreadDestroy,
}

/// Resource type for KernelAlloc operation
///
/// Corresponds to Lean: `inductive ResourceType`
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum ResourceType {
    /// Allocate memory region
    Memory {
        /// Size of the memory region to allocate
        size: u64,
    },
    /// Create capability (simplified payload)
    Capability {
        /// Capability payload identifier
        payload: u64,
    },
    /// Create actor (simplified config)
    Actor {
        /// Actor configuration identifier
        config: u64,
    },
}

// ============== LEGACY COMPATIBILITY ALIASES ==============

impl HostFunction {
    /// Legacy alias: CapInvoke -> CapCall
    #[deprecated(note = "Use HostFunction::CapCall instead (EROS consolidation)")]
    pub const CAP_INVOKE: HostFunction = HostFunction::CapCall;

    /// Legacy alias: IpcSend -> CapSend
    #[deprecated(note = "Use HostFunction::CapSend instead (EROS consolidation)")]
    pub const IPC_SEND: HostFunction = HostFunction::CapSend;

    /// Legacy alias: MemAlloc -> KernelAlloc
    #[deprecated(note = "Use HostFunction::KernelAlloc instead (EROS consolidation)")]
    pub const MEM_ALLOC: HostFunction = HostFunction::KernelAlloc;
}

impl HostFunction {
    /// Check if this host function is effectful
    ///
    /// All host functions cross the trust boundary, so all are effectful.
    ///
    /// Corresponds to Lean: `def is_effectful_host_call`
    #[inline]
    pub fn is_effectful(&self) -> bool {
        true
    }

    /// Check if this is a declassify operation
    ///
    /// Corresponds to Lean: `def is_declassify`
    #[inline]
    pub fn is_declassify(&self) -> bool {
        matches!(self, HostFunction::Declassify)
    }

    /// Check if this is a capability operation (may modify kernel.revocation)
    ///
    /// Corresponds to Lean: `def is_cap_operation`
    #[inline]
    pub fn is_cap_operation(&self) -> bool {
        matches!(self, HostFunction::CapCall | HostFunction::CapRevoke)
    }

    /// Check if this is a memory operation (modifies ghost state)
    ///
    /// Corresponds to Lean: `def is_mem_operation`
    #[inline]
    pub fn is_mem_operation(&self) -> bool {
        matches!(self, HostFunction::KernelAlloc | HostFunction::MemFree)
    }

    /// Execute the host function
    ///
    /// Corresponds to Lean: `def KernelExecHost`
    ///
    /// # Errors
    ///
    /// Returns `StepError::PluginNotFound` if the caller plugin does not exist.
    /// Returns `StepError::HostCallPreconditionFailed` if function-specific preconditions are not met.
    /// Returns `StepError::ActorNotFound` if a destination actor does not exist (CapSend).
    /// Returns `StepError::QuotaExceeded` if the allocation would exceed the plugin's memory quota (KernelAlloc).
    /// Returns `StepError::AddressAlreadyFreed` if the address is already freed (MemFree).
    /// Returns `StepError::CapabilityTargetsAddress` if a valid capability targets the address (MemFree).
    /// Returns `StepError::CapabilityNotFound` if the capability does not exist (CapRevoke).
    pub fn execute(
        &self,
        state: &State,
        hc: &HostCall,
        result: &HostResult,
    ) -> Result<State, StepError> {
        match self {
            HostFunction::CapCall => execute_cap_call(state, hc, result),
            HostFunction::CapSend => execute_cap_send(state, hc, result),
            HostFunction::KernelAlloc => execute_kernel_alloc(state, hc),
            HostFunction::MemFree => execute_mem_free(state, hc),
            HostFunction::CapRevoke => execute_cap_revoke(state, hc),
            HostFunction::Declassify => execute_declassify(state, hc),
            // Thread operations — Phase 3 stubs (require State extension)
            HostFunction::ThreadCreate
            | HostFunction::ThreadConfigure
            | HostFunction::ThreadResume
            | HostFunction::ThreadSuspend
            | HostFunction::ThreadDestroy => Err(StepError::HostCallPreconditionFailed(
                HostCallPrecondition::NotImplemented {
                    operation: "thread operations",
                },
            )),
        }
    }

    /// Execute the host function (mutating version).
    ///
    /// Same validation as `execute` but modifies `&mut State` in place.
    pub fn execute_mut(
        &self,
        state: &mut State,
        hc: &HostCall,
        result: &HostResult,
    ) -> Result<(), StepError> {
        match self {
            HostFunction::CapCall => execute_cap_call_mut(state, hc, result),
            HostFunction::CapSend => execute_cap_send_mut(state, hc, result),
            HostFunction::KernelAlloc => execute_kernel_alloc_mut(state, hc),
            HostFunction::MemFree => execute_mem_free_mut(state, hc),
            HostFunction::CapRevoke => execute_cap_revoke_mut(state, hc),
            HostFunction::Declassify => execute_declassify_mut(state, hc),
            HostFunction::ThreadCreate
            | HostFunction::ThreadConfigure
            | HostFunction::ThreadResume
            | HostFunction::ThreadSuspend
            | HostFunction::ThreadDestroy => Err(StepError::HostCallPreconditionFailed(
                HostCallPrecondition::NotImplemented {
                    operation: "thread operations",
                },
            )),
        }
    }

    /// Check if this is a thread operation (requires ThreadControl capability)
    ///
    /// Corresponds to Lean: thread_create/configure/resume/suspend/destroy
    #[inline]
    pub fn is_thread_operation(&self) -> bool {
        matches!(
            self,
            HostFunction::ThreadCreate
                | HostFunction::ThreadConfigure
                | HostFunction::ThreadResume
                | HostFunction::ThreadSuspend
                | HostFunction::ThreadDestroy
        )
    }
}

/// Host call request from plugin
///
/// Corresponds to Lean: `structure HostCall`
#[derive(Debug, Clone)]
pub struct HostCall {
    /// The plugin making the call
    ///
    /// Corresponds to Lean: `caller : PluginId`
    pub(crate) caller: PluginId,

    /// The function to invoke
    ///
    /// Corresponds to Lean: `function : HostFunctionId`
    pub(crate) function: HostFunction,

    /// Arguments to the function (simplified from WasmValue)
    ///
    /// Corresponds to Lean: `args : List Nat`
    pub(crate) args: Vec<u64>,

    /// Memory regions to read
    ///
    /// Corresponds to Lean: `reads : List (MemAddr x Size)`
    pub(crate) reads: Vec<(MemAddr, Size)>,

    /// Memory regions to write
    ///
    /// Corresponds to Lean: `writes : List (MemAddr x Size)`
    pub(crate) writes: Vec<(MemAddr, Size)>,
}

impl HostCall {
    /// Create a new host call
    pub fn new(
        caller: PluginId,
        function: HostFunction,
        args: Vec<u64>,
        reads: Vec<(MemAddr, Size)>,
        writes: Vec<(MemAddr, Size)>,
    ) -> Self {
        HostCall {
            caller,
            function,
            args,
            reads,
            writes,
        }
    }

    /// Get the caller plugin ID
    #[inline]
    pub fn caller(&self) -> PluginId {
        self.caller
    }

    /// Get the host function to invoke
    #[inline]
    pub fn function(&self) -> HostFunction {
        self.function
    }

    /// Get the arguments
    #[inline]
    pub fn args(&self) -> &[u64] {
        &self.args
    }

    /// Get the read regions
    #[inline]
    pub fn reads(&self) -> &[(MemAddr, Size)] {
        &self.reads
    }

    /// Get the write regions
    #[inline]
    pub fn writes(&self) -> &[(MemAddr, Size)] {
        &self.writes
    }

    /// Derive the required authorization action from the host call semantics.
    ///
    /// This binds authorization to the exact operation being performed,
    /// preventing the vulnerability where a valid `Authorized` token for one
    /// action could be reused to execute a different `HostCall`.
    ///
    /// # Errors
    ///
    /// Returns `StepError::HostCallPreconditionFailed` if required arguments are missing
    /// or the operation is not yet implemented (thread operations).
    pub fn required_action(&self) -> Result<Action, StepError> {
        // Helper to convert PolicyError to StepError
        let map_policy_err =
            |_| StepError::HostCallPreconditionFailed(HostCallPrecondition::NoCapabilityId);

        match self.function {
            HostFunction::CapCall => {
                let target: u64 =
                    self.args
                        .first()
                        .copied()
                        .ok_or(StepError::HostCallPreconditionFailed(
                            HostCallPrecondition::NoCapabilityId,
                        ))?;

                Action::new(
                    self.caller,
                    target.into(),
                    Rights::singleton(Right::Read),
                    "cap_call".to_string(),
                )
                .map_err(map_policy_err)
            }
            HostFunction::KernelAlloc => Action::new(
                self.caller,
                0,
                Rights::singleton(Right::Create),
                "kernel_alloc".to_string(),
            )
            .map_err(map_policy_err),
            HostFunction::MemFree => {
                let addr: u64 =
                    self.args
                        .first()
                        .copied()
                        .ok_or(StepError::HostCallPreconditionFailed(
                            HostCallPrecondition::NoAddress,
                        ))?;

                Action::new(
                    self.caller,
                    addr.into(),
                    Rights::singleton(Right::Delete),
                    "mem_free".to_string(),
                )
                .map_err(map_policy_err)
            }
            HostFunction::CapRevoke => {
                let cap_id: u64 =
                    self.args
                        .first()
                        .copied()
                        .ok_or(StepError::HostCallPreconditionFailed(
                            HostCallPrecondition::NoCapabilityId,
                        ))?;

                Action::new(
                    self.caller,
                    cap_id.into(),
                    Rights::singleton(Right::Revoke),
                    "cap_revoke".to_string(),
                )
                .map_err(map_policy_err)
            }
            HostFunction::Declassify => Action::new(
                self.caller,
                0,
                Rights::singleton(Right::Declassify),
                "declassify".to_string(),
            )
            .map_err(map_policy_err),
            HostFunction::CapSend => Action::new(
                self.caller,
                0,
                Rights::singleton(Right::Send),
                "cap_send".to_string(),
            )
            .map_err(map_policy_err),
            _ => Err(StepError::HostCallPreconditionFailed(
                HostCallPrecondition::NotImplemented {
                    operation: "thread operations",
                },
            )),
        }
    }

    /// Check preconditions for the host call
    ///
    /// Corresponds to Lean: `def host_call_pre`
    ///
    /// # Errors
    ///
    /// Returns `StepError::PluginNotFound` if the caller plugin does not exist.
    /// Returns `StepError::HostCallPreconditionFailed` if a read or write region is out of bounds.
    pub fn check_preconditions(&self, state: &State) -> Result<(), StepError> {
        // Get the caller plugin
        let plugin = state
            .get_plugin(self.caller)
            .ok_or(StepError::PluginNotFound(self.caller))?;

        // Check read regions are in bounds
        if let Some(err) = self.find_oob_read(plugin) {
            return Err(err);
        }

        // Check write regions are in bounds
        if let Some(err) = self.find_oob_write(plugin) {
            return Err(err);
        }

        Ok(())
    }

    /// Helper: find first out-of-bounds read region
    fn find_oob_read(&self, plugin: &crate::state::PluginState) -> Option<StepError> {
        self.reads.iter().find_map(|&(addr, size)| {
            if plugin.memory().addr_in_bounds(addr, size) {
                None
            } else {
                Some(StepError::HostCallPreconditionFailed(
                    HostCallPrecondition::ReadOutOfBounds { addr, size },
                ))
            }
        })
    }

    /// Helper: find first out-of-bounds write region
    fn find_oob_write(&self, plugin: &crate::state::PluginState) -> Option<StepError> {
        self.writes.iter().find_map(|&(addr, size)| {
            if plugin.memory().addr_in_bounds(addr, size) {
                None
            } else {
                Some(StepError::HostCallPreconditionFailed(
                    HostCallPrecondition::WriteOutOfBounds { addr, size },
                ))
            }
        })
    }
}

/// Result of host call execution
///
/// Corresponds to Lean: `structure HostResult`
#[derive(Debug, Clone, Default)]
#[must_use = "host call results must be consumed"]
pub struct HostResult {
    /// Capabilities created/delegated
    ///
    /// Corresponds to Lean: `newCaps : List Capability`
    pub(crate) new_caps: Vec<Capability>,

    /// Messages queued
    ///
    /// Corresponds to Lean: `newMsgs : List Message`
    pub(crate) new_msgs: Vec<Message>,
}

impl HostResult {
    /// Create an empty result
    pub fn empty() -> Self {
        HostResult {
            new_caps: Vec::new(),
            new_msgs: Vec::new(),
        }
    }

    /// Create a result with new capabilities
    pub fn with_caps(caps: Vec<Capability>) -> Self {
        HostResult {
            new_caps: caps,
            new_msgs: Vec::new(),
        }
    }

    /// Create a result with new messages
    pub fn with_msgs(msgs: Vec<Message>) -> Self {
        HostResult {
            new_caps: Vec::new(),
            new_msgs: msgs,
        }
    }

    /// Get new capabilities
    #[inline]
    pub fn new_caps(&self) -> &[Capability] {
        &self.new_caps
    }

    /// Get new messages
    #[inline]
    pub fn new_msgs(&self) -> &[Message] {
        &self.new_msgs
    }
}

// ============== HOST FUNCTION EXECUTION ==============

/// Execute cap_call (was cap_invoke)
///
/// Synchronous capability invocation: validate and use capability.
/// State unchanged (call just validates and uses the cap).
///
/// PERF NOTE: Returns `state.clone()` because the API takes `&State` (callers
/// need to retain the original for error paths and multi-step sequences).
/// The clone is architecturally necessary for the immutable-state-transition
/// model that mirrors the Lean spec (`Step s s'`). A by-value `State` API
/// would eliminate this clone but would require the caller to re-create state
/// on error paths, trading clone cost for ergonomic complexity.
fn execute_cap_call(
    state: &State,
    hc: &HostCall,
    _result: &HostResult,
) -> Result<State, StepError> {
    // Check caller holds a valid capability
    let plugin = state
        .get_plugin(hc.caller)
        .ok_or(StepError::PluginNotFound(hc.caller))?;

    // At least one held cap must be valid
    let has_valid_cap = plugin
        .held_caps_ref()
        .iter()
        .any(|&id| state.kernel().revocation().is_valid(id));

    if !has_valid_cap {
        return Err(StepError::HostCallPreconditionFailed(
            HostCallPrecondition::NoValidCapability,
        ));
    }

    // State unchanged for call — clone required by &State API contract
    Ok(state.clone())
}

/// Execute cap_send (was ipc_send)
///
/// Async message send. Capability delegation is now user-space protocol:
/// 1. Holder creates delegation message with cap payload
/// 2. Sends via cap_send to delegatee
/// 3. Delegatee receives and calls kernel_alloc to register
///
/// PERF NOTE: See `execute_cap_call` for why `state.clone()` is required here.
/// State is unchanged — the kernel delivers messages asynchronously.
fn execute_cap_send(state: &State, hc: &HostCall, result: &HostResult) -> Result<State, StepError> {
    // Check message in result
    if result.new_msgs.is_empty() {
        return Err(StepError::HostCallPreconditionFailed(
            HostCallPrecondition::NoMessage,
        ));
    }

    // Validate message source matches caller
    for msg in &result.new_msgs {
        if msg.src() != hc.caller {
            return Err(StepError::HostCallPreconditionFailed(
                HostCallPrecondition::SourceMismatch,
            ));
        }

        // Check destination actor exists and has capacity
        let dst_actor = state
            .get_actor(msg.dst())
            .ok_or(StepError::ActorNotFound(msg.dst()))?;

        if dst_actor.mailbox_len() >= dst_actor.capacity() {
            return Err(StepError::HostCallPreconditionFailed(
                HostCallPrecondition::MailboxFull,
            ));
        }
    }

    // State unchanged — clone required by &State API contract
    Ok(state.clone())
}

/// Execute kernel_alloc (was mem_alloc, resource_create)
///
/// Unified kernel resource allocation.
/// Enforces memory quota to prevent DoS attacks.
fn execute_kernel_alloc(state: &State, hc: &HostCall) -> Result<State, StepError> {
    // Get size from args (first arg is size for memory allocation)
    let size = hc.args.first().copied().unwrap_or(0);

    // QUOTA CHECK: Verify memory quota not exceeded
    let plugin = state
        .get_plugin(hc.caller)
        .ok_or(StepError::PluginNotFound(hc.caller))?;

    if !plugin.can_alloc_memory(size) {
        return Err(StepError::QuotaExceeded(
            0, // memory
            size,
            plugin.memory_used(),
            plugin.memory_quota(),
        ));
    }

    // Apply allocation and update quota tracking
    let mut new_state = state.apply_alloc(hc.caller, size);

    // Update memory used counter in plugin
    if let Some(p) = new_state.get_plugin_mut(hc.caller) {
        p.memory_used = p.memory_used.saturating_add(size);
    }

    Ok(new_state)
}

/// Execute mem_free
///
/// Free kernel-managed memory.
fn execute_mem_free(state: &State, hc: &HostCall) -> Result<State, StepError> {
    // Get addr from args
    let addr: MemAddr = hc
        .args
        .first()
        .copied()
        .ok_or(StepError::HostCallPreconditionFailed(
            HostCallPrecondition::NoAddress,
        ))?;

    // Check not already freed
    if state.ghost().is_freed(addr) {
        return Err(StepError::AddressAlreadyFreed(addr));
    }

    // Check no valid capability targets addr (ResourceId = u128, addr = u64)
    if let Some(&(cap_id, _)) = state
        .kernel()
        .revocation()
        .iter()
        .find(|(_, cap)| cap.is_valid() && cap.target() == u128::from(addr))
    {
        return Err(StepError::CapabilityTargetsAddress(cap_id, addr));
    }

    state.apply_free(addr).map_err(|e| {
        StepError::HostCallPreconditionFailed(HostCallPrecondition::FreeFailed { source: e })
    })
}

/// Execute cap_revoke
///
/// Revoke capability transitively.
fn execute_cap_revoke(state: &State, hc: &HostCall) -> Result<State, StepError> {
    // Get the cap_id from args (stored as u64, widened to CapId = u128)
    let cap_id: crate::types::CapId =
        hc.args
            .first()
            .copied()
            .map(u128::from)
            .ok_or(StepError::HostCallPreconditionFailed(
                HostCallPrecondition::NoCapabilityId,
            ))?;

    // Check cap exists
    if state.kernel().revocation().get(cap_id).is_none() {
        return Err(StepError::CapabilityNotFound(cap_id));
    }

    // Apply transitive revocation
    Ok(state.apply_cap_revoke(cap_id))
}

/// Execute declassify
///
/// Controlled information declassification.
fn execute_declassify(state: &State, hc: &HostCall) -> Result<State, StepError> {
    // Get new level from args (encoded as u64)
    let new_level_val = hc.args.first().copied().unwrap_or(0);
    let new_level = match new_level_val {
        0 => SecurityLevel::Public,
        1 => SecurityLevel::Internal,
        2 => SecurityLevel::Confidential,
        3 => SecurityLevel::Secret,
        other => {
            return Err(StepError::HostCallPreconditionFailed(
                HostCallPrecondition::InvalidSecurityLevel { value: other },
            ));
        }
    };

    // Check new level <= current level (can only declassify down)
    let plugin = state
        .get_plugin(hc.caller)
        .ok_or(StepError::PluginNotFound(hc.caller))?;

    if new_level > plugin.level() {
        return Err(StepError::HostCallPreconditionFailed(
            HostCallPrecondition::CannotClassifyUp,
        ));
    }

    // Update plugin level
    let mut new_state = state.clone();
    if let Some(plugin) = new_state.get_plugin_mut(hc.caller) {
        plugin.level = new_level;
    }

    Ok(new_state)
}

// ============== MUTATING HOST FUNCTION VARIANTS ==============

/// Execute cap_call (mutating) -- state-unchanged operation
fn execute_cap_call_mut(
    state: &mut State,
    hc: &HostCall,
    _result: &HostResult,
) -> Result<(), StepError> {
    let plugin = state
        .get_plugin(hc.caller)
        .ok_or(StepError::PluginNotFound(hc.caller))?;

    let has_valid_cap = plugin
        .held_caps_ref()
        .iter()
        .any(|&id| state.kernel().revocation().is_valid(id));

    if !has_valid_cap {
        return Err(StepError::HostCallPreconditionFailed(
            HostCallPrecondition::NoValidCapability,
        ));
    }

    // State unchanged for call
    Ok(())
}

/// Execute cap_send (mutating) -- state-unchanged operation
fn execute_cap_send_mut(
    state: &mut State,
    hc: &HostCall,
    result: &HostResult,
) -> Result<(), StepError> {
    if result.new_msgs.is_empty() {
        return Err(StepError::HostCallPreconditionFailed(
            HostCallPrecondition::NoMessage,
        ));
    }

    for msg in &result.new_msgs {
        if msg.src() != hc.caller {
            return Err(StepError::HostCallPreconditionFailed(
                HostCallPrecondition::SourceMismatch,
            ));
        }

        let dst_actor = state
            .get_actor(msg.dst())
            .ok_or(StepError::ActorNotFound(msg.dst()))?;

        if dst_actor.mailbox_len() >= dst_actor.capacity() {
            return Err(StepError::HostCallPreconditionFailed(
                HostCallPrecondition::MailboxFull,
            ));
        }
    }

    // State unchanged
    Ok(())
}

/// Execute kernel_alloc (mutating)
fn execute_kernel_alloc_mut(state: &mut State, hc: &HostCall) -> Result<(), StepError> {
    let size = hc.args.first().copied().unwrap_or(0);

    let plugin = state
        .get_plugin(hc.caller)
        .ok_or(StepError::PluginNotFound(hc.caller))?;

    if !plugin.can_alloc_memory(size) {
        return Err(StepError::QuotaExceeded(
            0, // memory
            size,
            plugin.memory_used(),
            plugin.memory_quota(),
        ));
    }

    // Apply allocation in place
    let _addr = state.apply_alloc_mut(hc.caller, size);

    // Update memory used counter in plugin
    if let Some(p) = state.get_plugin_mut(hc.caller) {
        p.memory_used = p.memory_used.saturating_add(size);
    }

    Ok(())
}

/// Execute mem_free (mutating)
fn execute_mem_free_mut(state: &mut State, hc: &HostCall) -> Result<(), StepError> {
    let addr: MemAddr = hc
        .args
        .first()
        .copied()
        .ok_or(StepError::HostCallPreconditionFailed(
            HostCallPrecondition::NoAddress,
        ))?;

    if state.ghost().is_freed(addr) {
        return Err(StepError::AddressAlreadyFreed(addr));
    }

    // ResourceId = u128, addr = u64
    if let Some(&(cap_id, _)) = state
        .kernel()
        .revocation()
        .iter()
        .find(|(_, cap)| cap.is_valid() && cap.target() == u128::from(addr))
    {
        return Err(StepError::CapabilityTargetsAddress(cap_id, addr));
    }

    state.apply_free_mut(addr).map_err(|e| {
        StepError::HostCallPreconditionFailed(HostCallPrecondition::FreeFailed { source: e })
    })
}

/// Execute cap_revoke (mutating)
fn execute_cap_revoke_mut(state: &mut State, hc: &HostCall) -> Result<(), StepError> {
    // Stored as u64, widened to CapId = u128
    let cap_id: crate::types::CapId =
        hc.args
            .first()
            .copied()
            .map(u128::from)
            .ok_or(StepError::HostCallPreconditionFailed(
                HostCallPrecondition::NoCapabilityId,
            ))?;

    if state.kernel().revocation().get(cap_id).is_none() {
        return Err(StepError::CapabilityNotFound(cap_id));
    }

    state
        .apply_cap_revoke_mut(cap_id)
        .map_err(|_| StepError::CapabilityNotFound(cap_id))?;

    Ok(())
}

/// Execute declassify (mutating)
fn execute_declassify_mut(state: &mut State, hc: &HostCall) -> Result<(), StepError> {
    let new_level_val = hc.args.first().copied().unwrap_or(0);
    let new_level = match new_level_val {
        0 => SecurityLevel::Public,
        1 => SecurityLevel::Internal,
        2 => SecurityLevel::Confidential,
        3 => SecurityLevel::Secret,
        other => {
            return Err(StepError::HostCallPreconditionFailed(
                HostCallPrecondition::InvalidSecurityLevel { value: other },
            ));
        }
    };

    let plugin = state
        .get_plugin(hc.caller)
        .ok_or(StepError::PluginNotFound(hc.caller))?;

    if new_level > plugin.level() {
        return Err(StepError::HostCallPreconditionFailed(
            HostCallPrecondition::CannotClassifyUp,
        ));
    }

    if let Some(plugin) = state.get_plugin_mut(hc.caller) {
        plugin.level = new_level;
    }

    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::state::PluginState;

    #[test]
    fn test_host_function_is_effectful() {
        // All host functions are effectful
        assert!(HostFunction::CapCall.is_effectful());
        assert!(HostFunction::KernelAlloc.is_effectful());
        assert!(HostFunction::Declassify.is_effectful());
    }

    #[test]
    fn test_host_function_is_declassify() {
        assert!(!HostFunction::CapCall.is_declassify());
        assert!(!HostFunction::KernelAlloc.is_declassify());
        assert!(HostFunction::Declassify.is_declassify());
    }

    #[test]
    fn test_host_function_is_cap_operation() {
        assert!(HostFunction::CapCall.is_cap_operation());
        assert!(HostFunction::CapRevoke.is_cap_operation());
        assert!(!HostFunction::CapSend.is_cap_operation());
        assert!(!HostFunction::KernelAlloc.is_cap_operation());
    }

    #[test]
    fn test_host_function_is_mem_operation() {
        assert!(HostFunction::KernelAlloc.is_mem_operation());
        assert!(HostFunction::MemFree.is_mem_operation());
        assert!(!HostFunction::CapCall.is_mem_operation());
    }

    #[test]
    fn test_host_call_check_preconditions_no_plugin() {
        let state = State::empty();
        let hc = HostCall::new(1, HostFunction::CapCall, vec![], vec![], vec![]);

        let result = hc.check_preconditions(&state);
        assert!(matches!(result, Err(StepError::PluginNotFound(1))));
    }

    #[test]
    fn test_host_call_check_preconditions_out_of_bounds() {
        let mut state = State::empty();
        let _ = state.insert_plugin(1, PluginState::empty(SecurityLevel::Public, 100));

        // Read region beyond bounds
        let hc = HostCall::new(1, HostFunction::CapCall, vec![], vec![(200, 50)], vec![]);

        let result = hc.check_preconditions(&state);
        assert!(matches!(
            result,
            Err(StepError::HostCallPreconditionFailed(_))
        ));
    }

    #[test]
    fn test_execute_kernel_alloc() {
        let mut state = State::empty();
        let _ = state.insert_plugin(1, PluginState::empty(SecurityLevel::Public, 1024));

        let hc = HostCall::new(1, HostFunction::KernelAlloc, vec![256], vec![], vec![]);
        let result = HostResult::empty();

        let new_state = HostFunction::KernelAlloc
            .execute(&state, &hc, &result)
            .expect("kernel_alloc should succeed");

        // Ghost state should show allocation
        assert!(new_state.ghost().resource_count() > state.ghost().resource_count());
    }

    #[test]
    fn test_execute_declassify() {
        let mut state = State::empty();
        let _ = state.insert_plugin(1, PluginState::empty(SecurityLevel::Secret, 0));

        // Declassify to Public (level 0)
        let hc = HostCall::new(1, HostFunction::Declassify, vec![0], vec![], vec![]);
        let result = HostResult::empty();

        let new_state = HostFunction::Declassify
            .execute(&state, &hc, &result)
            .expect("declassify should succeed");

        assert_eq!(new_state.plugin_level(1), Some(SecurityLevel::Public));
    }

    #[test]
    fn test_execute_declassify_up_fails() {
        let mut state = State::empty();
        let _ = state.insert_plugin(1, PluginState::empty(SecurityLevel::Public, 0));

        // Try to classify up to Secret (level 3) - should fail
        let hc = HostCall::new(1, HostFunction::Declassify, vec![3], vec![], vec![]);
        let result = HostResult::empty();

        let err = HostFunction::Declassify
            .execute(&state, &hc, &result)
            .expect_err("classify up should fail");

        assert!(matches!(err, StepError::HostCallPreconditionFailed(_)));
    }

    #[test]
    fn test_execute_declassify_invalid_level_rejected() {
        // SEC-002: values >= 4 must be rejected, not silently mapped to Secret
        let mut state = State::empty();
        let _ = state.insert_plugin(1, PluginState::empty(SecurityLevel::Secret, 0));

        for invalid_level in [4, 5, 100, u64::MAX] {
            let hc = HostCall::new(
                1,
                HostFunction::Declassify,
                vec![invalid_level],
                vec![],
                vec![],
            );
            let result = HostResult::empty();

            let err = HostFunction::Declassify
                .execute(&state, &hc, &result)
                .expect_err(&format!("level {invalid_level} should be rejected"));

            assert!(
                matches!(
                    err,
                    StepError::HostCallPreconditionFailed(
                        HostCallPrecondition::InvalidSecurityLevel { .. }
                    )
                ),
                "level {invalid_level}: expected InvalidSecurityLevel error, got {err:?}"
            );
        }
    }

    #[test]
    fn test_execute_declassify_all_valid_levels() {
        // All 4 valid levels (0-3) should work for a Secret plugin declassifying down
        let mut state = State::empty();
        let _ = state.insert_plugin(1, PluginState::empty(SecurityLevel::Secret, 0));

        let expected = [
            (0, SecurityLevel::Public),
            (1, SecurityLevel::Internal),
            (2, SecurityLevel::Confidential),
            (3, SecurityLevel::Secret),
        ];

        for (level_val, expected_level) in expected {
            let hc = HostCall::new(1, HostFunction::Declassify, vec![level_val], vec![], vec![]);
            let result = HostResult::empty();

            let new_state = HostFunction::Declassify
                .execute(&state, &hc, &result)
                .unwrap_or_else(|e| panic!("level {level_val} should succeed: {e:?}"));

            assert_eq!(new_state.plugin_level(1), Some(expected_level));
        }
    }
}
