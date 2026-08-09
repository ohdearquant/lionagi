// Copyright (C) 2026 HaiyangLi
// SPDX-License-Identifier: Apache-2.0
//! Lion Step Kernel Operations
//!
//! Corresponds to: Lion/Step/KernelOp.lean
//!
//! Kernel-internal operations (trusted TCB).

use crate::state::State;
use crate::types::{ActorId, ThreadId, WorkflowId};

use super::{KernelOpError, StepError};

/// Kernel-internal operations (no external trigger)
///
/// Corresponds to Lean: `inductive KernelOp`
#[derive(Debug, Clone, PartialEq, Eq)]
#[must_use = "kernel operations must be executed"]
pub enum KernelOp {
    /// Deliver one message to actor
    ///
    /// Corresponds to Lean: `| route_one (dst : ActorId)`
    RouteOne {
        /// Destination actor to receive the message
        dst: ActorId,
    },

    /// Advance workflow
    ///
    /// Corresponds to Lean: `| workflow_tick (w : WorkflowId)`
    WorkflowTick {
        /// Workflow to advance
        wid: WorkflowId,
    },

    /// Increment global time
    ///
    /// Corresponds to Lean: `| time_tick`
    TimeTick,

    /// Unblock actor waiting to send
    ///
    /// Corresponds to Lean: `| unblock_send (dst : ActorId)`
    UnblockSend {
        /// Destination actor to unblock
        dst: ActorId,
    },

    /// Context switch between threads
    ///
    /// Corresponds to Lean: `| thread_switch (from_ to_ : ThreadId)`
    ThreadSwitch {
        /// Thread to switch from
        from: ThreadId,
        /// Thread to switch to
        to: ThreadId,
    },
}

impl KernelOp {
    /// Execute the kernel operation
    ///
    /// Corresponds to Lean: `def KernelExecInternal`
    ///
    /// Each operation modifies only its designated state component:
    /// - route_one: moves message from pending to mailbox
    /// - time_tick: increments global time
    /// - workflow_tick: advances workflow state
    /// - unblock_send: clears blockedOn for actor
    ///
    /// # Errors
    ///
    /// Returns `StepError::ActorNotFound` if the destination actor does not exist (RouteOne, UnblockSend).
    /// Returns `StepError::KernelOpFailed` if the actor has no pending messages or its mailbox is full (RouteOne).
    /// Returns `StepError::KernelOpFailed` if the workflow is not found or is not running (WorkflowTick).
    pub fn execute(&self, state: &State) -> Result<State, StepError> {
        match self {
            KernelOp::RouteOne { dst } => execute_route_one(state, *dst),
            KernelOp::WorkflowTick { wid } => execute_workflow_tick(state, *wid),
            KernelOp::TimeTick => execute_time_tick(state),
            KernelOp::UnblockSend { dst } => execute_unblock_send(state, *dst),
            KernelOp::ThreadSwitch { from, to } => execute_thread_switch(state, *from, *to),
        }
    }

    /// Execute the kernel operation (mutating version).
    ///
    /// Same validation as `execute` but modifies `&mut State` in place.
    pub fn execute_mut(&self, state: &mut State) -> Result<(), StepError> {
        match self {
            KernelOp::RouteOne { dst } => execute_route_one_mut(state, *dst),
            KernelOp::WorkflowTick { wid } => execute_workflow_tick_mut(state, *wid),
            KernelOp::TimeTick => execute_time_tick_mut(state),
            KernelOp::UnblockSend { dst } => execute_unblock_send_mut(state, *dst),
            KernelOp::ThreadSwitch { from, to } => execute_thread_switch_mut(state, *from, *to),
        }
    }
}

/// Execute route_one
///
/// Move first message from pending to mailbox (if pending non-empty).
/// Scheduler ensures mailbox doesn't exceed capacity.
///
/// Corresponds to Lean: `KernelExecInternal .route_one`
fn execute_route_one(state: &State, dst: ActorId) -> Result<State, StepError> {
    let actor = state.get_actor(dst).ok_or(StepError::ActorNotFound(dst))?;

    // Check pending queue has messages
    if actor.pending_len() == 0 {
        return Err(StepError::KernelOpFailed(
            KernelOpError::NoPendingMessages { dst },
        ));
    }

    // Check mailbox has space
    if actor.mailbox_len() >= actor.capacity() {
        return Err(StepError::KernelOpFailed(
            KernelOpError::MailboxAtCapacity { dst },
        ));
    }

    // Deliver message
    let mut new_state = state.clone();
    if let Some(actor) = new_state.get_actor_mut(dst) {
        let _ = actor.deliver_mut();
    }

    Ok(new_state)
}

