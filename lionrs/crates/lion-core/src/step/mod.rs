// Copyright (C) 2026 HaiyangLi
// SPDX-License-Identifier: Apache-2.0
//! Lion Step Module
//!
//! Step relation and all step constructors for the Lion microkernel state machine.

mod authorization;
mod host_call;
mod kernel_op;
mod plugin_internal;

pub use authorization::{AuthorizationError, Authorized};
pub use host_call::{HostCall, HostFunction, HostResult, ResourceType};
pub use kernel_op::KernelOp;
pub use plugin_internal::PluginInternal;

use crate::state::{State, StateError};
use crate::types::{ActorId, CapId, MemAddr, PluginId, SecurityLevel, Size, WorkflowId};

/// Reasons a plugin precondition check can fail.
#[derive(Debug)]
pub enum PluginPrecondition {
    /// Plugin's actor is blocked on another actor
    Blocked {
        /// The plugin that is blocked
        pid: PluginId,
        /// The actor it is blocked on (if known)
        blocked_on: Option<ActorId>,
    },
    /// A read memory region is out of bounds
    ReadOutOfBounds {
        /// Start address of the region
        addr: MemAddr,
        /// Size of the region
        size: Size,
        /// Memory bounds of the plugin
        bounds: Size,
    },
    /// A write memory region is out of bounds
    WriteOutOfBounds {
        /// Start address of the region
        addr: MemAddr,
        /// Size of the region
        size: Size,
        /// Memory bounds of the plugin
        bounds: Size,
    },
    /// Attempted to consume from an empty mailbox
    MailboxEmpty,
}

impl std::fmt::Display for PluginPrecondition {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            PluginPrecondition::Blocked { pid, blocked_on } => {
                write!(f, "Plugin {pid} is blocked on {blocked_on:?}")
            }
            PluginPrecondition::ReadOutOfBounds { addr, size, bounds } => {
                write!(
                    f,
                    "Read region {addr}+{size} out of bounds (memory size {bounds})"
                )
            }
            PluginPrecondition::WriteOutOfBounds { addr, size, bounds } => {
                write!(
                    f,
                    "Write region {addr}+{size} out of bounds (memory size {bounds})"
                )
            }
            PluginPrecondition::MailboxEmpty => {
                write!(f, "Cannot consume mailbox: mailbox is empty")
            }
        }
    }
}

/// Reasons a host call precondition check can fail.
#[derive(Debug)]
pub enum HostCallPrecondition {
    /// A read memory region is out of bounds
    ReadOutOfBounds {
        /// Start address of the region
        addr: MemAddr,
        /// Size of the region
        size: Size,
    },
    /// A write memory region is out of bounds
    WriteOutOfBounds {
        /// Start address of the region
        addr: MemAddr,
        /// Size of the region
        size: Size,
    },
    /// No valid capability is held by the caller
    NoValidCapability,
    /// No message provided for send operation
    NoMessage,
    /// Message source does not match the caller
    SourceMismatch,
    /// Destination mailbox is full
    MailboxFull,
    /// Invalid security level value
    InvalidSecurityLevel {
        /// The invalid level value
        value: u64,
    },
    /// Cannot classify up (only declassification is allowed)
    CannotClassifyUp,
    /// No address argument provided for free operation
    NoAddress,
    /// No capability ID argument provided for revoke operation
    NoCapabilityId,
    /// Underlying free operation failed
    FreeFailed {
        /// The state error that caused the failure
        source: StateError,
    },
    /// Operation requires Phase 3 State extension (threads/scheduler)
    NotImplemented {
        /// Description of the unimplemented operation
        operation: &'static str,
    },
}

