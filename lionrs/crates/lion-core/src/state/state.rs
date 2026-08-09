// Copyright (C) 2026 HaiyangLi
// SPDX-License-Identifier: Apache-2.0
//! Lion State - Unified Global State
//!
//! Corresponds to: Lion/State/State.lean
//!
//! Complete system state for Lion microkernel.
//!
//! NOTE: Uses Vec<(K, V)> instead of BTreeMap.
//! All operations maintain sorted order by key for deterministic behavior.

use super::{ActorRuntime, KernelState, LinearMemory, MetaState, PluginState, WorkflowInstance};
use crate::types::{
    ActorId, CapId, Capability, MemAddr, PluginId, ResourceId, SecurityLevel, Size, Time,
    WorkflowId,
};

/// Error type for state operations
#[derive(Debug)]
pub enum StateError {
    /// Plugin not found
    PluginNotFound(PluginId),
    /// Actor not found
    ActorNotFound(ActorId),
    /// Resource not found
    ResourceNotFound(ResourceId),
    /// Workflow not found
    WorkflowNotFound(WorkflowId),
    /// Capability not found
    CapabilityNotFound(CapId),
    /// Plugin does not hold capability (plugin, cap)
    CapabilityNotHeld(PluginId, CapId),
    /// Isolation violation (active, affected)
    IsolationViolation(PluginId, PluginId),
    /// Dangling capability would result from freeing this address
    DanglingCapability(MemAddr),
    /// Plugin already exists
    PluginExists(PluginId),
    /// Actor already exists
    ActorExists(ActorId),
    /// Resource already exists
    ResourceExists(ResourceId),
    /// Workflow already exists
    WorkflowExists(WorkflowId),
    /// Counter overflow (time or epoch would exceed u64::MAX)
    CounterOverflow(&'static str),
}

impl std::fmt::Display for StateError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            StateError::PluginNotFound(id) => write!(f, "plugin {id} not found"),
            StateError::ActorNotFound(id) => write!(f, "actor {id} not found"),
            StateError::ResourceNotFound(id) => write!(f, "resource {id} not found"),
            StateError::WorkflowNotFound(id) => write!(f, "workflow {id} not found"),
            StateError::CapabilityNotFound(id) => write!(f, "capability {id} not found"),
            StateError::CapabilityNotHeld(pid, cid) => {
                write!(f, "plugin {pid} does not hold capability {cid}")
            }
            StateError::IsolationViolation(active, affected) => {
                write!(f, "isolation violation: {active} affects {affected}")
            }
            StateError::DanglingCapability(addr) => {
                write!(f, "freeing address {addr} would create dangling capability")
            }
            StateError::PluginExists(id) => write!(f, "plugin {id} already exists"),
            StateError::ActorExists(id) => write!(f, "actor {id} already exists"),
            StateError::ResourceExists(id) => write!(f, "resource {id} already exists"),
            StateError::WorkflowExists(id) => write!(f, "workflow {id} already exists"),
            StateError::CounterOverflow(name) => write!(f, "{name} counter overflow"),
        }
    }
}

impl std::error::Error for StateError {}

/// Resource metadata
///
/// Corresponds to Lean: `structure ResourceInfo`
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct ResourceInfo {
    /// Security level of the resource
    ///
    /// Corresponds to Lean: `level : SecurityLevel`
    pub(crate) level: SecurityLevel,
}

impl ResourceInfo {
    /// Create a new ResourceInfo with the given security level
    pub fn new(level: SecurityLevel) -> Self {
        ResourceInfo { level }
    }

    /// Get the security level
    #[inline]
    pub fn level(&self) -> SecurityLevel {
        self.level
    }
}

/// Complete system state
///
/// Corresponds to Lean: `@[ext] structure State`
///
/// INVARIANTS:
/// - All state transitions preserve isolation (non-active plugins unchanged)
/// - Temporal safety: freed resources stay freed
/// - Capability validity is checked at use time
/// - All collections are sorted by key for deterministic iteration
/// - Single source of truth: logical time lives in `kernel.now`, not duplicated here
#[derive(Debug, Clone)]
#[must_use = "state transitions return new state that must be used"]
pub struct State {
    /// Kernel state (TCB) -- owns the authoritative logical clock (`now`)
    ///
    /// Corresponds to Lean: `kernel : KernelState`
    pub(crate) kernel: KernelState,