/// Execute workflow_tick
///
/// Advance workflow state (must preserve has-work invariant).
///
/// Corresponds to Lean: `KernelExecInternal .workflow_tick`
///
/// PERF NOTE: Currently a no-op that validates preconditions and clones state.
/// The actual workflow advancement is abstracted in the formal model. A concrete
/// implementation would advance node states based on dependencies and avoid the
/// identity clone. The clone is retained for API uniformity with other kernel ops.
fn execute_workflow_tick(state: &State, wid: WorkflowId) -> Result<State, StepError> {
    let workflow = state.get_workflow(wid).ok_or(StepError::KernelOpFailed(
        KernelOpError::WorkflowNotFound { wid },
    ))?;

    // Check workflow is running
    if !workflow.is_running() {
        return Err(StepError::KernelOpFailed(
            KernelOpError::WorkflowNotRunning { wid },
        ));
    }

    // Abstract tick: validates preconditions, returns state unchanged.
    // A concrete implementation would advance node states based on dependencies.
    Ok(state.clone())
}

/// Execute time_tick
///
/// Increment time and kernel time.
///
/// Corresponds to Lean: `KernelExecInternal .time_tick`
fn execute_time_tick(state: &State) -> Result<State, StepError> {
    let mut new_state = state.clone();
    new_state
        .tick()
        .map_err(|e| StepError::KernelOpFailed(KernelOpError::CounterOverflow(e.to_string())))?;
    Ok(new_state)
}

/// Execute unblock_send
///
/// Clear blockedOn for the destination actor.
///
/// Corresponds to Lean: `KernelExecInternal .unblock_send`
fn execute_unblock_send(state: &State, dst: ActorId) -> Result<State, StepError> {
    let _actor = state.get_actor(dst).ok_or(StepError::ActorNotFound(dst))?;

    let mut new_state = state.clone();
    if let Some(actor) = new_state.get_actor_mut(dst) {
        actor.unblock_mut();
    }

    Ok(new_state)
}

/// Execute thread_switch
///
/// Context switch between threads: save `from` registers, restore `to` and
/// make it running. Requires threads/scheduler in State (Phase 3).
///
/// Corresponds to Lean: `KernelExecInternal (.thread_switch from_ to_)`
///
/// NOTE: State does not yet include threads/scheduler fields (Phase 3 extension).
/// This stub returns an error until the State type is extended.
fn execute_thread_switch(
    _state: &State,
    _from: ThreadId,
    _to: ThreadId,
) -> Result<State, StepError> {
    Err(StepError::KernelOpFailed(KernelOpError::NotImplemented {
        operation: "thread_switch",
    }))
}

// ============== MUTATING VARIANTS ==============

fn execute_route_one_mut(state: &mut State, dst: ActorId) -> Result<(), StepError> {
    let actor = state.get_actor(dst).ok_or(StepError::ActorNotFound(dst))?;

    if actor.pending_len() == 0 {
        return Err(StepError::KernelOpFailed(
            KernelOpError::NoPendingMessages { dst },
        ));
    }

    if actor.mailbox_len() >= actor.capacity() {
        return Err(StepError::KernelOpFailed(
            KernelOpError::MailboxAtCapacity { dst },
        ));
    }

    if let Some(actor) = state.get_actor_mut(dst) {
        let _ = actor.deliver_mut();
    }

    Ok(())
}

fn execute_workflow_tick_mut(state: &mut State, wid: WorkflowId) -> Result<(), StepError> {
    let workflow = state.get_workflow(wid).ok_or(StepError::KernelOpFailed(
        KernelOpError::WorkflowNotFound { wid },
    ))?;

    if !workflow.is_running() {
        return Err(StepError::KernelOpFailed(
            KernelOpError::WorkflowNotRunning { wid },
        ));
    }

    // Abstract tick: validates preconditions, state unchanged.
    Ok(())
}