impl std::fmt::Display for HostCallPrecondition {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            HostCallPrecondition::ReadOutOfBounds { addr, size } => {
                write!(f, "Read region {addr}+{size} out of bounds")
            }
            HostCallPrecondition::WriteOutOfBounds { addr, size } => {
                write!(f, "Write region {addr}+{size} out of bounds")
            }
            HostCallPrecondition::NoValidCapability => write!(f, "No valid capability held"),
            HostCallPrecondition::NoMessage => write!(f, "No message to send"),
            HostCallPrecondition::SourceMismatch => {
                write!(f, "Message source doesn't match caller")
            }
            HostCallPrecondition::MailboxFull => write!(f, "Destination mailbox full"),
            HostCallPrecondition::InvalidSecurityLevel { value } => {
                write!(f, "Invalid security level: {value}")
            }
            HostCallPrecondition::CannotClassifyUp => write!(f, "Cannot classify up"),
            HostCallPrecondition::NoAddress => write!(f, "No address to free"),
            HostCallPrecondition::NoCapabilityId => write!(f, "No capability ID to revoke"),
            HostCallPrecondition::FreeFailed { source } => {
                write!(f, "Free failed: {source:?}")
            }
            HostCallPrecondition::NotImplemented { operation } => {
                write!(
                    f,
                    "{operation} requires Phase 3 State extension (threads/scheduler)"
                )
            }
        }
    }
}

/// Reasons a kernel operation can fail.
#[derive(Debug)]
pub enum KernelOpError {
    /// Actor has no pending messages to route
    NoPendingMessages {
        /// The destination actor
        dst: ActorId,
    },
    /// Actor mailbox is at capacity
    MailboxAtCapacity {
        /// The destination actor
        dst: ActorId,
    },
    /// Workflow not found
    WorkflowNotFound {
        /// The workflow ID
        wid: WorkflowId,
    },
    /// Workflow is not in the running state
    WorkflowNotRunning {
        /// The workflow ID
        wid: WorkflowId,
    },
    /// Operation requires Phase 3 State extension (threads/scheduler)
    NotImplemented {
        /// Description of the unimplemented operation
        operation: &'static str,
    },
    /// Counter overflow (time or epoch would exceed u64::MAX)
    CounterOverflow(String),
}

impl std::fmt::Display for KernelOpError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            KernelOpError::NoPendingMessages { dst } => {
                write!(f, "Actor {dst} has no pending messages")
            }
            KernelOpError::MailboxAtCapacity { dst } => {
                write!(f, "Actor {dst} mailbox at capacity")
            }
            KernelOpError::WorkflowNotFound { wid } => write!(f, "Workflow {wid} not found"),
            KernelOpError::WorkflowNotRunning { wid } => {
                write!(f, "Workflow {wid} is not running")
            }
            KernelOpError::NotImplemented { operation } => {
                write!(
                    f,
                    "{operation} requires Phase 3 State extension (threads/scheduler)"
                )
            }
            KernelOpError::CounterOverflow(msg) => {
                write!(f, "counter overflow: {msg}")
            }
        }
    }
}

/// Reasons a state transition can be invalid.
#[derive(Debug)]
pub enum InvalidTransitionReason {
    /// Attempted transition between incompatible states
    IncompatibleStates {
        /// Description of what was expected
        expected: &'static str,
        /// Description of what was found
        found: &'static str,
    },
}

impl std::fmt::Display for InvalidTransitionReason {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            InvalidTransitionReason::IncompatibleStates { expected, found } => {
                write!(f, "expected {expected}, found {found}")
            }
        }
    }
}

/// Error type for step operations.
#[derive(Debug)]
pub enum StepError {
    /// Plugin not found
    PluginNotFound(PluginId),
    /// Actor not found
    ActorNotFound(PluginId),
    /// Memory address already freed (double-free prevention)
    AddressAlreadyFreed(MemAddr),
    /// Capability targets this address (temporal safety violation)
    CapabilityTargetsAddress(CapId, MemAddr),
    /// Capability not found
    CapabilityNotFound(CapId),
    /// Authorization failed
    AuthorizationFailed(AuthorizationError),
    /// Plugin precondition failed
    PluginPreconditionFailed(PluginPrecondition),
    /// Host call precondition failed
    HostCallPreconditionFailed(HostCallPrecondition),
    /// Kernel operation failed
    KernelOpFailed(KernelOpError),
    /// Invalid state transition
    InvalidTransition(InvalidTransitionReason),
    /// State operation failed
    StateError(StateError),
    /// Resource quota exceeded (DoS prevention)
    QuotaExceeded(u8, u64, u64, u64),
}