    /// Plugin states
    ///
    /// Corresponds to Lean: `plugins : PluginId -> PluginState`
    /// INVARIANT: Sorted by PluginId, no duplicates.
    pub(crate) plugins: Vec<(PluginId, PluginState)>,

    /// Actor runtimes
    ///
    /// Corresponds to Lean: `actors : ActorId -> ActorRuntime`
    /// INVARIANT: Sorted by ActorId, no duplicates.
    pub(crate) actors: Vec<(ActorId, ActorRuntime)>,

    /// Resource metadata
    ///
    /// Corresponds to Lean: `resources : ResourceId -> ResourceInfo`
    /// INVARIANT: Sorted by ResourceId, no duplicates.
    pub(crate) resources: Vec<(ResourceId, ResourceInfo)>,

    /// Workflow instances
    ///
    /// Corresponds to Lean: `workflows : WorkflowId -> WorkflowInstance`
    /// INVARIANT: Sorted by WorkflowId, no duplicates.
    pub(crate) workflows: Vec<(WorkflowId, WorkflowInstance)>,

    /// Ghost state for verification
    ///
    /// Corresponds to Lean: `ghost : MetaState`
    pub(crate) ghost: MetaState,
}

impl State {
    /// Create a new empty state
    pub fn empty() -> Self {
        State {
            kernel: KernelState::default(),
            plugins: Vec::new(),
            actors: Vec::new(),
            resources: Vec::new(),
            workflows: Vec::new(),
            ghost: MetaState::empty(),
        }
    }

    // ========================================
    // Helper methods for sorted Vec operations
    // ========================================

    /// Find index of plugin by ID (binary search on sorted vec)
    fn find_plugin_index(&self, pid: PluginId) -> Option<usize> {
        self.plugins
            .binary_search_by_key(&pid, |entry| entry.0)
            .ok()
    }

    /// Find insertion position for plugin (binary search on sorted vec)
    fn find_plugin_insert_pos(&self, pid: PluginId) -> (usize, bool) {
        match self.plugins.binary_search_by_key(&pid, |entry| entry.0) {
            Ok(pos) => (pos, true),
            Err(pos) => (pos, false),
        }
    }

    /// Find index of actor by ID (binary search on sorted vec)
    fn find_actor_index(&self, aid: ActorId) -> Option<usize> {
        self.actors.binary_search_by_key(&aid, |entry| entry.0).ok()
    }

    /// Find insertion position for actor (binary search on sorted vec)
    fn find_actor_insert_pos(&self, aid: ActorId) -> (usize, bool) {
        match self.actors.binary_search_by_key(&aid, |entry| entry.0) {
            Ok(pos) => (pos, true),
            Err(pos) => (pos, false),
        }
    }

    /// Find index of resource by ID (binary search on sorted vec)
    fn find_resource_index(&self, rid: ResourceId) -> Option<usize> {
        self.resources
            .binary_search_by_key(&rid, |entry| entry.0)
            .ok()
    }

    /// Find insertion position for resource (binary search on sorted vec)
    fn find_resource_insert_pos(&self, rid: ResourceId) -> (usize, bool) {
        match self.resources.binary_search_by_key(&rid, |entry| entry.0) {
            Ok(pos) => (pos, true),
            Err(pos) => (pos, false),
        }
    }

    /// Find index of workflow by ID (binary search on sorted vec)
    fn find_workflow_index(&self, wid: WorkflowId) -> Option<usize> {
        self.workflows
            .binary_search_by_key(&wid, |entry| entry.0)
            .ok()
    }

    /// Find insertion position for workflow (binary search on sorted vec)
    fn find_workflow_insert_pos(&self, wid: WorkflowId) -> (usize, bool) {
        match self.workflows.binary_search_by_key(&wid, |entry| entry.0) {
            Ok(pos) => (pos, true),
            Err(pos) => (pos, false),
        }
    }

    // ========================================
    // Public API
    // ========================================

    /// Get plugin memory
    ///
    /// Corresponds to Lean: `def State.plugin_memory`
    pub fn plugin_memory(&self, pid: PluginId) -> Option<&LinearMemory> {
        self.find_plugin_index(pid)
            .map(|idx| self.plugins[idx].1.memory())
    }