fn execute_time_tick_mut(state: &mut State) -> Result<(), StepError> {
    state
        .tick()
        .map_err(|e| StepError::KernelOpFailed(KernelOpError::CounterOverflow(e.to_string())))?;
    Ok(())
}

fn execute_unblock_send_mut(state: &mut State, dst: ActorId) -> Result<(), StepError> {
    let _actor = state.get_actor(dst).ok_or(StepError::ActorNotFound(dst))?;

    if let Some(actor) = state.get_actor_mut(dst) {
        actor.unblock_mut();
    }

    Ok(())
}

fn execute_thread_switch_mut(
    _state: &mut State,
    _from: ThreadId,
    _to: ThreadId,
) -> Result<(), StepError> {
    Err(StepError::KernelOpFailed(KernelOpError::NotImplemented {
        operation: "thread_switch",
    }))
}

// ============== FRAME THEOREMS ==============
//
// These correspond to Lean's comprehensive frame theorems.
// In Rust, we verify these properties through tests.

#[cfg(test)]
mod tests {
    use super::*;
    use crate::state::{ActorRuntime, Message, PluginState, WorkflowInstance};
    use crate::types::SecurityLevel;

    fn make_test_message(id: u128) -> Message {
        Message::new(id, 1, 2, SecurityLevel::Public, vec![1, 2, 3])
    }

    #[test]
    fn test_time_tick_increments_time() {
        let state = State::empty();
        assert_eq!(state.time(), 0);

        let op = KernelOp::TimeTick;
        let new_state = op.execute(&state).expect("time_tick should succeed");

        assert_eq!(new_state.time(), 1);
    }

    #[test]
    fn test_time_tick_preserves_plugins() {
        // Corresponds to Lean: time_tick_comprehensive_frame (plugins unchanged)
        let mut state = State::empty();
        let _ = state.insert_plugin(1, PluginState::empty(SecurityLevel::Public, 100));

        let op = KernelOp::TimeTick;
        let new_state = op.execute(&state).expect("time_tick should succeed");

        // Plugins should be unchanged
        assert!(new_state.get_plugin(1).is_some());
        assert_eq!(
            new_state.get_plugin(1).map(|p| p.memory_bounds()),
            Some(100)
        );
    }

    #[test]
    fn test_time_tick_preserves_actors() {
        // Corresponds to Lean: time_tick_comprehensive_frame (actors unchanged)
        let mut state = State::empty();
        let _ = state.insert_actor(1, ActorRuntime::empty(10));

        let op = KernelOp::TimeTick;
        let new_state = op.execute(&state).expect("time_tick should succeed");

        // Actors should be unchanged
        assert!(new_state.get_actor(1).is_some());
        assert_eq!(new_state.get_actor(1).map(|a| a.capacity()), Some(10));
    }

    #[test]
    fn test_route_one_moves_message() {
        let mut state = State::empty();
        let mut actor = ActorRuntime::empty(10);
        actor.enqueue_pending_mut(make_test_message(42));
        state.insert_actor(1, actor).unwrap();

        assert_eq!(state.get_actor(1).map(|a| a.pending_len()), Some(1));
        assert_eq!(state.get_actor(1).map(|a| a.mailbox_len()), Some(0));

        let op = KernelOp::RouteOne { dst: 1 };
        let new_state = op.execute(&state).expect("route_one should succeed");

        assert_eq!(new_state.get_actor(1).map(|a| a.pending_len()), Some(0));
        assert_eq!(new_state.get_actor(1).map(|a| a.mailbox_len()), Some(1));
    }

    #[test]
    fn test_route_one_preserves_other_actors() {
        // Corresponds to Lean: route_one_comprehensive_frame (other actors unchanged)
        let mut state = State::empty();

        let mut actor1 = ActorRuntime::empty(10);
        actor1.enqueue_pending_mut(make_test_message(1));
        state.insert_actor(1, actor1).unwrap();

        let actor2 = ActorRuntime::empty(5);
        state.insert_actor(2, actor2).unwrap();

        let op = KernelOp::RouteOne { dst: 1 };
        let new_state = op.execute(&state).expect("route_one should succeed");

        // Actor 2 should be unchanged
        assert_eq!(new_state.get_actor(2).map(|a| a.capacity()), Some(5));
        assert_eq!(new_state.get_actor(2).map(|a| a.pending_len()), Some(0));
    }

