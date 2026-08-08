// Copyright (C) 2026 HaiyangLi
// SPDX-License-Identifier: Apache-2.0
//! Lion State Plugin
//!
//! Corresponds to: Lion/State/Plugin.lean
//!
//! Plugin state for WASM sandbox instances.
//!
//! Design Note (from Lean): heldCaps stores CapId references, not Capability copies.
//! This follows the Handle/State Separation pattern:
//! - Plugins hold handles (CapId) - immutable references
//! - Kernel holds state (Capability with valid flag) - mutable
//! - Validity is checked at USE time via kernel table lookup
//! - Revocation just flips valid=false in table, no plugin cleanup needed

use super::LinearMemory;
use crate::types::{CapId, SecurityLevel, Size};

/// Error type for plugin operations
#[derive(Debug)]
pub enum PluginError {
    /// Capability not held by this plugin
    CapNotHeld(CapId),
    /// Memory access out of bounds (addr, len, bounds)
    MemoryOutOfBounds(u64, u64, u64),
    /// Memory quota exceeded (requested, used, quota)
    MemoryQuotaExceeded(u64, u64, u64),
    /// Capability quota exceeded (current, quota)
    CapQuotaExceeded(u64, u64),
    /// IPC queue limit exceeded (current, limit)
    IpcQueueLimitExceeded(u64, u64),
}

/// Plugin-local state (WASM globals, tables, etc.)
///
/// Corresponds to Lean: `def PluginLocal := Unit`
///
/// Concrete (Unit) for derived instances - actual state is abstract.
/// In Rust, we use an empty struct as the equivalent of Unit.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct PluginLocal;

/// Default quota value: half of u64::MAX to allow arithmetic headroom
/// Corresponds to Lean: `def defaultQuota : Nat := (2^63 : Nat)`
pub const DEFAULT_QUOTA: u64 = u64::MAX / 2;

/// State of a single plugin instance
///
/// Corresponds to Lean: `@[ext] structure PluginState`
///
/// INVARIANTS:
/// - heldCaps contains only capability IDs, not actual capabilities
/// - Validity of held capabilities is checked at use time via kernel lookup
/// - Memory bounds are immutable after creation
/// - Resource usage must not exceed quotas (DoS prevention)
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PluginState {
    /// Security classification level
    ///
    /// Corresponds to Lean: `level : SecurityLevel`
    pub(crate) level: SecurityLevel,

    /// Linear memory sandbox
    ///
    /// Corresponds to Lean: `memory : LinearMemory`
    pub(crate) memory: LinearMemory,

    /// Capability IDs held (handles, not copies)
    ///
    /// Corresponds to Lean: `heldCaps : Finset CapId`
    /// NOTE: Using Vec with sorted uniqueness.
    pub(crate) held_caps: Vec<CapId>,

    /// Opaque internal state
    ///
    /// Corresponds to Lean: `localState : PluginLocal`
    pub(crate) local_state: PluginLocal,

    // Resource quotas (DoS prevention)
    // Corresponds to Lean quota fields in PluginState
    /// Maximum memory allocation in bytes
    ///
    /// Corresponds to Lean: `memoryQuota : Nat`
    pub(crate) memory_quota: u64,

    /// Current memory usage in bytes
    ///
    /// Corresponds to Lean: `memoryUsed : Nat`
    pub(crate) memory_used: u64,

    /// Maximum held capabilities
    ///
    /// Corresponds to Lean: `capQuota : Nat`
    pub(crate) cap_quota: u64,

    /// Maximum pending IPC messages
    ///
    /// Corresponds to Lean: `ipcQueueLimit : Nat`
    pub(crate) ipc_queue_limit: u64,
}

impl PluginState {
    /// Create empty plugin with given security level and memory size
    ///
    /// Corresponds to Lean: `def PluginState.empty (level : SecurityLevel) (memSize : Nat) : PluginState`
    pub fn empty(level: SecurityLevel, mem_size: Size) -> Self {
        PluginState {
            level,
            memory: LinearMemory::empty(mem_size),
            held_caps: Vec::new(),
            local_state: PluginLocal,
            memory_quota: DEFAULT_QUOTA,
            memory_used: 0,
            cap_quota: DEFAULT_QUOTA,
            ipc_queue_limit: DEFAULT_QUOTA,
        }
    }