    /// Get plugin security level
    ///
    /// Corresponds to Lean: `def State.plugin_level`
    pub fn plugin_level(&self, pid: PluginId) -> Option<SecurityLevel> {
        self.find_plugin_index(pid)
            .map(|idx| self.plugins[idx].1.level())
    }

    /// Get resource security level
    ///
    /// Corresponds to Lean: `def State.resource_level`
    pub fn resource_level(&self, rid: ResourceId) -> Option<SecurityLevel> {
        self.find_resource_index(rid)
            .map(|idx| self.resources[idx].1.level())
    }

    /// Get capability from kernel
    ///
    /// Corresponds to Lean: `def State.get_cap`
    pub fn get_cap(&self, cap_id: CapId) -> Option<&Capability> {
        self.kernel.revocation().get(cap_id)
    }

    /// Check if capability is valid in current state
    ///
    /// Corresponds to Lean: `def State.cap_is_valid`
    pub fn cap_is_valid(&self, cap_id: CapId) -> bool {
        self.kernel.revocation().is_valid(cap_id)
    }

    /// Check if plugin holds capability by ID
    ///
    /// Corresponds to Lean: `def State.plugin_holds`
    pub fn plugin_holds(&self, pid: PluginId, cap_id: CapId) -> bool {
        self.find_plugin_index(pid)
            .is_some_and(|idx| self.plugins[idx].1.holds_cap(cap_id))
    }

    /// Allocate resource, update ghost history
    ///
    /// Corresponds to Lean: `def State.apply_alloc`
    pub fn apply_alloc(&self, owner: PluginId, _size: Size) -> Self {
        let addr = self.ghost.resource_count() as MemAddr;
        let new_ghost = self.ghost.alloc(addr, owner);
        State {
            kernel: self.kernel.clone(),
            plugins: self.plugins.clone(),
            actors: self.actors.clone(),
            resources: self.resources.clone(),
            workflows: self.workflows.clone(),
            ghost: new_ghost,
        }
    }

    /// Allocate resource (mutating version)
    pub fn apply_alloc_mut(&mut self, owner: PluginId, _size: Size) -> MemAddr {
        let addr = self.ghost.resource_count() as MemAddr;
        self.ghost.alloc_mut(addr, owner);
        addr
    }

    /// Free resource, mark as dead in ghost history
    ///
    /// Corresponds to Lean: `def State.apply_free`
    ///
    /// SECURITY: This checks for dangling capabilities before freeing.
    /// If any valid capability targets this address, freeing it would
    /// create a USE-AFTER-FREE vulnerability.
    ///
    /// # Errors
    ///
    /// Returns `StateError::DanglingCapability` if a valid capability still targets the address.
    pub fn apply_free(&self, addr: MemAddr) -> Result<Self, StateError> {
        // GUARD: reject a state that references capabilities no longer present
        // MemAddr = u64, ResourceId = u128; widen addr for comparison
        if self
            .kernel
            .revocation()
            .any_valid_targeting(u128::from(addr))
        {
            return Err(StateError::DanglingCapability(addr));
        }

        Ok(State {
            kernel: self.kernel.clone(),
            plugins: self.plugins.clone(),
            actors: self.actors.clone(),
            resources: self.resources.clone(),
            workflows: self.workflows.clone(),
            ghost: self.ghost.free(addr),
        })
    }

    /// Free resource (mutating version)
    ///
    /// SECURITY: Checks for dangling capabilities before freeing.
    /// Requires the resource to be currently allocated (not unallocated or already freed).
    ///
    /// # Errors
    ///
    /// Returns `StateError::DanglingCapability` if a valid capability still targets the address.
    /// Returns `StateError::ResourceNotFound` if the address was already freed or never allocated.
    pub fn apply_free_mut(&mut self, addr: MemAddr) -> Result<(), StateError> {
        // GUARD: reject a state that references capabilities no longer present
        // MemAddr = u64, ResourceId = u128; widen addr for comparison
        if self
            .kernel
            .revocation()
            .any_valid_targeting(u128::from(addr))
        {
            return Err(StateError::DanglingCapability(addr));
        }
        match self.ghost.free_mut(addr) {
            Ok(()) => Ok(()),
            Err(super::MemoryError::DoubleFree(_)) => {
                Err(StateError::ResourceNotFound(addr.into()))
            }
            Err(super::MemoryError::NotAllocated(_)) => {
                Err(StateError::ResourceNotFound(addr.into()))
            }
            Err(super::MemoryError::UseAfterFree(_)) => {
                Err(StateError::ResourceNotFound(addr.into()))
            }
            Err(super::MemoryError::OutOfBounds(_, _, _)) => {
                Err(StateError::ResourceNotFound(addr.into()))
            }
        }
    }

