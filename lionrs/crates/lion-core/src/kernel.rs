// Copyright (C) 2026 HaiyangLi
// SPDX-License-Identifier: Apache-2.0
//! High-level Kernel API wrapping the verified state machine.
//!
//! The `Kernel` struct provides an ergonomic interface over the
//! state machine. All operations delegate to the verified core.

use crate::state::{PluginState, State};
use crate::{CapId, Capability, Error, PluginId, SecurityLevel, Size, Step};

/// High-level kernel interface wrapping the verified state machine.
///
/// All state transitions go through `State` and `Step`,
/// which have machine-checked correspondence to the Lean specification.
///
/// # Examples
///
/// ```
/// use lion_core::{Kernel, SecurityLevel};
///
/// let mut kernel = Kernel::new();
/// kernel.register_plugin(1, SecurityLevel::Public, 4096).unwrap();
/// assert_eq!(kernel.plugin_count(), 1);
/// assert_eq!(kernel.plugin_level(1), Some(SecurityLevel::Public));
/// ```
#[derive(Debug, Clone)]
pub struct Kernel {
    state: State,
}

impl Kernel {
    /// Create a new kernel with empty state.
    pub fn new() -> Self {
        Kernel {
            state: State::empty(),
        }
    }

    /// Get a reference to the underlying verified state.
    #[inline]
    pub fn state(&self) -> &State {
        &self.state
    }

    /// Get the current logical time.
    #[inline]
    pub fn time(&self) -> u64 {
        self.state.time()
    }

    /// Advance the logical clock by one tick.
    ///
    /// # Errors
    ///
    /// Returns `Error` if the time counter would overflow u64::MAX.
    pub fn tick(&mut self) -> Result<(), Error> {
        self.state.tick()?;
        Ok(())
    }

    /// Execute a step against the current state.
    ///
    /// The step is validated and executed atomically. On success,
    /// the internal state is updated. On failure, the state is unchanged.
    ///
    /// # Errors
    ///
    /// Returns `Error::Step` if the step's preconditions are not met or execution fails.
    pub fn execute(&mut self, step: &Step) -> Result<(), Error> {
        let new_state = step.execute(&self.state)?;
        self.state = new_state;
        Ok(())
    }

    /// Execute a step by mutating state in place (production path).
    ///
    /// Same validation as `execute` but avoids the full-state clone.
    /// On failure, partial mutations may have occurred -- callers should
    /// treat failure as non-recoverable (same as the pure path which
    /// discards the failed clone).
    ///
    /// # Errors
    ///
    /// Returns `Error::Step` if the step's preconditions are not met or execution fails.
    pub fn execute_mut(&mut self, step: &Step) -> Result<(), Error> {
        step.execute_mut(&mut self.state).map_err(Error::Step)
    }

    /// Register a new plugin with the given security level and memory size.
    ///
    /// # Errors
    ///
    /// Returns an error if a plugin with the same ID already exists.
    pub fn register_plugin(
        &mut self,
        id: PluginId,
        level: SecurityLevel,
        mem_size: Size,
    ) -> Result<(), Error> {
        let ps = PluginState::empty(level, mem_size);
        self.state.insert_plugin(id, ps)?;
        Ok(())
    }

    /// Get the security level of a plugin.
    pub fn plugin_level(&self, id: PluginId) -> Option<SecurityLevel> {
        self.state.plugin_level(id)
    }

    /// Get a capability by ID.
    pub fn get_cap(&self, cap_id: CapId) -> Option<&Capability> {
        self.state.get_cap(cap_id)
    }

    /// Check if a capability is valid (not revoked).
    pub fn cap_is_valid(&self, cap_id: CapId) -> bool {
        self.state.cap_is_valid(cap_id)
    }

    /// Check if a plugin holds a specific capability.
    pub fn plugin_holds(&self, pid: PluginId, cap_id: CapId) -> bool {
        self.state.plugin_holds(pid, cap_id)
    }

