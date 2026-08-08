// Copyright (C) 2026 HaiyangLi
// SPDX-License-Identifier: Apache-2.0
//! Lion Step Plugin Internal
//!
//! Corresponds to: Lion/Step/PluginInternal.lean
//!
//! Plugin-internal computation (sandboxed, untrusted).

use crate::state::State;
use crate::types::{MemAddr, PluginId, Size};

use super::{PluginPrecondition, StepError};

/// Plugin internal computation descriptor
///
/// Corresponds to Lean: `structure PluginInternal`
///
/// Describes what a plugin's internal WASM computation does:
/// - Memory regions read
/// - Memory regions written
/// - Whether to consume from mailbox
#[derive(Debug, Clone, Default)]
pub struct PluginInternal {
    /// Memory regions read
    ///
    /// Corresponds to Lean: `reads : List (MemAddr x Size)`
    pub reads: Vec<(MemAddr, Size)>,

    /// Memory regions written
    ///
    /// Corresponds to Lean: `writes : List (MemAddr x Size)`
    pub writes: Vec<(MemAddr, Size)>,

    /// Whether to consume from mailbox
    ///
    /// Corresponds to Lean: `consume_mailbox : Bool`
    pub consume_mailbox: bool,
}

impl PluginInternal {
    /// Create a new plugin internal computation descriptor
    pub fn new(
        reads: Vec<(MemAddr, Size)>,
        writes: Vec<(MemAddr, Size)>,
        consume_mailbox: bool,
    ) -> Self {
        PluginInternal {
            reads,
            writes,
            consume_mailbox,
        }
    }

    /// Create a computation that just computes (no I/O)
    pub fn compute() -> Self {
        PluginInternal {
            reads: Vec::new(),
            writes: Vec::new(),
            consume_mailbox: false,
        }
    }

    /// Create a computation that reads memory
    pub fn with_reads(reads: Vec<(MemAddr, Size)>) -> Self {
        PluginInternal {
            reads,
            writes: Vec::new(),
            consume_mailbox: false,
        }
    }

    /// Create a computation that writes memory
    pub fn with_writes(writes: Vec<(MemAddr, Size)>) -> Self {
        PluginInternal {
            reads: Vec::new(),
            writes,
            consume_mailbox: false,
        }
    }

    /// Check preconditions for plugin internal execution
    ///
    /// Corresponds to Lean: `def plugin_internal_pre`
    ///
    /// Preconditions:
    /// 1. Plugin is not blocked on another actor
    /// 2. Read regions are in bounds
    /// 3. Write regions are in bounds
    /// 4. If consuming mailbox, mailbox must not be empty
    ///
    /// # Errors
    ///
    /// Returns `StepError::PluginNotFound` if the plugin does not exist.
    /// Returns `StepError::PluginPreconditionFailed` if the plugin's actor is blocked.
    /// Returns `StepError::PluginPreconditionFailed` if a read region is out of bounds.
    /// Returns `StepError::PluginPreconditionFailed` if a write region is out of bounds.
    /// Returns `StepError::PluginPreconditionFailed` if consuming mailbox but the mailbox is empty.
    pub fn check_preconditions(&self, state: &State, pid: PluginId) -> Result<(), StepError> {
        // Check plugin exists
        let plugin = state
            .get_plugin(pid)
            .ok_or(StepError::PluginNotFound(pid))?;

        // Check not blocked
        if let Some(actor) = state.get_actor(pid) {
            if actor.is_blocked() {
                return Err(StepError::PluginPreconditionFailed(
                    PluginPrecondition::Blocked {
                        pid,
                        blocked_on: actor.blocked_on(),
                    },
                ));
            }
        }

        // Check read regions in bounds
        if let Some(err) = self.find_oob_read(plugin) {
            return Err(err);
        }

        // Check write regions in bounds
        if let Some(err) = self.find_oob_write(plugin) {
            return Err(err);
        }

        // Check mailbox if consuming
        if self.consume_mailbox {
            if let Some(actor) = state.get_actor(pid) {
                if !actor.can_receive() {
                    return Err(StepError::PluginPreconditionFailed(
                        PluginPrecondition::MailboxEmpty,
                    ));
                }
            }
        }

        Ok(())
    }