impl std::fmt::Display for StepError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            StepError::PluginNotFound(id) => write!(f, "plugin {id} not found"),
            StepError::ActorNotFound(id) => write!(f, "actor {id} not found"),
            StepError::AddressAlreadyFreed(addr) => write!(f, "address {addr} already freed"),
            StepError::CapabilityTargetsAddress(cap, addr) => {
                write!(f, "capability {cap} targets address {addr}")
            }
            StepError::CapabilityNotFound(id) => write!(f, "capability {id} not found"),
            StepError::AuthorizationFailed(e) => write!(f, "authorization failed: {e}"),
            StepError::PluginPreconditionFailed(reason) => {
                write!(f, "plugin precondition failed: {reason}")
            }
            StepError::HostCallPreconditionFailed(reason) => {
                write!(f, "host call precondition failed: {reason}")
            }
            StepError::KernelOpFailed(reason) => write!(f, "kernel operation failed: {reason}"),
            StepError::InvalidTransition(reason) => write!(f, "invalid transition: {reason}"),
            StepError::StateError(e) => write!(f, "state error: {e}"),
            StepError::QuotaExceeded(kind, requested, current, quota) => {
                let kind_name = match kind {
                    0 => "memory",
                    1 => "capability",
                    2 => "ipc_queue",
                    _ => "unknown",
                };
                write!(
                    f,
                    "{kind_name} quota exceeded: requested {requested}, current {current}, quota {quota}"
                )
            }
        }
    }
}

impl std::error::Error for StepError {}

impl From<AuthorizationError> for StepError {
    fn from(e: AuthorizationError) -> Self {
        StepError::AuthorizationFailed(e)
    }
}

impl From<StateError> for StepError {
    fn from(e: StateError) -> Self {
        StepError::StateError(e)
    }
}

/// Step represents a single state transition in the Lion microkernel.
#[derive(Debug, Clone)]
#[allow(clippy::large_enum_variant)]
#[must_use = "steps must be executed to apply state transitions"]
pub enum Step {
    /// Plugin-internal computation (untrusted, sandboxed)
    PluginInternal {
        /// The plugin performing computation
        pid: PluginId,
        /// The internal computation descriptor
        pi: PluginInternal,
    },

    /// Host call (trust boundary, mediated)
    HostCall {
        /// The host call request
        hc: HostCall,
        /// Authorization witness (validated at execution)
        auth: Authorized,
        /// Result of host call execution
        result: HostResult,
    },

    /// Kernel-internal operation (trusted TCB)
    KernelInternal {
        /// The kernel operation to execute
        op: KernelOp,
    },

    /// Direct memory free operation (refinement bridge)
    MemFree {
        /// The plugin requesting the free
        caller: PluginId,
        /// The address to free
        addr: MemAddr,
    },

    /// Direct capability revoke operation (refinement bridge)
    CapRevoke {
        /// The plugin requesting revocation
        caller: PluginId,
        /// The capability ID to revoke
        cap_id: CapId,
    },
}

impl Step {
    /// Create a host call step with ATOMIC authorization.
    ///
    /// The action is derived from the `HostCall` via `hc.required_action()`,
    /// binding authorization to the exact operation being performed.
    /// This prevents the vulnerability where a valid `Authorized` token
    /// for one action could be reused to execute a different `HostCall`.
    pub fn host_call_atomic(
        state: &State,
        hc: HostCall,
        cap_id: CapId,
        ctx: crate::types::PolicyContext,
        result: HostResult,
    ) -> Result<Self, StepError> {
        let cap = state
            .get_cap(cap_id)
            .cloned()
            .ok_or(StepError::CapabilityNotFound(cap_id))?;

        let action = hc.required_action()?;
        if cap.holder() != hc.caller() {
            return Err(StepError::AuthorizationFailed(
                AuthorizationError::HolderMismatch {
                    expected: hc.caller(),
                    actual: cap.holder(),
                },
            ));
        }

        let auth = Authorized::validate_atomic(state, cap, action, ctx)?;
        Ok(Step::HostCall { hc, auth, result })
    }

    /// Check if this step is effectful (crosses trust boundary).
    pub fn is_effectful(&self) -> bool {
        matches!(self, Step::HostCall { .. })
    }