    #[test]
    fn test_route_one_preserves_plugins() {
        // Corresponds to Lean: route_one_memory_unchanged
        let mut state = State::empty();

        let mut actor = ActorRuntime::empty(10);
        actor.enqueue_pending_mut(make_test_message(1));
        state.insert_actor(1, actor).unwrap();

        let _ = state.insert_plugin(1, PluginState::empty(SecurityLevel::Secret, 1024));

        let op = KernelOp::RouteOne { dst: 1 };
        let new_state = op.execute(&state).expect("route_one should succeed");

        // Plugin should be unchanged
        assert_eq!(new_state.plugin_level(1), Some(SecurityLevel::Secret));
    }

    #[test]
    fn test_route_one_no_pending_fails() {
        let mut state = State::empty();
        let _ = state.insert_actor(1, ActorRuntime::empty(10));

        let op = KernelOp::RouteOne { dst: 1 };
        let result = op.execute(&state);

        assert!(matches!(result, Err(StepError::KernelOpFailed(_))));
    }

    #[test]
    fn test_route_one_mailbox_full_fails() {
        let mut state = State::empty();

        // Create actor with capacity 1, full mailbox
        let mut actor = ActorRuntime::empty(1);
        actor.enqueue_pending_mut(make_test_message(1));
        let _ = actor.deliver_mut(); // Fill mailbox
        actor.enqueue_pending_mut(make_test_message(2)); // Add another pending
        state.insert_actor(1, actor).unwrap();

        let op = KernelOp::RouteOne { dst: 1 };
        let result = op.execute(&state);

        assert!(matches!(result, Err(StepError::KernelOpFailed(_))));
    }

    #[test]
    fn test_unblock_send_clears_blocked() {
        let mut state = State::empty();
        let mut actor = ActorRuntime::empty(10);
        actor.set_blocked_mut(42);
        state.insert_actor(1, actor).unwrap();

        assert!(state.get_actor(1).map(|a| a.is_blocked()).unwrap_or(false));

        let op = KernelOp::UnblockSend { dst: 1 };
        let new_state = op.execute(&state).expect("unblock_send should succeed");

        assert!(!new_state
            .get_actor(1)
            .map(|a| a.is_blocked())
            .unwrap_or(true));
    }

    #[test]
    fn test_unblock_send_preserves_mailbox() {
        // Corresponds to Lean: unblock_send_comprehensive_frame
        let mut state = State::empty();
        let mut actor = ActorRuntime::empty(10);
        actor.enqueue_pending_mut(make_test_message(1));
        let _ = actor.deliver_mut();
        actor.set_blocked_mut(42);
        state.insert_actor(1, actor).unwrap();

        let initial_mailbox_len = state.get_actor(1).map(|a| a.mailbox_len()).unwrap_or(0);

        let op = KernelOp::UnblockSend { dst: 1 };
        let new_state = op.execute(&state).expect("unblock_send should succeed");

        let final_mailbox_len = new_state.get_actor(1).map(|a| a.mailbox_len()).unwrap_or(0);
        assert_eq!(initial_mailbox_len, final_mailbox_len);
    }

    #[test]
    fn test_workflow_tick_not_found() {
        let state = State::empty();

        let op = KernelOp::WorkflowTick { wid: 999 };
        let result = op.execute(&state);

        assert!(matches!(result, Err(StepError::KernelOpFailed(_))));
    }

    #[test]
    fn test_workflow_tick_preserves_other_workflows() {
        // Corresponds to Lean: workflow_tick_comprehensive_frame
        let mut state = State::empty();

        let _ = state.insert_workflow(1, WorkflowInstance::running(100));
        let _ = state.insert_workflow(2, WorkflowInstance::running(200));

        let op = KernelOp::WorkflowTick { wid: 1 };
        let new_state = op.execute(&state).expect("workflow_tick should succeed");

        // Workflow 2 should be unchanged
        assert!(new_state
            .get_workflow(2)
            .map(|w| w.is_running())
            .unwrap_or(false));
    }
}