    /// Create plugin with custom quotas
    ///
    /// Corresponds to Lean: `def PluginState.withQuotas`
    pub fn with_quotas(
        level: SecurityLevel,
        mem_size: Size,
        memory_quota: u64,
        cap_quota: u64,
        ipc_queue_limit: u64,
    ) -> Self {
        PluginState {
            level,
            memory: LinearMemory::empty(mem_size),
            held_caps: Vec::new(),
            local_state: PluginLocal,
            memory_quota,
            memory_used: 0,
            cap_quota,
            ipc_queue_limit,
        }
    }

    /// Check if plugin holds a capability by ID
    ///
    /// Corresponds to Lean: `def PluginState.holds_cap (ps : PluginState) (capId : CapId) : Prop`
    #[inline]
    pub fn holds_cap(&self, cap_id: CapId) -> bool {
        self.held_caps.binary_search(&cap_id).is_ok()
    }

    /// Grant capability ID to plugin (idempotent)
    ///
    /// Corresponds to Lean: `def PluginState.grant_cap`
    ///
    /// Returns the new PluginState with the capability ID added.
    pub fn grant_cap(&self, cap_id: CapId) -> Self {
        let mut new_caps = self.held_caps.clone();
        Self::insert_cap_sorted(&mut new_caps, cap_id);
        PluginState {
            level: self.level,
            memory: self.memory.clone(),
            held_caps: new_caps,
            local_state: self.local_state,
            memory_quota: self.memory_quota,
            memory_used: self.memory_used,
            cap_quota: self.cap_quota,
            ipc_queue_limit: self.ipc_queue_limit,
        }
    }

    /// Helper: insert cap_id into sorted vec, maintaining uniqueness
    fn insert_cap_sorted(caps: &mut Vec<CapId>, cap_id: CapId) {
        match caps.binary_search(&cap_id) {
            Ok(_) => {} // Already present
            Err(pos) => caps.insert(pos, cap_id),
        }
    }

    /// Grant capability ID to plugin (mutating version)
    pub fn grant_cap_mut(&mut self, cap_id: CapId) {
        Self::insert_cap_sorted(&mut self.held_caps, cap_id);
    }

    /// Remove capability ID from plugin
    ///
    /// Corresponds to Lean: `def PluginState.revoke_cap`
    ///
    /// Returns the new PluginState with the capability ID removed.
    pub fn revoke_cap(&self, cap_id: CapId) -> Self {
        let mut new_caps = self.held_caps.clone();
        Self::remove_cap_sorted(&mut new_caps, cap_id);
        PluginState {
            level: self.level,
            memory: self.memory.clone(),
            held_caps: new_caps,
            local_state: self.local_state,
            memory_quota: self.memory_quota,
            memory_used: self.memory_used,
            cap_quota: self.cap_quota,
            ipc_queue_limit: self.ipc_queue_limit,
        }
    }

    /// Helper: remove cap_id from sorted vec
    fn remove_cap_sorted(caps: &mut Vec<CapId>, cap_id: CapId) {
        if let Ok(pos) = caps.binary_search(&cap_id) {
            caps.remove(pos);
        }
    }

    /// Remove capability ID from plugin (mutating version)
    pub fn revoke_cap_mut(&mut self, cap_id: CapId) {
        Self::remove_cap_sorted(&mut self.held_caps, cap_id);
    }

    /// Get the security level
    #[inline]
    pub fn level(&self) -> SecurityLevel {
        self.level
    }

    /// Get a reference to the memory
    #[inline]
    pub fn memory(&self) -> &LinearMemory {
        &self.memory
    }

    /// Get a mutable reference to the memory
    #[inline]
    #[allow(dead_code)] // Lean correspondence
    pub(crate) fn memory_mut(&mut self) -> &mut LinearMemory {
        &mut self.memory
    }

    /// Get the number of held capabilities
    #[inline]
    pub fn held_cap_count(&self) -> usize {
        self.held_caps.len()
    }