    /// Get the subject (executing plugin) of the step, if any.
    pub fn subject(&self) -> Option<PluginId> {
        match self {
            Step::PluginInternal { pid, .. } => Some(*pid),
            Step::HostCall { hc, .. } => Some(hc.caller),
            Step::KernelInternal { .. } => None,
            Step::MemFree { caller, .. } | Step::CapRevoke { caller, .. } => Some(*caller),
        }
    }

    /// Get the security level of the step.
    pub fn level(&self, state: &State) -> SecurityLevel {
        match self.subject() {
            Some(pid) => state.plugin_level(pid).unwrap_or(SecurityLevel::Public),
            None => SecurityLevel::Secret,
        }
    }

    /// Check if this is a declassify operation.
    pub fn is_declassify(&self) -> bool {
        matches!(
            self,
            Step::HostCall {
                hc: HostCall {
                    function: HostFunction::Declassify,
                    ..
                },
                ..
            }
        )
    }

    /// Execute this step, returning the new state.
    pub fn execute(&self, state: &State) -> Result<State, StepError> {
        match self {
            Step::PluginInternal { pid, pi } => execute_plugin_internal(state, *pid, pi),
            Step::HostCall { hc, auth, result } => execute_host_call(state, hc, auth, result),
            Step::KernelInternal { op } => execute_kernel_internal(state, op),
            Step::MemFree { caller: _, addr } => execute_mem_free(state, *addr),
            Step::CapRevoke { caller: _, cap_id } => execute_cap_revoke(state, *cap_id),
        }
    }

    /// Mutating execution -- modifies state in place (production path).
    ///
    /// Equivalent to `execute` but avoids the full-state clone by mutating
    /// in place. The validation logic is identical.
    pub fn execute_mut(&self, state: &mut State) -> Result<(), StepError> {
        match self {
            Step::PluginInternal { pid, pi } => execute_plugin_internal_mut(state, *pid, pi),
            Step::HostCall { hc, auth, result } => execute_host_call_mut(state, hc, auth, result),
            Step::KernelInternal { op } => execute_kernel_internal_mut(state, op),
            Step::MemFree { caller: _, addr } => execute_mem_free_mut(state, *addr),
            Step::CapRevoke { caller: _, cap_id } => execute_cap_revoke_mut(state, *cap_id),
        }
    }
}

fn execute_plugin_internal(
    state: &State,
    pid: PluginId,
    pi: &PluginInternal,
) -> Result<State, StepError> {
    let plugin = state
        .get_plugin(pid)
        .ok_or(StepError::PluginNotFound(pid))?;

    pi.check_preconditions(state, pid)?;

    let mut new_state = state.clone();

    if let Some(_new_plugin) = new_state.get_plugin_mut(pid) {
        for &(addr, _size) in &pi.writes {
            if !plugin.memory().addr_in_bounds(addr, 1) {
                return Err(StepError::PluginPreconditionFailed(
                    PluginPrecondition::WriteOutOfBounds {
                        addr,
                        size: 1,
                        bounds: plugin.memory_bounds(),
                    },
                ));
            }
        }

        if pi.consume_mailbox {
            if let Some(actor) = new_state.get_actor_mut(pid) {
                let _ = actor.consume_mut();
            }
        }
    }

    Ok(new_state)
}

fn execute_host_call(
    state: &State,
    hc: &HostCall,
    auth: &Authorized,
    result: &HostResult,
) -> Result<State, StepError> {
    // Verify authorization subject matches the host call caller
    if auth.action().subject() != hc.caller() {
        return Err(StepError::AuthorizationFailed(
            AuthorizationError::HolderMismatch {
                expected: auth.action().subject(),
                actual: hc.caller(),
            },
        ));
    }

    // Verify the authorized action matches what the host call requires
    let derived = hc.required_action()?;
    if &derived != auth.action() {
        return Err(StepError::InvalidTransition(
            InvalidTransitionReason::IncompatibleStates {
                expected: "authorized host call",
                found: "mismatched host call",
            },
        ));
    }

    auth.validate(state)?;
    hc.check_preconditions(state)?;
    hc.function.execute(state, hc, result)
}

fn execute_kernel_internal(state: &State, op: &KernelOp) -> Result<State, StepError> {
    op.execute(state)
}