    /// Revoke capability (single cap)
    ///
    /// Corresponds to Lean: `def State.apply_revoke`
    pub fn apply_revoke(&self, cap_id: CapId) -> Self {
        State {
            kernel: KernelState {
                key_state: self.kernel.key_state().clone(),
                policy: self.kernel.policy().clone(),
                revocation: self.kernel.revocation().revoke(cap_id),
                now: self.kernel.now(),
                next_cap_id: self.kernel.next_cap_id(),
            },
            plugins: self.plugins.clone(),
            actors: self.actors.clone(),
            resources: self.resources.clone(),
            workflows: self.workflows.clone(),
            ghost: self.ghost.clone(),
        }
    }

    /// Revoke capability transitively
    ///
    /// Corresponds to Lean: `def State.apply_cap_revoke`
    pub fn apply_cap_revoke(&self, cap_id: CapId) -> Self {
        State {
            kernel: KernelState {
                key_state: self.kernel.key_state().clone(),
                policy: self.kernel.policy().clone(),
                revocation: self.kernel.revocation().revoke_transitive(cap_id),
                now: self.kernel.now(),
                next_cap_id: self.kernel.next_cap_id(),
            },
            plugins: self.plugins.clone(),
            actors: self.actors.clone(),
            resources: self.resources.clone(),
            workflows: self.workflows.clone(),
            ghost: self.ghost.clone(),
        }
    }

    /// Revoke capability transitively (mutating version)
    ///
    /// Uses the children-index optimized O(k) fast path with
    /// BFS traversal and visited set (no iteration cap).
    ///
    /// # Errors
    ///
    /// Returns `KernelError::CapNotFound` if the capability does not exist.
    pub fn apply_cap_revoke_mut(&mut self, cap_id: CapId) -> Result<(), super::KernelError> {
        self.kernel
            .revocation_mut()
            .revoke_transitive_fast_mut(cap_id)
    }

    /// Delegate capability: insert new cap into kernel and grant to target
    ///
    /// Corresponds to Lean: `def State.apply_cap_delegate`
    ///
    /// Returns error if capability ID already exists (collision).
    ///
    /// # Errors
    ///
    /// Returns `KernelError::CapIdCollision` if a capability with the same ID already exists.
    pub fn apply_cap_delegate(
        &self,
        new_cap: Capability,
        target: PluginId,
    ) -> Result<Self, super::KernelError> {
        let cap_id = new_cap.id();
        let new_revocation = self.kernel.revocation().insert(new_cap)?;
        let mut new_plugins = self.plugins.clone();
        // Find and update the target plugin (binary search on sorted vec)
        if let Ok(idx) = new_plugins.binary_search_by_key(&target, |(pid, _)| *pid) {
            new_plugins[idx].1.grant_cap_mut(cap_id);
        }

        Ok(State {
            kernel: KernelState {
                key_state: self.kernel.key_state().clone(),
                policy: self.kernel.policy().clone(),
                revocation: new_revocation,
                now: self.kernel.now(),
                next_cap_id: self.kernel.next_cap_id(),
            },
            plugins: new_plugins,
            actors: self.actors.clone(),
            resources: self.resources.clone(),
            workflows: self.workflows.clone(),
            ghost: self.ghost.clone(),
        })
    }

    /// Delegate capability (mutating version)
    ///
    /// # Errors
    ///
    /// Returns `KernelError::CapIdCollision` if a capability with the same ID already exists.
    pub fn apply_cap_delegate_mut(
        &mut self,
        new_cap: Capability,
        target: PluginId,
    ) -> Result<(), super::KernelError> {
        let cap_id = new_cap.id();
        self.kernel.revocation_mut().insert_mut(new_cap)?;
        if let Some(idx) = self.find_plugin_index(target) {
            self.plugins[idx].1.grant_cap_mut(cap_id);
        }
        Ok(())
    }