    /// Get the local state
    #[inline]
    pub fn local_state(&self) -> PluginLocal {
        self.local_state
    }

    /// Get held capability IDs as a Vec
    ///
    /// Note: Returns a cloned Vec.
    /// The Vec is maintained in sorted order.
    pub fn held_caps(&self) -> Vec<CapId> {
        self.held_caps.clone()
    }

    /// Get a reference to held capability IDs
    pub fn held_caps_ref(&self) -> &Vec<CapId> {
        &self.held_caps
    }

    /// Get memory bounds
    #[inline]
    pub fn memory_bounds(&self) -> Size {
        self.memory.bounds()
    }

    // ============== QUOTA OPERATIONS ==============

    /// Check if memory allocation would exceed quota
    ///
    /// Corresponds to Lean: `def PluginState.canAllocMemory`
    /// Returns false if addition would overflow (DoS prevention)
    #[inline]
    pub fn can_alloc_memory(&self, size: u64) -> bool {
        // Use checked_add to detect overflow - if overflow occurs, quota is exceeded
        match self.memory_used.checked_add(size) {
            Some(total) => total <= self.memory_quota,
            None => false, // Overflow means quota exceeded
        }
    }

    /// Check if plugin can hold another capability
    ///
    /// Corresponds to Lean: `def PluginState.canHoldCap`
    #[inline]
    pub fn can_hold_cap(&self) -> bool {
        (self.held_caps.len() as u64) < self.cap_quota
    }

    /// Check if adding to IPC queue would exceed limit
    ///
    /// Corresponds to Lean: `def PluginState.canQueueIpc`
    #[inline]
    pub fn can_queue_ipc(&self, queue_len: u64) -> bool {
        queue_len < self.ipc_queue_limit
    }

    /// Record memory allocation (update used counter)
    ///
    /// Corresponds to Lean: `def PluginState.allocMemory`
    /// Returns false if quota would be exceeded
    pub fn alloc_memory(&mut self, size: u64) -> bool {
        if !self.can_alloc_memory(size) {
            return false;
        }
        self.memory_used = self.memory_used.saturating_add(size);
        true
    }

    /// Record memory deallocation (update used counter)
    ///
    /// Corresponds to Lean: `def PluginState.freeMemory`
    pub fn free_memory(&mut self, size: u64) {
        self.memory_used = self.memory_used.saturating_sub(size);
    }

    /// Get current memory usage
    #[inline]
    pub fn memory_used(&self) -> u64 {
        self.memory_used
    }

    /// Get memory quota
    #[inline]
    pub fn memory_quota(&self) -> u64 {
        self.memory_quota
    }

    /// Get capability quota
    #[inline]
    pub fn cap_quota(&self) -> u64 {
        self.cap_quota
    }

    /// Get IPC queue limit
    #[inline]
    pub fn ipc_queue_limit(&self) -> u64 {
        self.ipc_queue_limit
    }
}