    /// Delegate a capability to a target plugin.
    ///
    /// The kernel mints the child capability internally:
    /// - Validates the parent exists and is valid
    /// - Allocates a fresh capability ID
    /// - Intersects requested rights with parent rights
    /// - Computes the HMAC seal with the current key
    /// - Inserts the child and grants it to the target plugin
    ///
    /// # Arguments
    /// * `parent_id` - The parent capability to derive from
    /// * `target` - The plugin to receive the delegated capability
    /// * `requested_rights` - The rights requested (will be intersected with parent)
    ///
    /// # Errors
    ///
    /// Returns `Error::Capability(CapabilityError::Revoked)` if the parent capability is revoked.
    /// Returns `Error::Capability(CapabilityError::EmptyRights)` if the rights intersection is empty.
    /// Returns `Error::Kernel(KernelError::CapNotFound)` if the parent capability does not exist.
    /// Returns `Error::Kernel(KernelError::CapIdExhausted)` if the capability ID space is exhausted.
    /// Returns `Error::Kernel(KernelError::CapIdCollision)` if the allocated ID already exists (should not happen).
    pub fn delegate_cap(
        &mut self,
        parent_id: CapId,
        target: PluginId,
        requested_rights: crate::Rights,
    ) -> Result<CapId, Error> {
        let parent = self
            .state
            .get_cap(parent_id)
            .cloned()
            .ok_or(crate::state::KernelError::CapNotFound(parent_id))?;

        if !parent.is_valid() {
            return Err(crate::CapabilityError::Revoked.into());
        }

        let child_rights = requested_rights.intersection(parent.rights());
        if child_rights.is_empty() {
            return Err(crate::CapabilityError::EmptyRights.into());
        }

        let id = self.state.kernel_mut().alloc_cap_id()?;
        let epoch = self.state.kernel().key_epoch();
        let payload = crate::CapPayload::new(
            target,
            parent.target(),
            child_rights.clone(),
            Some(parent_id),
            epoch,
        );
        let sig = crate::crypto::seal_payload(self.state.kernel().hmac_key(), &payload);

        let child = Capability::new(
            id,
            target,
            parent.target(),
            child_rights,
            Some(parent_id),
            epoch,
            sig,
        )?;

        self.state.apply_cap_delegate_mut(child, target)?;
        Ok(id)
    }

    /// Insert a pre-formed capability (kernel-internal / test use only).
    ///
    /// This bypasses kernel minting. Use `delegate_cap()` for the safe delegation path.
    ///
    /// # Errors
    ///
    /// Returns `Error::Kernel(KernelError::CapIdCollision)` if a capability with the same ID already exists.
    pub fn insert_cap_raw(&mut self, cap: Capability, target: PluginId) -> Result<(), Error> {
        self.state.apply_cap_delegate_mut(cap, target)?;
        Ok(())
    }

    /// Revoke a capability transitively.
    ///
    /// The capability and all capabilities derived from it are marked invalid.
    /// Uses the O(k) children-index fast path with proper BFS traversal.
    ///
    /// # Errors
    ///
    /// Returns `Error::Kernel(KernelError::CapNotFound)` if the capability does not exist.
    pub fn revoke_cap(&mut self, cap_id: CapId) -> Result<(), Error> {
        self.state.apply_cap_revoke_mut(cap_id)?;
        Ok(())
    }

    /// Allocate a memory resource, returning its address.
    pub fn alloc(&mut self, owner: PluginId, size: Size) -> u64 {
        self.state.apply_alloc_mut(owner, size)
    }

    /// Free a memory resource.
    ///
    /// # Errors
    ///
    /// Returns an error if valid capabilities still target this address
    /// (temporal safety) or if the address is not live.
    pub fn free(&mut self, addr: u64) -> Result<(), Error> {
        self.state.apply_free_mut(addr)?;
        Ok(())
    }

    /// Get the number of registered plugins.
    #[inline]
    pub fn plugin_count(&self) -> usize {
        self.state.plugin_count()
    }

    /// Get the number of actors.
    #[inline]
    pub fn actor_count(&self) -> usize {
        self.state.actor_count()
    }

    /// Get the number of resources.
    #[inline]
    pub fn resource_count(&self) -> usize {
        self.state.resource_count()
    }

    /// Get the number of workflows.
    #[inline]
    pub fn workflow_count(&self) -> usize {
        self.state.workflow_count()
    }
}