    /// Check if memory isolation is preserved after step
    ///
    /// Corresponds to Lean: `def State.preserves_isolation`
    pub fn preserves_isolation(&self, other: &State, active: PluginId) -> bool {
        self.plugins.iter().all(|(pid, ps)| {
            if *pid == active {
                return true;
            }
            match other.plugins.binary_search_by_key(pid, |(id, _)| *id) {
                Ok(idx) => *ps == other.plugins[idx].1,
                Err(_) => false,
            }
        })
    }

    /// Check temporal safety: freed resources stay freed
    ///
    /// Corresponds to Lean: `def State.temporal_safety`
    pub fn temporal_safety(&self, other: &State) -> bool {
        self.ghost
            .freed_set
            .iter()
            .all(|addr| other.ghost.is_freed(*addr))
    }

    /// Get the current logical time (delegates to kernel.now -- single source of truth)
    ///
    /// Corresponds to Lean: `def State.time`
    #[inline]
    pub fn time(&self) -> Time {
        self.kernel.now()
    }

    /// Advance logical time via the kernel clock (single source of truth).
    ///
    /// Uses checked arithmetic so overflow is explicit rather than silent.
    ///
    /// # Errors
    ///
    /// Returns `StateError::CounterOverflow` if the time counter would exceed u64::MAX.
    pub fn tick(&mut self) -> Result<(), StateError> {
        self.kernel
            .tick_checked()
            .map_err(|_| StateError::CounterOverflow("time"))
    }

    /// Get a reference to the kernel state
    #[inline]
    pub fn kernel(&self) -> &KernelState {
        &self.kernel
    }

    /// Get a mutable reference to the kernel state
    #[inline]
    #[allow(dead_code)] // Lean correspondence
    pub(crate) fn kernel_mut(&mut self) -> &mut KernelState {
        &mut self.kernel
    }

    /// Get a reference to the ghost state
    #[inline]
    pub fn ghost(&self) -> &MetaState {
        &self.ghost
    }

    /// Get a plugin state
    pub fn get_plugin(&self, pid: PluginId) -> Option<&PluginState> {
        self.find_plugin_index(pid).map(|idx| &self.plugins[idx].1)
    }

    /// Get a mutable plugin state
    pub fn get_plugin_mut(&mut self, pid: PluginId) -> Option<&mut PluginState> {
        self.find_plugin_index(pid)
            .map(|idx| &mut self.plugins[idx].1)
    }

    /// Insert a plugin (checks for collision)
    ///
    /// SECURITY: Returns error if plugin already exists to prevent silent overwrite.
    ///
    /// # Errors
    ///
    /// Returns `StateError::PluginExists` if a plugin with the given ID is already registered.
    pub fn insert_plugin(&mut self, pid: PluginId, ps: PluginState) -> Result<(), StateError> {
        let (pos, exists) = self.find_plugin_insert_pos(pid);
        if exists {
            return Err(StateError::PluginExists(pid));
        }
        self.plugins.insert(pos, (pid, ps));
        Ok(())
    }

    /// Get an actor runtime
    pub fn get_actor(&self, aid: ActorId) -> Option<&ActorRuntime> {
        self.find_actor_index(aid).map(|idx| &self.actors[idx].1)
    }

    /// Get a mutable actor runtime
    pub fn get_actor_mut(&mut self, aid: ActorId) -> Option<&mut ActorRuntime> {
        self.find_actor_index(aid)
            .map(|idx| &mut self.actors[idx].1)
    }

    /// Insert an actor (checks for collision)
    ///
    /// SECURITY: Returns error if actor already exists to prevent silent overwrite.
    ///
    /// # Errors
    ///
    /// Returns `StateError::ActorExists` if an actor with the given ID is already registered.
    pub fn insert_actor(&mut self, aid: ActorId, ar: ActorRuntime) -> Result<(), StateError> {
        let (pos, exists) = self.find_actor_insert_pos(aid);
        if exists {
            return Err(StateError::ActorExists(aid));
        }
        self.actors.insert(pos, (aid, ar));
        Ok(())
    }