fn execute_mem_free(state: &State, addr: MemAddr) -> Result<State, StepError> {
    if state.ghost().is_freed(addr) {
        return Err(StepError::AddressAlreadyFreed(addr));
    }

    // ResourceId = u128, addr = u64 (MemAddr)
    if let Some(&(cap_id, _)) = state
        .kernel()
        .revocation()
        .iter()
        .find(|(_, cap)| cap.is_valid() && cap.target() == u128::from(addr))
    {
        return Err(StepError::CapabilityTargetsAddress(cap_id, addr));
    }

    state.apply_free(addr).map_err(StepError::StateError)
}

fn execute_cap_revoke(state: &State, cap_id: CapId) -> Result<State, StepError> {
    if state.kernel().revocation().get(cap_id).is_none() {
        return Err(StepError::CapabilityNotFound(cap_id));
    }

    Ok(state.apply_cap_revoke(cap_id))
}

// ============== MUTATING VARIANTS ==============
//
// These perform the same validation as their pure counterparts
// but modify `&mut State` in place instead of cloning.

fn execute_plugin_internal_mut(
    state: &mut State,
    pid: PluginId,
    pi: &PluginInternal,
) -> Result<(), StepError> {
    let plugin = state
        .get_plugin(pid)
        .ok_or(StepError::PluginNotFound(pid))?;

    pi.check_preconditions(state, pid)?;

    // Validate writes before mutating (borrows plugin immutably)
    for &(addr, _size) in &pi.writes {
        if !plugin.memory().addr_in_bounds(addr, 1) {
            return Err(StepError::PluginPreconditionFailed(
                PluginPrecondition::WriteOutOfBounds {
                    addr,
                    size: 1,
                    bounds: plugin.memory_bounds(),
                },
            ));
        }
    }

    if pi.consume_mailbox {
        if let Some(actor) = state.get_actor_mut(pid) {
            let _ = actor.consume_mut();
        }
    }

    Ok(())
}

fn execute_host_call_mut(
    state: &mut State,
    hc: &HostCall,
    auth: &Authorized,
    result: &HostResult,
) -> Result<(), StepError> {
    // Verify authorization subject matches the host call caller
    if auth.action().subject() != hc.caller() {
        return Err(StepError::AuthorizationFailed(
            AuthorizationError::HolderMismatch {
                expected: auth.action().subject(),
                actual: hc.caller(),
            },
        ));
    }

    // Verify the authorized action matches what the host call requires
    let derived = hc.required_action()?;
    if &derived != auth.action() {
        return Err(StepError::InvalidTransition(
            InvalidTransitionReason::IncompatibleStates {
                expected: "authorized host call",
                found: "mismatched host call",
            },
        ));
    }

    auth.validate(state)?;
    hc.check_preconditions(state)?;
    hc.function.execute_mut(state, hc, result)
}

fn execute_kernel_internal_mut(state: &mut State, op: &KernelOp) -> Result<(), StepError> {
    op.execute_mut(state)
}

fn execute_mem_free_mut(state: &mut State, addr: MemAddr) -> Result<(), StepError> {
    if state.ghost().is_freed(addr) {
        return Err(StepError::AddressAlreadyFreed(addr));
    }

    // ResourceId = u128, addr = u64 (MemAddr)
    if let Some(&(cap_id, _)) = state
        .kernel()
        .revocation()
        .iter()
        .find(|(_, cap)| cap.is_valid() && cap.target() == u128::from(addr))
    {
        return Err(StepError::CapabilityTargetsAddress(cap_id, addr));
    }

    state.apply_free_mut(addr).map_err(StepError::StateError)
}

fn execute_cap_revoke_mut(state: &mut State, cap_id: CapId) -> Result<(), StepError> {
    if state.kernel().revocation().get(cap_id).is_none() {
        return Err(StepError::CapabilityNotFound(cap_id));
    }

    state
        .apply_cap_revoke_mut(cap_id)
        .map_err(|_| StepError::CapabilityNotFound(cap_id))
}

/// Reachability relation -- reflexive transitive closure of Step.
pub fn reachable(start: &State, end: &State, steps: &[Step]) -> Result<bool, StepError> {
    let mut current = start.clone();
    for step in steps {
        current = step.execute(&current)?;
    }
    Ok(current.time() == end.time())
}