impl Default for Kernel {
    fn default() -> Self {
        Self::new()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{KernelOp, Right, Rights};

    #[test]
    fn test_new_kernel() {
        let k = Kernel::new();
        assert_eq!(k.time(), 0);
        assert_eq!(k.plugin_count(), 0);
    }

    #[test]
    fn test_tick() {
        let mut k = Kernel::new();
        k.tick().unwrap();
        assert_eq!(k.time(), 1);
        k.tick().unwrap();
        assert_eq!(k.time(), 2);
    }

    #[test]
    fn test_register_plugin() {
        let mut k = Kernel::new();
        k.register_plugin(1, SecurityLevel::Public, 4096).unwrap();
        assert_eq!(k.plugin_count(), 1);
        assert_eq!(k.plugin_level(1), Some(SecurityLevel::Public));
    }

    #[test]
    fn test_register_duplicate_plugin() {
        let mut k = Kernel::new();
        k.register_plugin(1, SecurityLevel::Public, 0).unwrap();
        let result = k.register_plugin(1, SecurityLevel::Internal, 0);
        assert!(result.is_err());
    }

    #[test]
    fn test_delegate_and_revoke() {
        let mut k = Kernel::new();
        k.register_plugin(1, SecurityLevel::Public, 0).unwrap();

        // Insert a root capability via raw path (kernel bootstrapping)
        let cap = Capability::new(
            100,
            1,
            1,
            Rights::singleton(Right::Read),
            None,
            0,
            crate::SealedTag::empty(),
        )
        .unwrap();

        k.insert_cap_raw(cap, 1).unwrap();
        assert!(k.cap_is_valid(100));
        assert!(k.plugin_holds(1, 100));

        k.revoke_cap(100).unwrap();
        assert!(!k.cap_is_valid(100));
    }

    #[test]
    fn test_delegate_cap_kernel_minted() {
        let mut k = Kernel::new();
        k.register_plugin(1, SecurityLevel::Public, 0).unwrap();
        k.register_plugin(2, SecurityLevel::Public, 0).unwrap();

        // Insert a root capability
        let root = Capability::new(
            100,
            1,
            42,
            Rights::singleton(Right::Read),
            None,
            0,
            crate::SealedTag::empty(),
        )
        .unwrap();
        k.insert_cap_raw(root, 1).unwrap();

        // Delegate via kernel minting
        let child_id = k
            .delegate_cap(100, 2, Rights::singleton(Right::Read))
            .unwrap();

        assert!(k.cap_is_valid(child_id));
        assert!(k.plugin_holds(2, child_id));

        // Child rights should be at most parent rights
        let child = k.get_cap(child_id).unwrap();
        assert!(child.rights().is_subset_of(&Rights::singleton(Right::Read)));
        assert_eq!(child.parent(), Some(100));
    }

    #[test]
    fn test_delegate_cap_revoked_parent_fails() {
        let mut k = Kernel::new();
        k.register_plugin(1, SecurityLevel::Public, 0).unwrap();
        k.register_plugin(2, SecurityLevel::Public, 0).unwrap();

        let root = Capability::new(
            100,
            1,
            42,
            Rights::singleton(Right::Read),
            None,
            0,
            crate::SealedTag::empty(),
        )
        .unwrap();
        k.insert_cap_raw(root, 1).unwrap();
        k.revoke_cap(100).unwrap();

        // Should fail: parent is revoked
        let result = k.delegate_cap(100, 2, Rights::singleton(Right::Read));
        assert!(result.is_err());
    }

    #[test]
    fn test_delegate_cap_empty_rights_intersection_fails() {
        let mut k = Kernel::new();
        k.register_plugin(1, SecurityLevel::Public, 0).unwrap();
        k.register_plugin(2, SecurityLevel::Public, 0).unwrap();

        let root = Capability::new(
            100,
            1,
            42,
            Rights::singleton(Right::Read),
            None,
            0,
            crate::SealedTag::empty(),
        )
        .unwrap();
        k.insert_cap_raw(root, 1).unwrap();

        // Request Write when parent only has Read -- intersection is empty
        let result = k.delegate_cap(100, 2, Rights::singleton(Right::Write));
        assert!(result.is_err());
    }

    #[test]
    fn test_alloc_and_free() {
        let mut k = Kernel::new();
        let addr = k.alloc(1, 1024);
        k.free(addr).unwrap();
    }

    #[test]
    fn test_execute_time_tick() {
        let mut k = Kernel::new();
        let step = Step::KernelInternal {
            op: KernelOp::TimeTick,
        };
        k.execute(&step).unwrap();
        assert_eq!(k.time(), 1);
    }

    // ============== execute vs execute_mut EQUIVALENCE TESTS ==============

    /// Helper: assert two kernel states are observably equal.
    fn assert_kernels_eq(a: &Kernel, b: &Kernel) {
        assert_eq!(a.time(), b.time(), "time mismatch");
        assert_eq!(a.plugin_count(), b.plugin_count(), "plugin_count mismatch");
        assert_eq!(a.actor_count(), b.actor_count(), "actor_count mismatch");
        assert_eq!(
            a.resource_count(),
            b.resource_count(),
            "resource_count mismatch"
        );
        assert_eq!(
            a.workflow_count(),
            b.workflow_count(),
            "workflow_count mismatch"
        );
    }

    #[test]
    fn test_equiv_time_tick() {
        let base = Kernel::new();

        let mut k_pure = base.clone();
        let mut k_mut = base;

        let step = Step::KernelInternal {
            op: KernelOp::TimeTick,
        };

        k_pure.execute(&step).unwrap();
        k_mut.execute_mut(&step).unwrap();

        assert_kernels_eq(&k_pure, &k_mut);
    }

    #[test]
    fn test_equiv_plugin_internal() {
        use crate::state::ActorRuntime;
        use crate::step::PluginInternal;

        let mut base = Kernel::new();
        base.register_plugin(1, SecurityLevel::Public, 1024)
            .unwrap();
        // Insert actor so consume_mailbox works
        base.state.insert_actor(1, ActorRuntime::empty(10)).unwrap();

        let mut k_pure = base.clone();
        let mut k_mut = base;

        let step = Step::PluginInternal {
            pid: 1,
            pi: PluginInternal::with_writes(vec![(0, 64)]),
        };

        k_pure.execute(&step).unwrap();
        k_mut.execute_mut(&step).unwrap();

        assert_kernels_eq(&k_pure, &k_mut);
        assert_eq!(
            k_pure.plugin_level(1),
            k_mut.plugin_level(1),
            "plugin level diverged"
        );
    }

    #[test]
    fn test_equiv_route_one() {
        use crate::state::{ActorRuntime, Message};

        let mut base = Kernel::new();
        let mut actor = ActorRuntime::empty(10);
        actor.enqueue_pending_mut(Message::new(1, 2, 1, SecurityLevel::Public, vec![1, 2]));
        base.state.insert_actor(1, actor).unwrap();

        let mut k_pure = base.clone();
        let mut k_mut = base;

        let step = Step::KernelInternal {
            op: KernelOp::RouteOne { dst: 1 },
        };

        k_pure.execute(&step).unwrap();
        k_mut.execute_mut(&step).unwrap();

        assert_kernels_eq(&k_pure, &k_mut);

        let a_pure = k_pure.state().get_actor(1).unwrap();
        let a_mut = k_mut.state().get_actor(1).unwrap();
        assert_eq!(a_pure.pending_len(), a_mut.pending_len());
        assert_eq!(a_pure.mailbox_len(), a_mut.mailbox_len());
    }

    #[test]
    fn test_equiv_unblock_send() {
        use crate::state::ActorRuntime;

        let mut base = Kernel::new();
        let mut actor = ActorRuntime::empty(10);
        actor.set_blocked_mut(42);
        base.state.insert_actor(1, actor).unwrap();

        let mut k_pure = base.clone();
        let mut k_mut = base;

        let step = Step::KernelInternal {
            op: KernelOp::UnblockSend { dst: 1 },
        };

        k_pure.execute(&step).unwrap();
        k_mut.execute_mut(&step).unwrap();

        assert_kernels_eq(&k_pure, &k_mut);

        let a_pure = k_pure.state().get_actor(1).unwrap();
        let a_mut = k_mut.state().get_actor(1).unwrap();
        assert_eq!(a_pure.is_blocked(), a_mut.is_blocked());
        assert!(!a_pure.is_blocked());
    }

    #[test]
    fn test_equiv_mem_free() {
        let mut base = Kernel::new();
        let addr = base.alloc(1, 256);

        let mut k_pure = base.clone();
        let mut k_mut = base;

        let step = Step::MemFree { caller: 1, addr };

        k_pure.execute(&step).unwrap();
        k_mut.execute_mut(&step).unwrap();

        assert_kernels_eq(&k_pure, &k_mut);
        assert!(k_pure.state().ghost().is_freed(addr));
        assert!(k_mut.state().ghost().is_freed(addr));
    }

    #[test]
    fn test_equiv_cap_revoke() {
        let mut base = Kernel::new();
        base.register_plugin(1, SecurityLevel::Public, 0).unwrap();

        let cap = Capability::new(
            100,
            1,
            42,
            Rights::singleton(Right::Read),
            None,
            0,
            crate::SealedTag::empty(),
        )
        .unwrap();
        base.insert_cap_raw(cap, 1).unwrap();

        let mut k_pure = base.clone();
        let mut k_mut = base;

        let step = Step::CapRevoke {
            caller: 1,
            cap_id: 100,
        };

        k_pure.execute(&step).unwrap();
        k_mut.execute_mut(&step).unwrap();

        assert_kernels_eq(&k_pure, &k_mut);
        assert!(!k_pure.cap_is_valid(100));
        assert!(!k_mut.cap_is_valid(100));
    }

    #[test]
    fn test_equiv_cap_revoke_delegation_tree() {
        let mut base = Kernel::new();
        base.register_plugin(1, SecurityLevel::Public, 0).unwrap();

        // Build 3-deep delegation tree: 100 -> 101 -> 102, plus unrelated 103
        let cap_root = Capability::new(
            100,
            1,
            42,
            Rights::singleton(Right::Read),
            None,
            0,
            crate::SealedTag::empty(),
        )
        .unwrap();
        base.insert_cap_raw(cap_root, 1).unwrap();

        let cap_child = Capability::new(
            101,
            1,
            42,
            Rights::singleton(Right::Read),
            Some(100),
            0,
            crate::SealedTag::empty(),
        )
        .unwrap();
        base.insert_cap_raw(cap_child, 1).unwrap();

        let cap_grandchild = Capability::new(
            102,
            1,
            42,
            Rights::singleton(Right::Read),
            Some(101),
            0,
            crate::SealedTag::empty(),
        )
        .unwrap();
        base.insert_cap_raw(cap_grandchild, 1).unwrap();

        let cap_unrelated = Capability::new(
            103,
            1,
            42,
            Rights::singleton(Right::Read),
            None,
            0,
            crate::SealedTag::empty(),
        )
        .unwrap();
        base.insert_cap_raw(cap_unrelated, 1).unwrap();

        let mut k_pure = base.clone();
        let mut k_mut = base;

        let step = Step::CapRevoke {
            caller: 1,
            cap_id: 100,
        };

        k_pure.execute(&step).unwrap();
        k_mut.execute_mut(&step).unwrap();

        assert_kernels_eq(&k_pure, &k_mut);

        // Root and descendants revoked
        assert!(!k_pure.cap_is_valid(100));
        assert!(!k_pure.cap_is_valid(101));
        assert!(!k_pure.cap_is_valid(102));
        // Unrelated cap preserved
        assert!(k_pure.cap_is_valid(103));

        // Same for mut path
        assert!(!k_mut.cap_is_valid(100));
        assert!(!k_mut.cap_is_valid(101));
        assert!(!k_mut.cap_is_valid(102));
        assert!(k_mut.cap_is_valid(103));
    }

    #[test]
    fn test_equiv_error_paths_match() {
        // Both paths should fail identically for double-free
        let mut k_pure = Kernel::new();
        let addr = k_pure.alloc(1, 256);
        k_pure.free(addr).unwrap();
        let mut k_mut = k_pure.clone();

        let step = Step::MemFree { caller: 1, addr };

        let r_pure = k_pure.execute(&step);
        let r_mut = k_mut.execute_mut(&step);

        assert!(r_pure.is_err(), "pure should fail on double-free");
        assert!(r_mut.is_err(), "mut should fail on double-free");
    }
}