    /// Get a resource
    pub fn get_resource(&self, rid: ResourceId) -> Option<&ResourceInfo> {
        self.find_resource_index(rid)
            .map(|idx| &self.resources[idx].1)
    }

    /// Insert a resource (checks for collision)
    ///
    /// SECURITY: Returns error if resource already exists to prevent silent overwrite.
    ///
    /// # Errors
    ///
    /// Returns `StateError::ResourceExists` if a resource with the given ID is already registered.
    pub fn insert_resource(&mut self, rid: ResourceId, ri: ResourceInfo) -> Result<(), StateError> {
        let (pos, exists) = self.find_resource_insert_pos(rid);
        if exists {
            return Err(StateError::ResourceExists(rid));
        }
        self.resources.insert(pos, (rid, ri));
        Ok(())
    }

    /// Get a workflow
    pub fn get_workflow(&self, wid: WorkflowId) -> Option<&WorkflowInstance> {
        self.find_workflow_index(wid)
            .map(|idx| &self.workflows[idx].1)
    }

    /// Get a mutable workflow
    pub fn get_workflow_mut(&mut self, wid: WorkflowId) -> Option<&mut WorkflowInstance> {
        self.find_workflow_index(wid)
            .map(|idx| &mut self.workflows[idx].1)
    }

    /// Insert a workflow (checks for collision)
    ///
    /// SECURITY: Returns error if workflow already exists to prevent silent overwrite.
    ///
    /// # Errors
    ///
    /// Returns `StateError::WorkflowExists` if a workflow with the given ID is already registered.
    pub fn insert_workflow(
        &mut self,
        wid: WorkflowId,
        wi: WorkflowInstance,
    ) -> Result<(), StateError> {
        let (pos, exists) = self.find_workflow_insert_pos(wid);
        if exists {
            return Err(StateError::WorkflowExists(wid));
        }
        self.workflows.insert(pos, (wid, wi));
        Ok(())
    }

    /// Get the number of plugins
    #[inline]
    pub fn plugin_count(&self) -> usize {
        self.plugins.len()
    }

    /// Get the number of actors
    #[inline]
    pub fn actor_count(&self) -> usize {
        self.actors.len()
    }

    /// Get the number of resources
    #[inline]
    pub fn resource_count(&self) -> usize {
        self.resources.len()
    }

    /// Get the number of workflows
    #[inline]
    pub fn workflow_count(&self) -> usize {
        self.workflows.len()
    }

    /// Get all plugin IDs
    pub fn plugin_ids(&self) -> Vec<PluginId> {
        let mut ids = Vec::with_capacity(self.plugins.len());
        let mut i = 0;
        while i < self.plugins.len() {
            ids.push(self.plugins[i].0);
            i += 1;
        }
        ids
    }

    /// Get all resource IDs
    pub fn resource_ids(&self) -> Vec<ResourceId> {
        let mut ids = Vec::with_capacity(self.resources.len());
        let mut i = 0;
        while i < self.resources.len() {
            ids.push(self.resources[i].0);
            i += 1;
        }
        ids
    }
}

impl Default for State {
    fn default() -> Self {
        State::empty()
    }
}

/// Check if two states are low-equivalent (one-way)
///
/// Corresponds to Lean: `def low_equivalent_left`
///
/// All low (<= L) components of s1 agree with s2.
#[allow(dead_code)] // Lean correspondence
pub fn low_equivalent_left(l: SecurityLevel, s1: &State, s2: &State) -> bool {
    plugins_low_equiv(l, s1, s2) && resources_low_equiv(l, s1, s2)
}

/// Helper: check all low plugins match
#[allow(dead_code)] // Lean correspondence
fn plugins_low_equiv(l: SecurityLevel, s1: &State, s2: &State) -> bool {
    s1.plugins.iter().all(|(pid, ps1)| {
        if ps1.level() > l {
            return true;
        }
        match s2.plugins.binary_search_by_key(pid, |(id, _)| *id) {
            Ok(idx) => *ps1 == s2.plugins[idx].1,
            Err(_) => false,
        }
    })
}