    /// Helper: find first out-of-bounds read region
    fn find_oob_read(&self, plugin: &crate::state::PluginState) -> Option<StepError> {
        self.reads.iter().find_map(|&(addr, size)| {
            if plugin.memory().addr_in_bounds(addr, size) {
                None
            } else {
                Some(StepError::PluginPreconditionFailed(
                    PluginPrecondition::ReadOutOfBounds {
                        addr,
                        size,
                        bounds: plugin.memory_bounds(),
                    },
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
                Some(StepError::PluginPreconditionFailed(
                    PluginPrecondition::WriteOutOfBounds {
                        addr,
                        size,
                        bounds: plugin.memory_bounds(),
                    },
                ))
            }
        })
    }
}

// ============== FRAME THEOREMS ==============
//
// These correspond to Lean's comprehensive frame theorem:
// plugin_internal_comprehensive_frame
//
// Plugin internal execution (WASM sandbox) only affects:
// - The executing plugin's memory contents
// - The executing plugin's local state
//
// It CANNOT modify:
// - Other plugins' state
// - Kernel state
// - Actor state (except mailbox consumption)
// - Resources
// - Workflows
// - Ghost state
// - Time
// - Security-critical fields (level, heldCaps, memory.bounds)

#[cfg(test)]
mod tests {
    use super::*;
    use crate::state::{ActorRuntime, Message, PluginState};
    use crate::types::SecurityLevel;

    #[test]
    fn test_plugin_internal_default() {
        let pi = PluginInternal::default();
        assert!(pi.reads.is_empty());
        assert!(pi.writes.is_empty());
        assert!(!pi.consume_mailbox);
    }

    #[test]
    fn test_plugin_internal_compute() {
        let pi = PluginInternal::compute();
        assert!(pi.reads.is_empty());
        assert!(pi.writes.is_empty());
        assert!(!pi.consume_mailbox);
    }

    #[test]
    fn test_plugin_internal_with_reads() {
        let pi = PluginInternal::with_reads(vec![(0, 100), (200, 50)]);
        assert_eq!(pi.reads.len(), 2);
        assert!(pi.writes.is_empty());
    }

    #[test]
    fn test_plugin_internal_with_writes() {
        let pi = PluginInternal::with_writes(vec![(0, 100)]);
        assert!(pi.reads.is_empty());
        assert_eq!(pi.writes.len(), 1);
    }

    #[test]
    fn test_check_preconditions_plugin_not_found() {
        let state = State::empty();
        let pi = PluginInternal::compute();

        let result = pi.check_preconditions(&state, 1);
        assert!(matches!(result, Err(StepError::PluginNotFound(1))));
    }

    #[test]
    fn test_check_preconditions_blocked() {
        let mut state = State::empty();
        let _ = state.insert_plugin(1, PluginState::empty(SecurityLevel::Public, 1024));

        let mut actor = ActorRuntime::empty(10);
        actor.set_blocked_mut(42);
        state.insert_actor(1, actor).unwrap();

        let pi = PluginInternal::compute();
        let result = pi.check_preconditions(&state, 1);

        assert!(matches!(
            result,
            Err(StepError::PluginPreconditionFailed(_))
        ));
    }

    #[test]
    fn test_check_preconditions_read_out_of_bounds() {
        let mut state = State::empty();
        let _ = state.insert_plugin(1, PluginState::empty(SecurityLevel::Public, 100));

        // Read region beyond memory bounds
        let pi = PluginInternal::with_reads(vec![(0, 200)]);
        let result = pi.check_preconditions(&state, 1);

        assert!(matches!(
            result,
            Err(StepError::PluginPreconditionFailed(_))
        ));
    }

    #[test]
    fn test_check_preconditions_write_out_of_bounds() {
        let mut state = State::empty();
        let _ = state.insert_plugin(1, PluginState::empty(SecurityLevel::Public, 100));

        // Write region beyond memory bounds
        let pi = PluginInternal::with_writes(vec![(50, 100)]);
        let result = pi.check_preconditions(&state, 1);

        assert!(matches!(
            result,
            Err(StepError::PluginPreconditionFailed(_))
        ));
    }

    #[test]
    fn test_check_preconditions_consume_empty_mailbox() {
        let mut state = State::empty();
        let _ = state.insert_plugin(1, PluginState::empty(SecurityLevel::Public, 1024));
        let _ = state.insert_actor(1, ActorRuntime::empty(10));

        let pi = PluginInternal {
            reads: Vec::new(),
            writes: Vec::new(),
            consume_mailbox: true,
        };
        let result = pi.check_preconditions(&state, 1);

        assert!(matches!(
            result,
            Err(StepError::PluginPreconditionFailed(_))
        ));
    }

    #[test]
    fn test_check_preconditions_success() {
        let mut state = State::empty();
        let _ = state.insert_plugin(1, PluginState::empty(SecurityLevel::Public, 1024));
        let _ = state.insert_actor(1, ActorRuntime::empty(10));

        let pi = PluginInternal::with_reads(vec![(0, 100), (500, 200)]);
        let result = pi.check_preconditions(&state, 1);

        assert!(result.is_ok());
    }

    #[test]
    fn test_check_preconditions_consume_non_empty_mailbox() {
        let mut state = State::empty();
        let _ = state.insert_plugin(1, PluginState::empty(SecurityLevel::Public, 1024));

        let mut actor = ActorRuntime::empty(10);
        let msg = Message::new(1, 2, 1, SecurityLevel::Public, vec![]);
        actor.enqueue_pending_mut(msg);
        let _ = actor.deliver_mut();
        state.insert_actor(1, actor).unwrap();

        let pi = PluginInternal {
            reads: Vec::new(),
            writes: Vec::new(),
            consume_mailbox: true,
        };
        let result = pi.check_preconditions(&state, 1);

        assert!(result.is_ok());
    }

    // ============== FRAME PROPERTY TESTS ==============

    #[test]
    fn test_frame_other_plugins_unchanged() {
        // Corresponds to Lean: plugin_internal_frame
        // Other plugins should be unchanged after plugin internal execution
        let mut state = State::empty();
        let _ = state.insert_plugin(1, PluginState::empty(SecurityLevel::Public, 1024));
        let _ = state.insert_plugin(2, PluginState::empty(SecurityLevel::Secret, 2048));
        let _ = state.insert_actor(1, ActorRuntime::empty(10));

        let initial_plugin2 = state.get_plugin(2).cloned();

        // Execute plugin 1 internal
        let step = super::super::Step::PluginInternal {
            pid: 1,
            pi: PluginInternal::compute(),
        };
        let new_state = step.execute(&state).expect("should succeed");

        // Plugin 2 should be unchanged
        assert_eq!(new_state.get_plugin(2), initial_plugin2.as_ref());
    }

    #[test]
    fn test_frame_kernel_unchanged() {
        // Corresponds to Lean: plugin_internal_kernel_unchanged
        let mut state = State::empty();
        let _ = state.insert_plugin(1, PluginState::empty(SecurityLevel::Public, 1024));
        let _ = state.insert_actor(1, ActorRuntime::empty(10));

        let initial_kernel_now = state.kernel().now();

        let step = super::super::Step::PluginInternal {
            pid: 1,
            pi: PluginInternal::compute(),
        };
        let new_state = step.execute(&state).expect("should succeed");

        // Kernel time should be unchanged
        assert_eq!(new_state.kernel().now(), initial_kernel_now);
    }

    #[test]
    fn test_frame_time_unchanged() {
        // Corresponds to Lean: plugin_internal_time_unchanged
        let mut state = State::empty();
        let _ = state.insert_plugin(1, PluginState::empty(SecurityLevel::Public, 1024));
        let _ = state.insert_actor(1, ActorRuntime::empty(10));

        let initial_time = state.time();

        let step = super::super::Step::PluginInternal {
            pid: 1,
            pi: PluginInternal::compute(),
        };
        let new_state = step.execute(&state).expect("should succeed");

        // Time should be unchanged
        assert_eq!(new_state.time(), initial_time);
    }

    #[test]
    fn test_security_invariant_level_preserved() {
        // Corresponds to Lean: plugin_internal_level_preserved
        let mut state = State::empty();
        let _ = state.insert_plugin(1, PluginState::empty(SecurityLevel::Confidential, 1024));
        let _ = state.insert_actor(1, ActorRuntime::empty(10));

        let step = super::super::Step::PluginInternal {
            pid: 1,
            pi: PluginInternal::with_writes(vec![(0, 100)]),
        };
        let new_state = step.execute(&state).expect("should succeed");

        // Security level should be preserved
        assert_eq!(new_state.plugin_level(1), Some(SecurityLevel::Confidential));
    }

    #[test]
    fn test_security_invariant_caps_preserved() {
        // Corresponds to Lean: plugin_internal_caps_preserved
        let mut state = State::empty();
        let mut ps = PluginState::empty(SecurityLevel::Public, 1024);
        ps.grant_cap_mut(42);
        ps.grant_cap_mut(43);
        state.insert_plugin(1, ps).unwrap();
        let _ = state.insert_actor(1, ActorRuntime::empty(10));

        let step = super::super::Step::PluginInternal {
            pid: 1,
            pi: PluginInternal::compute(),
        };
        let new_state = step.execute(&state).expect("should succeed");

        // Held caps should be preserved
        assert!(new_state.plugin_holds(1, 42));
        assert!(new_state.plugin_holds(1, 43));
    }

    #[test]
    fn test_security_invariant_bounds_preserved() {
        // Corresponds to Lean: plugin_internal_bounds_preserved
        let mut state = State::empty();
        let _ = state.insert_plugin(1, PluginState::empty(SecurityLevel::Public, 1024));
        let _ = state.insert_actor(1, ActorRuntime::empty(10));

        let step = super::super::Step::PluginInternal {
            pid: 1,
            pi: PluginInternal::with_writes(vec![(0, 100)]),
        };
        let new_state = step.execute(&state).expect("should succeed");

        // Memory bounds should be preserved
        assert_eq!(
            new_state.get_plugin(1).map(|p| p.memory_bounds()),
            Some(1024)
        );
    }
}