impl Default for PluginState {
    /// Default plugin: Public level (no clearance), empty memory, no caps.
    ///
    /// Corresponds to Lean: `noncomputable instance : Inhabited PluginState`
    ///
    /// This is the semantically correct default - uninitialized plugins
    /// have no security privileges.
    fn default() -> Self {
        PluginState {
            level: SecurityLevel::Public,
            memory: LinearMemory::empty(0),
            held_caps: Vec::new(),
            local_state: PluginLocal,
            memory_quota: DEFAULT_QUOTA,
            memory_used: 0,
            cap_quota: DEFAULT_QUOTA,
            ipc_queue_limit: DEFAULT_QUOTA,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_plugin_local_default() {
        let local = PluginLocal;
        assert_eq!(local, PluginLocal);
    }

    #[test]
    fn test_plugin_state_empty() {
        let ps = PluginState::empty(SecurityLevel::Confidential, 1024);
        assert_eq!(ps.level(), SecurityLevel::Confidential);
        assert_eq!(ps.memory_bounds(), 1024);
        assert_eq!(ps.held_cap_count(), 0);
    }

    #[test]
    fn test_plugin_state_default_level() {
        // Corresponds to Lean theorem: PluginState.default_level
        let ps = PluginState::default();
        assert_eq!(ps.level(), SecurityLevel::Public);
    }

    #[test]
    fn test_plugin_state_grant_cap() {
        let mut ps = PluginState::empty(SecurityLevel::Public, 0);

        // Grant cap
        ps.grant_cap_mut(42);
        assert!(ps.holds_cap(42));
        assert_eq!(ps.held_cap_count(), 1);

        // Grant same cap again (idempotent)
        ps.grant_cap_mut(42);
        assert!(ps.holds_cap(42));
        assert_eq!(ps.held_cap_count(), 1);
    }

    #[test]
    fn test_plugin_state_grant_cap_mem() {
        // Corresponds to Lean theorem: PluginState.grant_cap_mem
        let ps = PluginState::empty(SecurityLevel::Public, 0);
        let ps = ps.grant_cap(42);
        assert!(ps.holds_cap(42));
    }

    #[test]
    fn test_plugin_state_grant_cap_preserves() {
        // Corresponds to Lean theorem: PluginState.grant_cap_preserves
        let ps = PluginState::empty(SecurityLevel::Public, 0);
        let ps = ps.grant_cap(1);
        let ps = ps.grant_cap(2);

        // Both should be held
        assert!(ps.holds_cap(1));
        assert!(ps.holds_cap(2));
    }

    #[test]
    fn test_plugin_state_revoke_cap_not_mem() {
        // Corresponds to Lean theorem: PluginState.revoke_cap_not_mem
        let ps = PluginState::empty(SecurityLevel::Public, 0);
        let ps = ps.grant_cap(42);
        let ps = ps.revoke_cap(42);

        assert!(!ps.holds_cap(42));
        assert_eq!(ps.held_cap_count(), 0);
    }

    #[test]
    fn test_plugin_state_revoke_cap_preserves() {
        // Corresponds to Lean theorem: PluginState.revoke_cap_preserves
        let ps = PluginState::empty(SecurityLevel::Public, 0);
        let ps = ps.grant_cap(1);
        let ps = ps.grant_cap(2);
        let ps = ps.revoke_cap(1);

        // 2 should still be held
        assert!(!ps.holds_cap(1));
        assert!(ps.holds_cap(2));
    }

    #[test]
    fn test_plugin_state_grant_cap_memory() {
        // Corresponds to Lean theorem: PluginState.grant_cap_memory
        let ps = PluginState::empty(SecurityLevel::Public, 1024);
        let old_memory = ps.memory().clone();
        let ps = ps.grant_cap(42);

        // Memory should be unchanged
        assert_eq!(ps.memory(), &old_memory);
    }

    #[test]
    fn test_plugin_state_grant_cap_memory_bounds() {
        // Corresponds to Lean theorem: PluginState.grant_cap_memory_bounds
        let ps = PluginState::empty(SecurityLevel::Public, 1024);
        let ps = ps.grant_cap(42);

        // Bounds should be unchanged
        assert_eq!(ps.memory_bounds(), 1024);
    }

    #[test]
    fn test_plugin_state_grant_cap_level() {
        // Corresponds to Lean theorem: PluginState.grant_cap_level
        let ps = PluginState::empty(SecurityLevel::Confidential, 0);
        let ps = ps.grant_cap(42);

        // Level should be unchanged
        assert_eq!(ps.level(), SecurityLevel::Confidential);
    }

    #[test]
    fn test_plugin_state_grant_cap_local_state() {
        // Corresponds to Lean theorem: PluginState.grant_cap_localState
        let ps = PluginState::empty(SecurityLevel::Public, 0);
        let old_local = ps.local_state();
        let ps = ps.grant_cap(42);

        // Local state should be unchanged
        assert_eq!(ps.local_state(), old_local);
    }

    #[test]
    fn test_plugin_state_held_caps_iterator() {
        let mut ps = PluginState::empty(SecurityLevel::Public, 0);
        ps.grant_cap_mut(1);
        ps.grant_cap_mut(3);
        ps.grant_cap_mut(2);

        // BTreeSet provides deterministic iteration order
        let caps = ps.held_caps();
        assert_eq!(caps, vec![1, 2, 3]);
    }

    // ============== QUOTA TESTS ==============

    #[test]
    fn test_default_quota() {
        let ps = PluginState::default();
        assert_eq!(ps.memory_quota(), DEFAULT_QUOTA);
        assert_eq!(ps.memory_used(), 0);
        assert_eq!(ps.cap_quota(), DEFAULT_QUOTA);
        assert_eq!(ps.ipc_queue_limit(), DEFAULT_QUOTA);
    }

    #[test]
    fn test_with_quotas() {
        let ps = PluginState::with_quotas(
            SecurityLevel::Confidential,
            1024,
            4096, // memory quota
            10,   // cap quota
            100,  // ipc limit
        );
        assert_eq!(ps.level(), SecurityLevel::Confidential);
        assert_eq!(ps.memory_bounds(), 1024);
        assert_eq!(ps.memory_quota(), 4096);
        assert_eq!(ps.cap_quota(), 10);
        assert_eq!(ps.ipc_queue_limit(), 100);
    }

    #[test]
    fn test_can_alloc_memory() {
        let ps = PluginState::with_quotas(
            SecurityLevel::Public,
            0,
            1000, // 1000 byte quota
            10,
            100,
        );

        // Should allow allocation within quota
        assert!(ps.can_alloc_memory(500));
        assert!(ps.can_alloc_memory(1000));

        // Should reject allocation exceeding quota
        assert!(!ps.can_alloc_memory(1001));
    }

    #[test]
    fn test_alloc_memory_tracking() {
        let mut ps = PluginState::with_quotas(SecurityLevel::Public, 0, 1000, 10, 100);

        // First allocation
        assert!(ps.alloc_memory(400));
        assert_eq!(ps.memory_used(), 400);

        // Second allocation
        assert!(ps.alloc_memory(400));
        assert_eq!(ps.memory_used(), 800);

        // Third allocation would exceed quota
        assert!(!ps.alloc_memory(300));
        assert_eq!(ps.memory_used(), 800); // unchanged

        // But smaller allocation still works
        assert!(ps.alloc_memory(200));
        assert_eq!(ps.memory_used(), 1000);
    }

    #[test]
    fn test_free_memory_tracking() {
        let mut ps = PluginState::with_quotas(SecurityLevel::Public, 0, 1000, 10, 100);

        ps.alloc_memory(800);
        assert_eq!(ps.memory_used(), 800);

        ps.free_memory(300);
        assert_eq!(ps.memory_used(), 500);

        // Can allocate again after freeing
        assert!(ps.can_alloc_memory(500));
        assert!(ps.alloc_memory(500));
        assert_eq!(ps.memory_used(), 1000);
    }

    #[test]
    fn test_can_hold_cap() {
        let mut ps = PluginState::with_quotas(
            SecurityLevel::Public,
            0,
            1000,
            3, // max 3 caps
            100,
        );

        assert!(ps.can_hold_cap());
        ps.grant_cap_mut(1);
        assert!(ps.can_hold_cap());
        ps.grant_cap_mut(2);
        assert!(ps.can_hold_cap());
        ps.grant_cap_mut(3);
        assert!(!ps.can_hold_cap()); // at quota now

        // After revoke, can hold more
        ps.revoke_cap_mut(1);
        assert!(ps.can_hold_cap());
    }

    #[test]
    fn test_can_queue_ipc() {
        let ps = PluginState::with_quotas(
            SecurityLevel::Public,
            0,
            1000,
            10,
            5, // max 5 IPC messages
        );

        assert!(ps.can_queue_ipc(0));
        assert!(ps.can_queue_ipc(4));
        assert!(!ps.can_queue_ipc(5));
        assert!(!ps.can_queue_ipc(100));
    }

    #[test]
    fn test_quota_overflow_safety() {
        let mut ps = PluginState::with_quotas(
            SecurityLevel::Public,
            0,
            u64::MAX, // max quota
            10,
            100,
        );

        // Even with max quota, saturating add should not overflow
        ps.memory_used = u64::MAX - 10;
        assert!(!ps.can_alloc_memory(100)); // would overflow
        assert!(ps.can_alloc_memory(5)); // still within bounds
    }
}