/// Helper: check all low resources match
#[allow(dead_code)] // Lean correspondence
fn resources_low_equiv(l: SecurityLevel, s1: &State, s2: &State) -> bool {
    s1.resources.iter().all(|(rid, ri1)| {
        if ri1.level() > l {
            return true;
        }
        match s2.resources.binary_search_by_key(rid, |(id, _)| *id) {
            Ok(idx) => *ri1 == s2.resources[idx].1,
            Err(_) => false,
        }
    })
}

/// Check if two states are low-equivalent (symmetric)
///
/// Corresponds to Lean: `def low_equivalent`
///
/// States agree on all data at level <= L (for noninterference).
#[allow(dead_code)] // Lean correspondence
pub fn low_equivalent(l: SecurityLevel, s1: &State, s2: &State) -> bool {
    low_equivalent_left(l, s1, s2) && low_equivalent_left(l, s2, s1)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::types::{Right, Rights, SealedTag};

    fn make_test_cap(id: CapId) -> Capability {
        Capability::new(
            id,
            1,
            1,
            Rights::singleton(Right::Read),
            None,
            0,
            SealedTag::empty(),
        )
        .expect("valid capability")
    }

    #[test]
    fn test_state_empty() {
        let s = State::empty();
        assert_eq!(s.time(), 0);
        assert_eq!(s.plugin_count(), 0);
        assert_eq!(s.actor_count(), 0);
    }

    #[test]
    fn test_state_apply_alloc() {
        let mut s = State::empty();
        let addr = s.apply_alloc_mut(1, 1024);

        assert!(s.ghost.is_live(addr));
    }

    #[test]
    fn test_state_apply_free() {
        let mut s = State::empty();
        let addr = s.apply_alloc_mut(1, 1024);
        s.apply_free_mut(addr).expect("free should succeed");

        assert!(!s.ghost.is_live(addr));
        assert!(s.ghost.is_freed(addr));
    }

    #[test]
    fn test_state_temporal_safety() {
        let mut s1 = State::empty();
        let addr = s1.apply_alloc_mut(1, 1024);
        s1.apply_free_mut(addr).expect("free should succeed");

        // s2 should also have addr freed
        let s2 = s1.clone();
        assert!(s1.temporal_safety(&s2));

        // s3 with more allocations should still preserve temporal safety
        let mut s3 = s1.clone();
        s3.apply_alloc_mut(2, 512);
        assert!(s1.temporal_safety(&s3));
    }

    #[test]
    fn test_state_plugin_holds() {
        let mut s = State::empty();
        let ps = PluginState::empty(SecurityLevel::Public, 0);
        s.insert_plugin(1, ps).unwrap();

        // Plugin doesn't hold any caps initially
        assert!(!s.plugin_holds(1, 42));

        // Grant cap
        if let Some(ps) = s.get_plugin_mut(1) {
            ps.grant_cap_mut(42);
        }
        assert!(s.plugin_holds(1, 42));
    }

    #[test]
    fn test_state_cap_delegate() {
        let mut s = State::empty();
        let ps = PluginState::empty(SecurityLevel::Public, 0);
        s.insert_plugin(1, ps).unwrap();

        let cap = make_test_cap(100);
        s.apply_cap_delegate_mut(cap, 1)
            .expect("delegate should succeed");

        // Plugin should hold the cap
        assert!(s.plugin_holds(1, 100));
        // Cap should be valid in kernel
        assert!(s.cap_is_valid(100));
    }

    #[test]
    fn test_state_cap_revoke() {
        let mut s = State::empty();
        let ps = PluginState::empty(SecurityLevel::Public, 0);
        s.insert_plugin(1, ps).unwrap();

        let cap = make_test_cap(100);
        s.apply_cap_delegate_mut(cap, 1)
            .expect("delegate should succeed");

        assert!(s.cap_is_valid(100));

        s.apply_cap_revoke_mut(100).expect("revoke should succeed");
        assert!(!s.cap_is_valid(100));
    }

    #[test]
    fn test_state_isolation() {
        let mut s1 = State::empty();
        let _ = s1.insert_plugin(1, PluginState::empty(SecurityLevel::Public, 100));
        let _ = s1.insert_plugin(2, PluginState::empty(SecurityLevel::Internal, 200));

        // Modify only plugin 1
        let mut s2 = s1.clone();
        if let Some(ps) = s2.get_plugin_mut(1) {
            ps.grant_cap_mut(42);
        }

        // Isolation should be preserved for plugin 2
        assert!(s1.preserves_isolation(&s2, 1));
    }

    #[test]
    fn test_low_equivalent_refl() {
        // Corresponds to Lean theorem: low_equivalent_refl
        let s = State::empty();
        assert!(low_equivalent(SecurityLevel::Public, &s, &s));
        assert!(low_equivalent(SecurityLevel::Secret, &s, &s));
    }

    #[test]
    fn test_low_equivalent_symm() {
        // Corresponds to Lean theorem: low_equivalent_symm
        let mut s1 = State::empty();
        let _ = s1.insert_plugin(1, PluginState::empty(SecurityLevel::Public, 0));

        let s2 = s1.clone();

        assert!(low_equivalent(SecurityLevel::Public, &s1, &s2));
        assert!(low_equivalent(SecurityLevel::Public, &s2, &s1));
    }

    #[test]
    fn test_low_equivalent_high_changes_invisible() {
        let mut s1 = State::empty();
        let _ = s1.insert_plugin(1, PluginState::empty(SecurityLevel::Public, 0));
        let _ = s1.insert_plugin(2, PluginState::empty(SecurityLevel::Secret, 0));

        let mut s2 = s1.clone();
        // Modify the Secret plugin
        if let Some(ps) = s2.get_plugin_mut(2) {
            ps.grant_cap_mut(42);
        }

        // At Public level, states should still be equivalent
        // (Secret changes are invisible to Public observer)
        assert!(low_equivalent(SecurityLevel::Public, &s1, &s2));

        // At Secret level, states should NOT be equivalent
        assert!(!low_equivalent(SecurityLevel::Secret, &s1, &s2));
    }

    #[test]
    fn test_resource_info() {
        let ri = ResourceInfo::new(SecurityLevel::Confidential);
        assert_eq!(ri.level(), SecurityLevel::Confidential);

        let ri_default = ResourceInfo::default();
        assert_eq!(ri_default.level(), SecurityLevel::Public);
    }

    #[test]
    fn test_state_tick() {
        let mut s = State::empty();
        assert_eq!(s.time(), 0);

        s.tick().expect("tick should succeed");
        assert_eq!(s.time(), 1);

        s.tick().expect("tick should succeed");
        assert_eq!(s.time(), 2);
    }

    #[test]
    fn test_state_time_delegates_to_kernel() {
        // Verify that State.time() reads from kernel.now()
        let s = State::empty();
        assert_eq!(s.time(), s.kernel().now());
    }

    // ============== BINARY SEARCH EDGE-CASE TESTS ==============

    #[test]
    fn test_state_plugin_insert_order() {
        let mut s = State::empty();
        let ps = || PluginState::empty(SecurityLevel::Public, 1024);
        // Insert out of order
        s.insert_plugin(50, ps()).unwrap();
        s.insert_plugin(10, ps()).unwrap();
        s.insert_plugin(90, ps()).unwrap();
        s.insert_plugin(30, ps()).unwrap();

        // Verify sorted
        let ids: Vec<u128> = s.plugins.iter().map(|(id, _)| *id).collect();
        assert_eq!(ids, vec![10, 30, 50, 90]);
    }

    #[test]
    fn test_state_empty_lookup() {
        let s = State::empty();
        assert!(s.get_plugin(0).is_none());
        assert!(s.get_actor(0).is_none());
        assert!(s.get_workflow(0).is_none());
    }

    #[test]
    fn test_state_single_element_lookup() {
        let mut s = State::empty();
        s.insert_plugin(42, PluginState::empty(SecurityLevel::Public, 1024))
            .unwrap();

        assert!(s.get_plugin(42).is_some());
        assert!(s.get_plugin(41).is_none());
        assert!(s.get_plugin(43).is_none());
    }

    #[test]
    fn test_state_plugin_collision() {
        let mut s = State::empty();
        let ps = || PluginState::empty(SecurityLevel::Public, 1024);
        s.insert_plugin(1, ps()).unwrap();
        let result = s.insert_plugin(1, ps());
        assert!(matches!(result, Err(StateError::PluginExists(1))));
    }
}
