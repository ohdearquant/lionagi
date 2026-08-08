// Copyright (C) 2026 HaiyangLi
// SPDX-License-Identifier: Apache-2.0
//! Lion State Kernel
//!
//! Corresponds to: Lion/State/Kernel.lean
//!
//! Kernel state for the trusted computing base, including revocation tracking.
//!
//! NOTE: Uses Vec<(CapId, Capability)> instead of BTreeMap.
//! All operations maintain sorted order by CapId for deterministic behavior.

use crate::types::{CapId, CapPayload, Capability, Key, PolicyState, SealedTag, Time};

// ============================================================================
// KEY STATE (HMAC Key Rotation)
// ============================================================================
//
// Corresponds to Lean: `structure KeyState`
//
// Supports HMAC key rotation with grace period for in-flight capabilities.

/// Key state for HMAC key rotation support.
///
/// Corresponds to Lean: `structure KeyState`
///
/// Design rationale:
/// - `current`: The active key used for sealing new capabilities
/// - `epoch`: Monotonically increasing counter identifying the current key
/// - `previous`: Optional previous key for validating in-flight capabilities
/// - `previous_epoch`: Epoch of the previous key
///
/// The grace period (keeping previous key) allows capabilities that were
/// sealed just before rotation to still be verified.
///
/// SECURITY INVARIANT: epoch is monotonically increasing (never wraps in practice).
#[derive(Debug, Clone)]
pub struct KeyState {
    /// Current active key for sealing
    ///
    /// Corresponds to Lean: `current : Key`
    pub(crate) current: Key,

    /// Current key epoch (monotonically increasing)
    ///
    /// Corresponds to Lean: `epoch : Nat`
    pub(crate) epoch: u64,

    /// Previous key (for grace period verification)
    ///
    /// Corresponds to Lean: `previous : Option Key`
    pub(crate) previous: Option<Key>,

    /// Previous key epoch
    ///
    /// Corresponds to Lean: `previousEpoch : Option Nat`
    pub(crate) previous_epoch: Option<u64>,
}

impl KeyState {
    /// Create initial key state with no previous key
    ///
    /// Corresponds to Lean: `def KeyState.initial`
    pub fn initial(key: Key) -> Self {
        KeyState {
            current: key,
            epoch: 0,
            previous: None,
            previous_epoch: None,
        }
    }

    /// Create empty key state (for default/placeholder purposes)
    ///
    /// Corresponds to Lean: `def KeyState.empty`
    pub fn empty() -> Self {
        KeyState {
            current: Key::empty(),
            epoch: 0,
            previous: None,
            previous_epoch: None,
        }
    }

    /// Rotate to a new key
    ///
    /// Corresponds to Lean: `def KeyState.rotate`
    ///
    /// The current key becomes the previous key for grace period verification.
    /// The epoch is incremented.
    ///
    /// SECURITY: Only kernel can trigger rotation (not plugins).
    /// Uses checked arithmetic -- epoch overflow is a critical error.
    pub fn rotate(&mut self, new_key: Key) {
        self.previous = Some(self.current.clone());
        self.previous_epoch = Some(self.epoch);
        self.current = new_key;
        // In practice, u64 epoch overflow is unreachable (>18 quintillion rotations).
        // We keep checked_add for proof-relevant correctness; panic is acceptable here
        // because reaching u64::MAX key rotations is physically impossible.
        self.epoch = self
            .epoch
            .checked_add(1)
            .expect("key epoch overflow (unreachable in practice)");
    }

    /// Rotate to a new key (immutable version)
    ///
    /// Returns a new KeyState with the rotated key.
    pub fn rotated(&self, new_key: Key) -> Self {
        KeyState {
            current: new_key,
            epoch: self
                .epoch
                .checked_add(1)
                .expect("key epoch overflow (unreachable in practice)"),
            previous: Some(self.current.clone()),
            previous_epoch: Some(self.epoch),
        }
    }

    /// Get the current key
    #[inline]
    pub fn current(&self) -> &Key {
        &self.current
    }

    /// Get the current epoch
    #[inline]
    pub fn epoch(&self) -> u64 {
        self.epoch
    }

    /// Get the previous key (if available)
    #[inline]
    pub fn previous(&self) -> Option<&Key> {
        self.previous.as_ref()
    }

    /// Get the previous epoch (if available)
    #[inline]
    pub fn previous_epoch(&self) -> Option<u64> {
        self.previous_epoch
    }

    /// Check if epoch is current or within grace period
    ///
    /// Corresponds to Lean: `def KeyState.epochValid`
    pub fn epoch_valid(&self, cap_epoch: u64) -> bool {
        // Valid if epoch matches current or is newer
        if cap_epoch >= self.epoch {
            return true;
        }
        // Or matches previous (grace period)
        match self.previous_epoch {
            Some(prev_epoch) => cap_epoch >= prev_epoch,
            None => false,
        }
    }

    /// Verify a capability's seal against the key state
    ///
    /// Corresponds to Lean: `def KeyState.verify`
    ///
    /// Strategy:
    /// 1. Try current key first
    /// 2. Fall back to previous key for in-flight capabilities (grace period)
    ///
    /// Returns true if the capability verifies against either key.
    pub fn verify_seal(&self, payload: &CapPayload, tag: &SealedTag) -> bool {
        // Try current key
        if crate::crypto::verify_seal(&self.current, payload, tag) {
            return true;
        }
        // Fall back to previous key if available
        match &self.previous {
            Some(prev_key) => crate::crypto::verify_seal(prev_key, payload, tag),
            None => false,
        }
    }

    /// Combined verification: seal AND epoch valid
    ///
    /// Corresponds to Lean: `def KeyState.verifyWithEpoch`
    pub fn verify_with_epoch(&self, payload: &CapPayload, tag: &SealedTag, cap_epoch: u64) -> bool {
        self.epoch_valid(cap_epoch) && self.verify_seal(payload, tag)
    }

    /// Clear the previous key (end grace period)
    ///
    /// Call this after sufficient time has passed since rotation
    /// to clear the previous key from memory.
    pub fn clear_previous(&mut self) {
        self.previous = None;
        self.previous_epoch = None;
    }
}

impl Default for KeyState {
    fn default() -> Self {
        KeyState::empty()
    }
}

/// Capability table in kernel
///
/// Corresponds to Lean: `abbrev CapTable := Std.HashMap CapId Capability`
/// INVARIANT: Always sorted by CapId, no duplicate IDs.
pub type CapTable = Vec<(CapId, Capability)>;

/// Error type for kernel operations
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum KernelError {
    /// Capability not found in the table
    CapNotFound(CapId),
    /// Capability ID exhausted (overflow)
    CapIdExhausted,
    /// Capability ID collision during insert
    CapIdCollision(CapId),
    /// Invalid capability state
    InvalidCapability(String),
    /// Counter overflow (time or epoch would exceed u64::MAX)
    CounterOverflow(&'static str),
}

impl std::fmt::Display for KernelError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            KernelError::CapNotFound(id) => write!(f, "Capability {id} not found"),
            KernelError::CapIdExhausted => write!(f, "Capability ID space exhausted"),
            KernelError::CapIdCollision(id) => write!(f, "Capability ID {id} already exists"),
            KernelError::InvalidCapability(msg) => write!(f, "Invalid capability: {msg}"),
            KernelError::CounterOverflow(name) => write!(f, "{name} counter overflow"),
        }
    }
}

impl std::error::Error for KernelError {}

/// Children index for O(log n) revocation: maps parent_id -> list of child cap_ids
///
/// Corresponds to Lean: `abbrev ChildrenIndex := Std.HashMap CapId (List CapId)`
/// INVARIANT: Always sorted by parent CapId, no duplicate parent IDs.
pub type ChildrenIndex = Vec<(CapId, Vec<CapId>)>;

/// Revocation epoch tracking
///
/// Corresponds to Lean: `structure RevocationState`
///
/// INVARIANTS:
/// - caps is sorted by CapId, no duplicate IDs
/// - `caps[i].0 == caps[i].1.id()` for all entries (keys match cap IDs)
/// - Parent IDs are always less than child IDs (ParentLt)
/// - Valid capabilities have valid parents (ValidParentPresent)
/// - children_index is consistent with parent relationships in caps (ChildrenIndexConsistent)
/// - children_index is complete: every cap with a parent is in the index (ChildrenIndexComplete)
#[derive(Debug, Clone)]
pub struct RevocationState {
    /// Capability table (sorted Vec)
    ///
    /// Corresponds to Lean: `caps : CapTable`
    /// INVARIANT: Sorted by CapId, no duplicates
    pub(crate) caps: CapTable,

    /// Global revocation epoch
    ///
    /// Corresponds to Lean: `epoch : Nat`
    pub(crate) epoch: u64,

    /// Reverse index for efficient revocation: parent_id -> child cap_ids
    ///
    /// Corresponds to Lean: `childrenIndex : ChildrenIndex`
    /// INVARIANT: Sorted by parent CapId, children lists may be unsorted
    pub(crate) children_index: ChildrenIndex,
}

impl RevocationState {
    /// Create empty revocation state
    ///
    /// Corresponds to Lean: `def RevocationState.empty : RevocationState`
    pub fn empty() -> Self {
        RevocationState {
            caps: Vec::new(),
            epoch: 0,
            children_index: Vec::new(),
        }
    }

    /// Helper: Find index of capability by ID using binary search
    ///
    /// Production version: uses binary_search_by_key with closure
    fn find_index(&self, cap_id: CapId) -> Option<usize> {
        self.caps
            .binary_search_by_key(&cap_id, |entry| entry.0)
            .ok()
    }

    /// Helper: Find index of capability by ID using binary search
    ///

    /// Helper: Find insertion point for sorted insert using binary search
    ///
    /// Production version: uses binary_search_by_key with closure
    fn find_insert_pos(&self, cap_id: CapId) -> (usize, bool) {
        match self.caps.binary_search_by_key(&cap_id, |entry| entry.0) {
            Ok(pos) => (pos, true),
            Err(pos) => (pos, false),
        }
    }

    /// Helper: Find insertion point for sorted insert using binary search
    ///

    /// Check if capability exists and is valid
    ///
    /// Corresponds to Lean: `def RevocationState.is_valid`
    pub fn is_valid(&self, cap_id: CapId) -> bool {
        self.find_index(cap_id)
            .is_some_and(|idx| self.caps[idx].1.is_valid())
    }

    /// Get a capability by ID
    pub fn get(&self, cap_id: CapId) -> Option<&Capability> {
        self.find_index(cap_id).map(|idx| &self.caps[idx].1)
    }

    /// Get a mutable reference to a capability by ID
    #[allow(dead_code)] // Lean correspondence
    pub(crate) fn get_mut(&mut self, cap_id: CapId) -> Option<&mut Capability> {
        self.find_index(cap_id).map(|idx| &mut self.caps[idx].1)
    }

    /// Check if the table contains a capability
    #[inline]
    pub fn contains(&self, cap_id: CapId) -> bool {
        self.find_index(cap_id).is_some()
    }

    /// Revoke capability by ID (single cap only)
    ///
    /// Corresponds to Lean: `def RevocationState.revoke`
    ///
    /// Returns the new state with the capability revoked.
    /// If the capability doesn't exist, returns the state unchanged.
    pub fn revoke(&self, cap_id: CapId) -> Self {
        let new_caps = self
            .caps
            .iter()
            .map(|(id, cap)| {
                if *id == cap_id {
                    let mut revoked_cap = cap.clone();
                    revoked_cap.revoke();
                    (*id, revoked_cap)
                } else {
                    (*id, cap.clone())
                }
            })
            .collect();
        RevocationState {
            caps: new_caps,
            epoch: self.epoch,
            children_index: self.children_index.clone(),
        }
    }

    /// Revoke capability by ID (mutating version)
    ///
    /// # Errors
    ///
    /// Returns `KernelError::CapNotFound` if the capability ID is not in the table.
    pub fn revoke_mut(&mut self, cap_id: CapId) -> Result<(), KernelError> {
        match self.find_index(cap_id) {
            Some(idx) => {
                self.caps[idx].1.revoke();
                Ok(())
            }
            None => Err(KernelError::CapNotFound(cap_id)),
        }
    }

    /// Check if ancestorId is in the parent chain of capId
    ///
    /// Corresponds to Lean: `def RevocationState.has_ancestor`
    ///
    /// Uses a fuel parameter for termination (ParentLt guarantees termination with fuel = capId).
    pub fn has_ancestor(&self, cap_id: CapId, ancestor_id: CapId) -> bool {
        self.has_ancestor_with_fuel(cap_id, ancestor_id, cap_id)
    }

    /// Internal: has_ancestor with explicit fuel for termination
    fn has_ancestor_with_fuel(&self, cap_id: CapId, ancestor_id: CapId, fuel: u128) -> bool {
        if fuel == 0 {
            return false;
        }

        match self.get(cap_id) {
            None => false,
            Some(cap) => match cap.parent() {
                None => false,
                Some(parent_id) => {
                    if parent_id == ancestor_id {
                        true
                    } else {
                        self.has_ancestor_with_fuel(parent_id, ancestor_id, fuel - 1)
                    }
                }
            },
        }
    }

    /// Check if cap_id is in the revoke set (id == cap_id OR has cap_id as ancestor)
    ///
    /// Corresponds to Lean: `def RevocationState.in_revoke_set`
    pub fn in_revoke_set(&self, k: CapId, cap_id: CapId) -> bool {
        k == cap_id || self.has_ancestor(k, cap_id)
    }

    /// Revoke capability and all descendants transitively
    ///
    /// Corresponds to Lean: `def RevocationState.revoke_transitive`
    ///
    /// Sets valid=false on capId and any cap whose parent chain includes capId.
    pub fn revoke_transitive(&self, cap_id: CapId) -> Self {
        let new_caps = self
            .caps
            .iter()
            .map(|(k, cap)| {
                if self.in_revoke_set(*k, cap_id) {
                    let mut revoked = cap.clone();
                    revoked.revoke();
                    (*k, revoked)
                } else {
                    (*k, cap.clone())
                }
            })
            .collect();
        RevocationState {
            caps: new_caps,
            epoch: self.epoch,
            children_index: self.children_index.clone(),
        }
    }

    /// Revoke capability and all descendants transitively (mutating version)
    pub fn revoke_transitive_mut(&mut self, cap_id: CapId) {
        // Collect indices to revoke (borrow conflict resolved by iterating caps directly)
        let mut to_revoke = Vec::with_capacity(self.caps.len());
        to_revoke.extend(
            self.caps
                .iter()
                .enumerate()
                .filter_map(|(i, (id, _))| self.in_revoke_set(*id, cap_id).then_some(i)),
        );

        // Then revoke them
        for idx in to_revoke {
            self.caps[idx].1.revoke();
        }
    }

    /// Insert a capability (returns error on collision)
    ///
    /// Corresponds to Lean: `def RevocationState.insert`
    ///
    /// SECURITY: This function MUST check for ID collision to maintain
    /// the uniqueness invariant and prevent capability forgery via replacement.
    ///
    /// Also maintains children_index for O(log n) revocation.
    ///
    /// # Errors
    ///
    /// Returns `KernelError::CapIdCollision` if a capability with the same ID already exists.
    pub fn insert(&self, cap: Capability) -> Result<Self, KernelError> {
        let id = cap.id();
        let parent = cap.parent();
        let (pos, exists) = self.find_insert_pos(id);

        if exists {
            return Err(KernelError::CapIdCollision(id));
        }

        // Build new caps with insertion at position
        let mut new_caps = Vec::with_capacity(self.caps.len() + 1);
        new_caps.extend_from_slice(&self.caps[..pos]);
        new_caps.push((id, cap));
        new_caps.extend_from_slice(&self.caps[pos..]);

        // Update children_index if cap has a parent
        let mut new_index = self.children_index.clone();
        if let Some(parent_id) = parent {
            Self::add_child_to_index(&mut new_index, parent_id, id);
        }

        Ok(RevocationState {
            caps: new_caps,
            epoch: self.epoch,
            children_index: new_index,
        })
    }

    /// Insert a capability (mutating version)
    ///
    /// Also maintains children_index for O(log n) revocation.
    ///
    /// # Errors
    ///
    /// Returns `KernelError::CapIdCollision` if a capability with the same ID already exists.
    pub fn insert_mut(&mut self, cap: Capability) -> Result<(), KernelError> {
        let id = cap.id();
        let parent = cap.parent();
        let (pos, exists) = self.find_insert_pos(id);

        if exists {
            return Err(KernelError::CapIdCollision(id));
        }

        self.caps.insert(pos, (id, cap));

        // Update children_index if cap has a parent
        if let Some(parent_id) = parent {
            Self::add_child_to_index(&mut self.children_index, parent_id, id);
        }

        Ok(())
    }

    /// Collect all descendants of cap_id using children_index (BFS traversal)
    ///
    /// Corresponds to Lean: `def RevocationState.collectDescendants`
    ///
    /// Returns the set of all cap IDs that should be revoked (cap_id + all descendants).
    ///
    /// SECURITY: No fuel/iteration limit. The graph is finite and `visited`
    /// (BTreeSet) guarantees termination by preventing re-visiting nodes.
    /// Removing the previous 10,000-step hard cap prevents silent truncation
    /// of the revocation set, which was a security bug: a delegation tree
    /// with >10,000 reachable nodes would leave descendants valid after revocation.
    fn collect_descendants(&self, cap_id: CapId) -> Vec<CapId> {
        let mut queue: Vec<CapId> = Vec::with_capacity(self.caps.len().saturating_add(1));
        let mut head: usize = 0;
        let mut visited = std::collections::BTreeSet::new();
        let mut collected: Vec<CapId> = Vec::new();

        queue.push(cap_id);

        while head < queue.len() {
            let id = queue[head];
            head += 1;

            if !visited.insert(id) {
                continue;
            }

            collected.push(id);
            queue.extend(self.get_children(id).iter().copied());
        }

        collected
    }

    /// Get children of a capability from the children_index
    ///
    /// Production version: uses binary_search_by_key with closure
    fn get_children(&self, parent_id: CapId) -> &[CapId] {
        match self
            .children_index
            .binary_search_by_key(&parent_id, |entry| entry.0)
        {
            Ok(idx) => &self.children_index[idx].1,
            Err(_) => &[],
        }
    }

    /// Get children of a capability from the children_index
    ///

    /// Add a child to the children_index (maintains sorted order)
    ///
    /// Production version: uses binary_search and Vec::insert for efficiency
    fn add_child_to_index(index: &mut ChildrenIndex, parent_id: CapId, child_id: CapId) {
        match index.binary_search_by_key(&parent_id, |entry| entry.0) {
            Ok(idx) => {
                index[idx].1.push(child_id);
            }
            Err(pos) => {
                index.insert(pos, (parent_id, vec![child_id]));
            }
        }
    }

    /// Add a child to the children_index (maintains sorted order)
    ///
    /// Rebuilds the index using only Vec::new/push/len and array indexing.
    /// Semantically identical to the production version above.

    /// Revoke capability and all descendants transitively (O(k) optimized)
    ///
    /// Corresponds to Lean: `def RevocationState.revoke_transitive_fast`
    ///
    /// Uses children_index for O(k) traversal where k = number of descendants.
    pub fn revoke_transitive_fast(&self, cap_id: CapId) -> Self {
        let mut to_revoke = self.collect_descendants(cap_id);
        to_revoke.sort_unstable();

        let new_caps = self
            .caps
            .iter()
            .map(|(k, cap)| {
                if to_revoke.binary_search(k).is_ok() {
                    let mut revoked = cap.clone();
                    revoked.revoke();
                    (*k, revoked)
                } else {
                    (*k, cap.clone())
                }
            })
            .collect();

        RevocationState {
            caps: new_caps,
            epoch: self.epoch,
            children_index: self.children_index.clone(),
        }
    }

    /// Revoke capability and all descendants transitively (O(k) optimized, mutating)
    ///
    /// # Errors
    ///
    /// Returns `KernelError::CapNotFound` if the capability does not exist.
    pub fn revoke_transitive_fast_mut(&mut self, cap_id: CapId) -> Result<(), KernelError> {
        if self.find_index(cap_id).is_none() {
            return Err(KernelError::CapNotFound(cap_id));
        }

        let mut to_revoke = self.collect_descendants(cap_id);
        to_revoke.sort_unstable();

        for id in to_revoke {
            if let Some(idx) = self.find_index(id) {
                self.caps[idx].1.revoke();
            }
        }

        Ok(())
    }

    /// Get the current revocation epoch
    #[inline]
    pub fn epoch(&self) -> u64 {
        self.epoch
    }

    /// Get the number of capabilities
    #[inline]
    pub fn cap_count(&self) -> usize {
        self.caps.len()
    }

    /// Get all valid capabilities
    pub fn valid_caps(&self) -> Vec<(CapId, Capability)> {
        // Pre-allocate for worst case (all valid); shrink is free for Vec
        let mut result = Vec::with_capacity(self.caps.len());
        let mut i = 0;
        while i < self.caps.len() {
            if self.caps[i].1.is_valid() {
                result.push((self.caps[i].0, self.caps[i].1.clone()));
            }
            i += 1;
        }
        result
    }

    /// Get all capability IDs
    pub fn cap_ids(&self) -> Vec<CapId> {
        let mut ids = Vec::with_capacity(self.caps.len());
        let mut i = 0;
        while i < self.caps.len() {
            ids.push(self.caps[i].0);
            i += 1;
        }
        ids
    }

    /// Iterate over all capabilities
    ///
    /// Returns a slice iterator over (CapId, Capability) pairs.
    /// This provides compatibility with code that uses .iter() on the caps.
    #[inline]
    pub fn iter(&self) -> std::slice::Iter<'_, (CapId, Capability)> {
        self.caps.iter()
    }

    /// Check if any valid capability targets the given resource
    ///
    /// SECURITY: This check is CRITICAL for apply_free.
    /// If any valid capability targets an address, freeing that address
    /// would create a USE-AFTER-FREE vulnerability (dangling capability).
    ///
    /// Corresponds to Lean: Memory safety guard in `apply_free`
    pub fn any_valid_targeting(&self, target: crate::types::ResourceId) -> bool {
        self.caps
            .iter()
            .any(|(_, cap)| cap.is_valid() && cap.target() == target)
    }
}

impl Default for RevocationState {
    fn default() -> Self {
        RevocationState::empty()
    }
}

/// Complete kernel state (TCB)
///
/// Corresponds to Lean: `structure KernelState`
#[derive(Debug, Clone)]
pub struct KernelState {
    /// Key state for HMAC sealing with rotation support
    ///
    /// Corresponds to Lean: `keyState : KeyState`
    pub(crate) key_state: KeyState,

    /// Policy state for access control
    ///
    /// Corresponds to Lean: `policy : PolicyState`
    pub(crate) policy: PolicyState,

    /// Revocation tracking
    ///
    /// Corresponds to Lean: `revocation : RevocationState`
    pub(crate) revocation: RevocationState,

    /// Current logical time
    ///
    /// Corresponds to Lean: `now : Time`
    pub(crate) now: Time,

    /// Next capability ID for deterministic allocation
    ///
    /// Corresponds to Lean: `nextCapId : CapId`
    ///
    /// INVARIANT (FreshIdInvariant):
    /// - All existing cap IDs are < next_cap_id
    /// - Allocating a new cap increments this counter
    /// - This enables constructive proof that fresh IDs exist
    pub(crate) next_cap_id: CapId,
}

impl KernelState {
    /// Create initial kernel state
    ///
    /// Corresponds to Lean: `def KernelState.initial (key : Key) (pol : PolicyState) : KernelState`
    pub fn initial(key: Key, policy: PolicyState) -> Self {
        KernelState {
            key_state: KeyState::initial(key),
            policy,
            revocation: RevocationState::empty(),
            now: 0,
            next_cap_id: 0,
        }
    }

    /// Advance kernel time (immutable, checked)
    ///
    /// Corresponds to Lean: `def KernelState.tick (ks : KernelState) : KernelState`
    ///
    /// # Errors
    ///
    /// Returns `KernelError::CounterOverflow` if the time counter would exceed u64::MAX.
    pub fn tick(&self) -> Result<Self, KernelError> {
        let new_now = self
            .now
            .checked_add(1)
            .ok_or(KernelError::CounterOverflow("time"))?;
        Ok(KernelState {
            key_state: self.key_state.clone(),
            policy: self.policy.clone(),
            revocation: self.revocation.clone(),
            now: new_now,
            next_cap_id: self.next_cap_id,
        })
    }

    /// Rotate HMAC key (kernel-only operation)
    ///
    /// Corresponds to Lean: `def KernelState.rotateKey`
    ///
    /// The current key becomes the previous key for grace period verification.
    /// The epoch is incremented.
    ///
    /// SECURITY: This operation should only be callable by privileged kernel code.
    pub fn rotate_key(&mut self, new_key: Key) {
        self.key_state.rotate(new_key);
    }

    /// Rotate HMAC key (immutable version)
    pub fn with_rotated_key(&self, new_key: Key) -> Self {
        KernelState {
            key_state: self.key_state.rotated(new_key),
            policy: self.policy.clone(),
            revocation: self.revocation.clone(),
            now: self.now,
            next_cap_id: self.next_cap_id,
        }
    }

    /// Get the current key epoch
    #[inline]
    pub fn key_epoch(&self) -> u64 {
        self.key_state.epoch()
    }

    /// Get a reference to the key state
    #[inline]
    pub fn key_state(&self) -> &KeyState {
        &self.key_state
    }

    /// Get a mutable reference to the key state (kernel-internal only)
    #[inline]
    #[allow(dead_code)] // Lean correspondence
    pub(crate) fn key_state_mut(&mut self) -> &mut KeyState {
        &mut self.key_state
    }

    /// Verify capability seal using key state (supports rotation)
    ///
    /// Corresponds to Lean: `def KernelState.verify_cap_seal`
    pub fn verify_cap_seal(&self, cap: &Capability) -> bool {
        self.key_state
            .verify_with_epoch(&cap.payload(), cap.signature(), cap.epoch())
    }

    /// Allocate a fresh capability ID
    ///
    /// Corresponds to Lean: `def KernelState.alloc_cap_id`
    ///
    /// INVARIANT: The returned ID is guaranteed fresh (not in use).
    ///
    /// # Errors
    ///
    /// Returns `KernelError::CapIdExhausted` if the capability ID counter would overflow.
    pub fn alloc_cap_id(&mut self) -> Result<CapId, KernelError> {
        let id = self.next_cap_id;
        self.next_cap_id = match self.next_cap_id.checked_add(1) {
            Some(v) => v,
            None => return Err(KernelError::CapIdExhausted),
        };
        Ok(id)
    }

    /// Get the next capability ID (without allocating)
    #[inline]
    pub fn next_cap_id(&self) -> CapId {
        self.next_cap_id
    }

    /// Advance kernel time (mutating, checked)
    ///
    /// # Errors
    ///
    /// Returns `KernelError::CounterOverflow` if the time counter would exceed u64::MAX.
    pub fn tick_checked(&mut self) -> Result<(), KernelError> {
        self.now = self
            .now
            .checked_add(1)
            .ok_or(KernelError::CounterOverflow("time"))?;
        Ok(())
    }

    /// Check if capability is not revoked
    ///
    /// Corresponds to Lean: `def KernelState.cap_not_revoked`
    pub fn cap_not_revoked(&self, cap: &Capability) -> bool {
        self.revocation.is_valid(cap.id())
    }

    /// Get the current time
    #[inline]
    pub fn now(&self) -> Time {
        self.now
    }

    /// Get a reference to the revocation state
    #[inline]
    pub fn revocation(&self) -> &RevocationState {
        &self.revocation
    }

    /// Get a mutable reference to the revocation state
    #[inline]
    pub(crate) fn revocation_mut(&mut self) -> &mut RevocationState {
        &mut self.revocation
    }

    /// Get a reference to the policy state
    #[inline]
    pub fn policy(&self) -> &PolicyState {
        &self.policy
    }

    /// Get a mutable reference to the policy state
    #[inline]
    #[allow(dead_code)] // Lean correspondence
    pub(crate) fn policy_mut(&mut self) -> &mut PolicyState {
        &mut self.policy
    }

    /// Get a reference to the current HMAC key (kernel-internal only)
    ///
    /// Backward compatibility: returns the current key from key_state
    #[inline]
    #[allow(dead_code)] // Lean correspondence
    pub(crate) fn hmac_key(&self) -> &Key {
        self.key_state.current()
    }
}

impl Default for KernelState {
    /// Default kernel state: empty key, deny-all policy, no capabilities, time=0, next_cap_id=0
    fn default() -> Self {
        KernelState {
            key_state: KeyState::empty(),
            policy: PolicyState::default(),
            revocation: RevocationState::empty(),
            now: 0,
            next_cap_id: 0,
        }
    }
}

// ============================================================================
// KERNEL STATE CORE
// ============================================================================
//
// These types form the security-critical kernel state that can be machine-verified.
// PolicyState remains outside KernelStateCore so policy evaluation stays at the
// shell boundary, but it is now enum-backed and extractable separately.
//
// Corresponds to Lean: `Lion/State/KernelStateCore.lean`

use crate::types::{PluginId, ResourceId, SecurityLevel};

/// Plugin security metadata (extractable)
///
/// Corresponds to Lean: `structure PluginMeta`
///
/// INVARIANT: held_caps is sorted, no duplicates
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PluginMeta {
    /// Security level of this plugin
    pub level: SecurityLevel,
    /// Capabilities held by this plugin (sorted Vec)
    pub held_caps: Vec<CapId>,
}

impl PluginMeta {
    /// Create empty plugin metadata (Public level, no capabilities)
    ///
    /// Corresponds to Lean: `def PluginMeta.empty`
    pub fn empty() -> Self {
        PluginMeta {
            level: SecurityLevel::Public,
            held_caps: Vec::new(),
        }
    }

    /// Grant a capability to this plugin (maintains sorted order)
    ///
    /// Corresponds to Lean: `def PluginMeta.grant_cap`
    /// Production version: uses binary_search for efficiency
    pub fn grant_cap(&mut self, cap_id: CapId) {
        match self.held_caps.binary_search(&cap_id) {
            Ok(_) => {} // Already present
            Err(pos) => self.held_caps.insert(pos, cap_id),
        }
    }

    /// Corresponds to Lean: `def PluginMeta.grant_cap`
    /// Uses while-index loops only.

    /// Revoke a capability from this plugin
    ///
    /// Corresponds to Lean: `def PluginMeta.revoke_cap`
    pub fn revoke_cap(&mut self, cap_id: CapId) {
        if let Ok(pos) = self.held_caps.binary_search(&cap_id) {
            self.held_caps.remove(pos);
        }
    }

    /// Check if plugin holds a capability
    ///
    /// Corresponds to Lean: `def PluginMeta.holds_cap`
    pub fn holds_cap(&self, cap_id: CapId) -> bool {
        self.held_caps.binary_search(&cap_id).is_ok()
    }
}

impl Default for PluginMeta {
    fn default() -> Self {
        PluginMeta::empty()
    }
}

/// Resource security metadata (extractable)
///
/// Corresponds to Lean: `structure ResourceMeta`
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ResourceMeta {
    /// Security level of this resource
    pub level: SecurityLevel,
}

impl ResourceMeta {
    /// Create empty resource metadata (Public level)
    ///
    /// Corresponds to Lean: `def ResourceMeta.empty`
    pub fn empty() -> Self {
        ResourceMeta {
            level: SecurityLevel::Public,
        }
    }
}

impl Default for ResourceMeta {
    fn default() -> Self {
        ResourceMeta::empty()
    }
}

/// Extractable kernel state core
///
/// Contains only security-critical state that can be machine-verified.
/// PolicyState remains outside this core; policy evaluation happens in the shell.
///
/// Corresponds to Lean: `structure KernelStateCore`
///
/// INVARIANTS:
/// - plugin_metas is sorted by PluginId, unique keys
/// - resource_metas is sorted by ResourceId, unique keys
#[derive(Debug, Clone)]
pub struct KernelStateCore {
    /// Key state for capability sealing (with rotation support)
    pub(crate) key_state: KeyState,
    /// Capability table with revocation tracking (also owns the revocation epoch)
    pub(crate) revocation: RevocationState,
    /// Plugin security metadata (sorted Vec)
    pub(crate) plugin_metas: Vec<(PluginId, PluginMeta)>,
    /// Resource security metadata (sorted Vec)
    pub(crate) resource_metas: Vec<(ResourceId, ResourceMeta)>,
    /// Current logical time
    pub(crate) now: Time,
    /// Next capability ID for allocation
    #[allow(dead_code)] // Lean correspondence
    pub(crate) next_cap_id: CapId,
}

impl KernelStateCore {
    /// Create initial kernel state core
    ///
    /// Corresponds to Lean: `Inhabited KernelStateCore`
    pub fn empty(hmac_key: Key) -> Self {
        KernelStateCore {
            key_state: KeyState::initial(hmac_key),
            revocation: RevocationState::empty(),
            plugin_metas: Vec::new(),
            resource_metas: Vec::new(),
            now: 0,
            next_cap_id: 0,
        }
    }

    /// Get a reference to the current HMAC key
    ///
    /// Backward compatibility accessor
    #[inline]
    pub fn hmac_key(&self) -> &Key {
        self.key_state.current()
    }

    /// Get a reference to the key state
    #[inline]
    pub fn key_state(&self) -> &KeyState {
        &self.key_state
    }

    /// Rotate HMAC key (kernel-only operation)
    pub fn rotate_key(&mut self, new_key: Key) {
        self.key_state.rotate(new_key);
    }

    /// Verify capability seal using key state (supports rotation)
    pub fn verify_cap_seal(&self, cap: &Capability) -> bool {
        self.key_state
            .verify_with_epoch(&cap.payload(), cap.signature(), cap.epoch())
    }

    /// Get plugin metadata (returns default if not found)
    ///
    /// Production version: uses binary_search_by_key with closure
    pub fn get_plugin_meta(&self, pid: PluginId) -> PluginMeta {
        match self
            .plugin_metas
            .binary_search_by_key(&pid, |entry| entry.0)
        {
            Ok(idx) => self.plugin_metas[idx].1.clone(),
            Err(_) => PluginMeta::empty(),
        }
    }

    /// Get plugin metadata (returns default if not found)
    ///

    /// Set plugin metadata (upsert)
    ///
    /// Production version: uses binary_search for O(log n) lookup.
    /// Normalizes held_caps (sort + dedup) to maintain sorted invariant.
    pub fn set_plugin_meta(&mut self, pid: PluginId, mut meta: PluginMeta) {
        meta.held_caps.sort_unstable();
        meta.held_caps.dedup();
        match self
            .plugin_metas
            .binary_search_by_key(&pid, |entry| entry.0)
        {
            Ok(idx) => {
                self.plugin_metas[idx].1 = meta;
            }
            Err(pos) => {
                self.plugin_metas.insert(pos, (pid, meta));
            }
        }
    }

    /// rebuilds the vec manually. Normalizes held_caps (sort + dedup).

    /// Get resource metadata (returns default if not found)
    ///
    /// Production version: uses binary_search_by_key with closure
    pub fn get_resource_meta(&self, rid: ResourceId) -> ResourceMeta {
        match self
            .resource_metas
            .binary_search_by_key(&rid, |entry| entry.0)
        {
            Ok(idx) => self.resource_metas[idx].1.clone(),
            Err(_) => ResourceMeta::empty(),
        }
    }

    /// Get resource metadata (returns default if not found)
    ///

    /// Set resource metadata (upsert)
    ///
    /// Production version: uses binary_search for O(log n) lookup
    pub fn set_resource_meta(&mut self, rid: ResourceId, meta: ResourceMeta) {
        match self
            .resource_metas
            .binary_search_by_key(&rid, |entry| entry.0)
        {
            Ok(idx) => {
                self.resource_metas[idx].1 = meta;
            }
            Err(pos) => {
                self.resource_metas.insert(pos, (rid, meta));
            }
        }
    }

    /// rebuilds the vec manually

    /// Get plugin security level
    ///
    /// Corresponds to Lean: `def KernelStateCore.plugin_level`
    pub fn plugin_level(&self, pid: PluginId) -> SecurityLevel {
        self.get_plugin_meta(pid).level
    }

    /// Get resource security level
    ///
    /// Corresponds to Lean: `def KernelStateCore.resource_level`
    pub fn resource_level(&self, rid: ResourceId) -> SecurityLevel {
        self.get_resource_meta(rid).level
    }

    /// Check if plugin holds capability
    ///
    /// Corresponds to Lean: `def KernelStateCore.plugin_holds`
    pub fn plugin_holds(&self, pid: PluginId, cap_id: CapId) -> bool {
        self.plugin_metas
            .binary_search_by_key(&pid, |e| e.0)
            .ok()
            .is_some_and(|i| self.plugin_metas[i].1.holds_cap(cap_id))
    }

    /// Check if capability is valid
    ///
    /// Corresponds to Lean: `def KernelStateCore.cap_is_valid`
    pub fn cap_is_valid(&self, cap_id: CapId) -> bool {
        self.revocation.is_valid(cap_id)
    }

    /// Get current time
    pub fn now(&self) -> Time {
        self.now
    }

    /// Get current revocation epoch (delegates to revocation state -- single source of truth)
    pub fn epoch(&self) -> u64 {
        self.revocation.epoch()
    }
}

impl Default for KernelStateCore {
    fn default() -> Self {
        KernelStateCore::empty(Key::empty())
    }
}

/// Kernel dispatch request (extractable)
///
/// All security-critical mutations go through this enum.
/// Policy evaluation happens in the shell BEFORE dispatch.
///
/// Corresponds to Lean: `inductive KernelRequest`
#[derive(Debug, Clone)]
pub enum KernelRequest {
    /// Delegate a capability to a plugin
    /// Pre-condition: Policy already approved by shell
    CapDelegate {
        /// The new capability to delegate
        new_cap: Capability,
        /// The plugin receiving the capability
        target: PluginId,
    },
    /// Revoke a capability (and descendants)
    CapRevoke {
        /// The capability ID to revoke
        cap_id: CapId,
    },
    /// Update plugin security label
    SetPluginLevel {
        /// The plugin whose level is being changed
        plugin_id: PluginId,
        /// The new security level to assign
        level: SecurityLevel,
    },
    /// Update resource security label
    SetResourceLevel {
        /// The resource whose level is being changed
        resource_id: ResourceId,
        /// The new security level to assign
        level: SecurityLevel,
    },
    /// Advance time
    Tick,
    /// Rotate HMAC key (kernel-only, privileged operation)
    ///
    /// SECURITY: Only privileged kernel code should invoke this.
    /// The current key becomes the previous key for grace period verification.
    RotateKey {
        /// The new key to use for sealing capabilities
        new_key: Key,
    },
}

/// Kernel dispatch result
///
/// Corresponds to Lean: `def KernelResult := KernelStateCore ⊕ KernelError`
pub type KernelResult = Result<KernelStateCore, KernelError>;

/// Kernel dispatch function
///
/// This is the ONLY entry point for modifying KernelStateCore.
/// Policy evaluation happens in shell - this function assumes
/// authorization has been verified.
///
/// Corresponds to Lean: `def kernelDispatch`
///
/// # Errors
///
/// Returns `KernelError::CapIdCollision` if a `CapDelegate` request has an ID that already exists.
pub fn kernel_dispatch(core: KernelStateCore, req: KernelRequest) -> KernelResult {
    match req {
        KernelRequest::CapDelegate { new_cap, target } => {
            dispatch_cap_delegate(core, new_cap, target)
        }
        KernelRequest::CapRevoke { cap_id } => dispatch_cap_revoke(core, cap_id),
        KernelRequest::SetPluginLevel { plugin_id, level } => {
            dispatch_set_plugin_level(core, plugin_id, level)
        }
        KernelRequest::SetResourceLevel { resource_id, level } => {
            dispatch_set_resource_level(core, resource_id, level)
        }
        KernelRequest::Tick => dispatch_tick(core),
        KernelRequest::RotateKey { new_key } => Ok(dispatch_rotate_key(core, new_key)),
    }
}

/// Dispatch: delegate capability
///
/// Corresponds to Lean: `def KernelDispatch.dispatchCapDelegate`
fn dispatch_cap_delegate(
    mut core: KernelStateCore,
    new_cap: Capability,
    target: PluginId,
) -> KernelResult {
    // Check for ID collision
    if core.revocation.contains(new_cap.id()) {
        return Err(KernelError::CapIdCollision(new_cap.id()));
    }

    // Insert capability
    let cap_id = new_cap.id();
    core.revocation.insert_mut(new_cap)?;

    // Grant to target plugin
    let mut target_meta = core.get_plugin_meta(target);
    target_meta.grant_cap(cap_id);
    core.set_plugin_meta(target, target_meta);

    Ok(core)
}

/// Dispatch: revoke capability
///
/// Corresponds to Lean: `def KernelDispatch.dispatchCapRevoke`
#[allow(clippy::unnecessary_wraps)] // Must return KernelResult for dispatch table uniformity
fn dispatch_cap_revoke(mut core: KernelStateCore, cap_id: CapId) -> KernelResult {
    core.revocation.revoke_transitive_fast_mut(cap_id)?;
    Ok(core)
}

/// Dispatch: set plugin security level
///
/// Corresponds to Lean: `def KernelDispatch.dispatchSetPluginLevel`
#[allow(clippy::unnecessary_wraps)] // Must return KernelResult for dispatch table uniformity
fn dispatch_set_plugin_level(
    mut core: KernelStateCore,
    plugin_id: PluginId,
    level: SecurityLevel,
) -> KernelResult {
    let mut meta = core.get_plugin_meta(plugin_id);
    meta.level = level;
    core.set_plugin_meta(plugin_id, meta);
    Ok(core)
}

/// Dispatch: set resource security level
///
/// Corresponds to Lean: `def KernelDispatch.dispatchSetResourceLevel`
#[allow(clippy::unnecessary_wraps)] // Must return KernelResult for dispatch table uniformity
fn dispatch_set_resource_level(
    mut core: KernelStateCore,
    resource_id: ResourceId,
    level: SecurityLevel,
) -> KernelResult {
    let mut meta = core.get_resource_meta(resource_id);
    meta.level = level;
    core.set_resource_meta(resource_id, meta);
    Ok(core)
}

/// Dispatch: tick (advance time)
///
/// Corresponds to Lean: `def KernelDispatch.dispatchTick`
///
/// # Errors
///
/// Returns `KernelError::CounterOverflow` if the time counter would exceed u64::MAX.
fn dispatch_tick(mut core: KernelStateCore) -> Result<KernelStateCore, KernelError> {
    core.now = core
        .now
        .checked_add(1)
        .ok_or(KernelError::CounterOverflow("time"))?;
    Ok(core)
}

/// Dispatch: rotate HMAC key
///
/// Corresponds to Lean: `def KernelDispatch.dispatchRotateKey`
///
/// The current key becomes the previous key for grace period verification.
/// Capabilities sealed with the old key remain valid during the grace period.
///
/// SECURITY: This is a privileged operation. Only kernel-level code
/// should be able to invoke key rotation.
fn dispatch_rotate_key(mut core: KernelStateCore, new_key: Key) -> KernelStateCore {
    core.key_state.rotate(new_key);
    core
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::types::{Right, Rights, SealedTag};

    fn make_test_cap(id: CapId, parent: Option<CapId>) -> Capability {
        Capability::new(
            id,
            1, // holder
            1, // target
            Rights::singleton(Right::Read),
            parent,
            0, // epoch
            SealedTag::empty(),
        )
        .expect("valid capability")
    }

    #[test]
    fn test_revocation_state_empty() {
        let rs = RevocationState::empty();
        assert_eq!(rs.cap_count(), 0);
        assert_eq!(rs.epoch(), 0);
        assert!(!rs.is_valid(0));
    }

    #[test]
    fn test_revocation_state_insert() {
        let mut rs = RevocationState::empty();
        let cap = make_test_cap(1, None);
        rs.insert_mut(cap).expect("insert should succeed");

        assert_eq!(rs.cap_count(), 1);
        assert!(rs.contains(1));
        assert!(rs.is_valid(1));
    }

    #[test]
    fn test_revocation_state_insert_collision() {
        let mut rs = RevocationState::empty();
        let cap1 = make_test_cap(1, None);
        rs.insert_mut(cap1).expect("first insert should succeed");

        let cap2 = make_test_cap(1, None);
        let result = rs.insert_mut(cap2);
        assert!(matches!(result, Err(KernelError::CapIdCollision(1))));
    }

    #[test]
    fn test_revocation_state_insert_sorted() {
        let mut rs = RevocationState::empty();
        // Insert out of order
        rs.insert_mut(make_test_cap(5, None)).expect("insert 5");
        rs.insert_mut(make_test_cap(2, None)).expect("insert 2");
        rs.insert_mut(make_test_cap(8, None)).expect("insert 8");
        rs.insert_mut(make_test_cap(1, None)).expect("insert 1");

        // Verify sorted order
        let ids = rs.cap_ids();
        assert_eq!(ids, vec![1, 2, 5, 8]);
    }

    #[test]
    fn test_revocation_state_revoke() {
        let mut rs = RevocationState::empty();
        let cap = make_test_cap(1, None);
        rs.insert_mut(cap).expect("insert should succeed");

        assert!(rs.is_valid(1));
        rs.revoke_mut(1).expect("revoke should succeed");
        assert!(!rs.is_valid(1));
    }

    #[test]
    fn test_revocation_state_has_ancestor() {
        let mut rs = RevocationState::empty();

        // Create parent-child chain: 1 <- 2 <- 3
        let cap1 = make_test_cap(1, None);
        let cap2 = make_test_cap(2, Some(1));
        let cap3 = make_test_cap(3, Some(2));

        rs.insert_mut(cap1).expect("insert 1");
        rs.insert_mut(cap2).expect("insert 2");
        rs.insert_mut(cap3).expect("insert 3");

        // Test ancestry
        assert!(rs.has_ancestor(2, 1)); // 2's parent is 1
        assert!(rs.has_ancestor(3, 2)); // 3's parent is 2
        assert!(rs.has_ancestor(3, 1)); // 1 is ancestor of 3

        // Negative cases
        assert!(!rs.has_ancestor(1, 2)); // 1 has no parent
        assert!(!rs.has_ancestor(1, 3)); // 1 has no parent
        assert!(!rs.has_ancestor(2, 3)); // 3 is not ancestor of 2
    }

    #[test]
    fn test_revocation_state_revoke_transitive() {
        let mut rs = RevocationState::empty();

        // Create parent-child chain: 1 <- 2 <- 3
        let cap1 = make_test_cap(1, None);
        let cap2 = make_test_cap(2, Some(1));
        let cap3 = make_test_cap(3, Some(2));

        rs.insert_mut(cap1).expect("insert 1");
        rs.insert_mut(cap2).expect("insert 2");
        rs.insert_mut(cap3).expect("insert 3");

        // All should be valid initially
        assert!(rs.is_valid(1));
        assert!(rs.is_valid(2));
        assert!(rs.is_valid(3));

        // Revoke 1 transitively - should revoke 1, 2, and 3
        rs.revoke_transitive_mut(1);

        assert!(!rs.is_valid(1));
        assert!(!rs.is_valid(2));
        assert!(!rs.is_valid(3));
    }

    #[test]
    fn test_revocation_state_revoke_transitive_partial() {
        let mut rs = RevocationState::empty();

        // Create parent-child chain: 1 <- 2 <- 3
        let cap1 = make_test_cap(1, None);
        let cap2 = make_test_cap(2, Some(1));
        let cap3 = make_test_cap(3, Some(2));

        rs.insert_mut(cap1).expect("insert 1");
        rs.insert_mut(cap2).expect("insert 2");
        rs.insert_mut(cap3).expect("insert 3");

        // Revoke 2 transitively - should revoke 2 and 3, but not 1
        rs.revoke_transitive_mut(2);

        assert!(rs.is_valid(1)); // Parent still valid
        assert!(!rs.is_valid(2)); // Revoked
        assert!(!rs.is_valid(3)); // Child of revoked
    }

    #[test]
    fn test_kernel_state_initial() {
        let key = Key::empty();
        let policy = PolicyState::deny_all();
        let ks = KernelState::initial(key, policy);

        assert_eq!(ks.now(), 0);
        assert_eq!(ks.revocation().cap_count(), 0);
    }

    #[test]
    fn test_kernel_state_tick() {
        let mut ks = KernelState::default();
        assert_eq!(ks.now(), 0);

        ks.tick_checked().expect("tick should succeed");
        assert_eq!(ks.now(), 1);

        ks.tick_checked().expect("tick should succeed");
        assert_eq!(ks.now(), 2);
    }

    #[test]
    fn test_kernel_state_cap_not_revoked() {
        let mut ks = KernelState::default();
        let cap = make_test_cap(1, None);

        // Capability not in kernel -> not valid
        assert!(!ks.cap_not_revoked(&cap));

        // Insert and check
        ks.revocation_mut()
            .insert_mut(cap.clone())
            .expect("insert should succeed");
        assert!(ks.cap_not_revoked(&cap));

        // Revoke and check
        ks.revocation_mut()
            .revoke_mut(1)
            .expect("revoke should succeed");
        assert!(!ks.cap_not_revoked(&cap));
    }

    // ============== KEY STATE TESTS ==============

    #[test]
    fn test_key_state_initial() {
        let key = Key::from_bytes([1u8; 32]);
        let ks = KeyState::initial(key.clone());

        assert_eq!(ks.epoch(), 0);
        assert!(ks.previous().is_none());
        assert!(ks.previous_epoch().is_none());
    }

    #[test]
    fn test_key_state_rotation() {
        let key1 = Key::from_bytes([1u8; 32]);
        let key2 = Key::from_bytes([2u8; 32]);
        let mut ks = KeyState::initial(key1.clone());

        // Rotate to key2
        ks.rotate(key2.clone());

        assert_eq!(ks.epoch(), 1);
        assert!(ks.previous().is_some());
        assert_eq!(ks.previous_epoch(), Some(0));
        // Current key should be key2
        assert_eq!(ks.current().as_bytes(), key2.as_bytes());
        // Previous key should be key1
        assert_eq!(ks.previous().unwrap().as_bytes(), key1.as_bytes());
    }

    #[test]
    fn test_key_state_epoch_valid() {
        let key1 = Key::from_bytes([1u8; 32]);
        let key2 = Key::from_bytes([2u8; 32]);
        let mut ks = KeyState::initial(key1);

        // Initial state: epoch 0
        assert!(ks.epoch_valid(0)); // Current epoch valid
        assert!(ks.epoch_valid(1)); // Future epoch valid

        // After rotation: epoch 1, previous_epoch 0
        ks.rotate(key2);
        assert!(ks.epoch_valid(1)); // Current epoch valid
        assert!(ks.epoch_valid(0)); // Previous epoch (grace period) valid
        assert!(ks.epoch_valid(2)); // Future epoch valid
    }

    #[test]
    fn test_key_state_multiple_rotations() {
        let key1 = Key::from_bytes([1u8; 32]);
        let key2 = Key::from_bytes([2u8; 32]);
        let key3 = Key::from_bytes([3u8; 32]);
        let mut ks = KeyState::initial(key1.clone());

        // First rotation
        ks.rotate(key2.clone());
        assert_eq!(ks.epoch(), 1);
        assert_eq!(ks.previous_epoch(), Some(0));

        // Second rotation
        ks.rotate(key3.clone());
        assert_eq!(ks.epoch(), 2);
        assert_eq!(ks.previous_epoch(), Some(1)); // Previous epoch is now 1, not 0
                                                  // key2 is now the previous key (key1 is lost)
        assert_eq!(ks.previous().unwrap().as_bytes(), key2.as_bytes());
    }

    #[test]
    fn test_key_state_clear_previous() {
        let key1 = Key::from_bytes([1u8; 32]);
        let key2 = Key::from_bytes([2u8; 32]);
        let mut ks = KeyState::initial(key1);

        ks.rotate(key2);
        assert!(ks.previous().is_some());

        ks.clear_previous();
        assert!(ks.previous().is_none());
        assert!(ks.previous_epoch().is_none());
        // Current key should remain
        assert_eq!(ks.epoch(), 1);
    }

    #[test]
    fn test_kernel_state_rotate_key() {
        let key1 = Key::from_bytes([1u8; 32]);
        let key2 = Key::from_bytes([2u8; 32]);
        let mut ks = KernelState::initial(key1.clone(), PolicyState::deny_all());

        assert_eq!(ks.key_epoch(), 0);

        ks.rotate_key(key2.clone());

        assert_eq!(ks.key_epoch(), 1);
        assert_eq!(ks.hmac_key().as_bytes(), key2.as_bytes());
    }

    #[test]
    fn test_kernel_dispatch_rotate_key() {
        let key1 = Key::from_bytes([1u8; 32]);
        let key2 = Key::from_bytes([2u8; 32]);
        let core = KernelStateCore::empty(key1);

        let result = kernel_dispatch(
            core,
            KernelRequest::RotateKey {
                new_key: key2.clone(),
            },
        );
        assert!(result.is_ok());

        let new_core = result.unwrap();
        assert_eq!(new_core.key_state().epoch(), 1);
        assert_eq!(new_core.hmac_key().as_bytes(), key2.as_bytes());
    }

    // ============== BINARY SEARCH EDGE-CASE TESTS ==============

    #[test]
    fn test_plugin_meta_grant_revoke_holds_empty() {
        let mut pm = PluginMeta::empty();
        assert!(!pm.holds_cap(0));
        assert!(!pm.holds_cap(u128::MAX));

        pm.revoke_cap(42); // no-op on empty
        assert!(pm.held_caps.is_empty());
    }

    #[test]
    fn test_plugin_meta_grant_sorted_order() {
        let mut pm = PluginMeta::empty();
        pm.grant_cap(5);
        pm.grant_cap(1);
        pm.grant_cap(9);
        pm.grant_cap(3);
        assert_eq!(pm.held_caps, vec![1, 3, 5, 9]);
    }

    #[test]
    fn test_plugin_meta_grant_dedup() {
        let mut pm = PluginMeta::empty();
        pm.grant_cap(3);
        pm.grant_cap(3);
        pm.grant_cap(3);
        assert_eq!(pm.held_caps, vec![3]);
    }

    #[test]
    fn test_plugin_meta_revoke_first_middle_last() {
        let mut pm = PluginMeta::empty();
        pm.grant_cap(1);
        pm.grant_cap(5);
        pm.grant_cap(9);

        pm.revoke_cap(1); // first
        assert_eq!(pm.held_caps, vec![5, 9]);

        pm.grant_cap(1);
        pm.revoke_cap(5); // middle
        assert_eq!(pm.held_caps, vec![1, 9]);

        pm.grant_cap(5);
        pm.revoke_cap(9); // last
        assert_eq!(pm.held_caps, vec![1, 5]);
    }

    #[test]
    fn test_set_plugin_meta_normalizes_unsorted_held_caps() {
        let mut core = KernelStateCore::empty(Key::empty());
        let meta = PluginMeta {
            level: SecurityLevel::Internal,
            held_caps: vec![9, 3, 5, 3, 1, 5], // unsorted, duplicates
        };
        core.set_plugin_meta(100, meta);
        let stored = core.get_plugin_meta(100);
        assert_eq!(stored.held_caps, vec![1, 3, 5, 9]); // sorted + deduped
    }

    #[test]
    fn test_kernel_state_core_plugin_meta_upsert_edge_cases() {
        let mut core = KernelStateCore::empty(Key::empty());

        // Insert into empty vec
        core.set_plugin_meta(
            50,
            PluginMeta {
                level: SecurityLevel::Public,
                held_caps: vec![],
            },
        );
        assert_eq!(core.plugin_metas.len(), 1);

        // Insert at beginning
        core.set_plugin_meta(
            10,
            PluginMeta {
                level: SecurityLevel::Internal,
                held_caps: vec![],
            },
        );
        assert_eq!(core.plugin_metas[0].0, 10);

        // Insert at end
        core.set_plugin_meta(
            90,
            PluginMeta {
                level: SecurityLevel::Secret,
                held_caps: vec![],
            },
        );
        assert_eq!(core.plugin_metas[2].0, 90);

        // Insert in middle
        core.set_plugin_meta(
            30,
            PluginMeta {
                level: SecurityLevel::Confidential,
                held_caps: vec![],
            },
        );
        let ids: Vec<u128> = core.plugin_metas.iter().map(|(id, _)| *id).collect();
        assert_eq!(ids, vec![10, 30, 50, 90]);

        // Update existing (middle)
        core.set_plugin_meta(
            50,
            PluginMeta {
                level: SecurityLevel::Secret,
                held_caps: vec![1],
            },
        );
        assert_eq!(core.get_plugin_meta(50).level, SecurityLevel::Secret);
        assert_eq!(core.plugin_metas.len(), 4); // no growth
    }

    #[test]
    fn test_kernel_state_core_resource_meta_upsert_edge_cases() {
        let mut core = KernelStateCore::empty(Key::empty());

        // Insert into empty
        core.set_resource_meta(
            5,
            ResourceMeta {
                level: SecurityLevel::Public,
            },
        );
        assert_eq!(core.resource_metas.len(), 1);

        // Insert at beginning
        core.set_resource_meta(
            1,
            ResourceMeta {
                level: SecurityLevel::Internal,
            },
        );
        assert_eq!(core.resource_metas[0].0, 1);

        // Update existing
        core.set_resource_meta(
            5,
            ResourceMeta {
                level: SecurityLevel::Secret,
            },
        );
        assert_eq!(core.get_resource_meta(5).level, SecurityLevel::Secret);
        assert_eq!(core.resource_metas.len(), 2);
    }

    #[test]
    fn test_revocation_state_single_element() {
        let mut rs = RevocationState::empty();
        rs.insert_mut(make_test_cap(42, None)).unwrap();

        assert!(rs.is_valid(42));
        assert!(!rs.is_valid(41));
        assert!(!rs.is_valid(43));
        assert_eq!(rs.cap_count(), 1);
    }

    #[test]
    fn test_revocation_state_insert_at_boundaries() {
        let mut rs = RevocationState::empty();
        // Insert at end, beginning, middle in that order
        rs.insert_mut(make_test_cap(100, None)).unwrap();
        rs.insert_mut(make_test_cap(1, None)).unwrap();
        rs.insert_mut(make_test_cap(50, None)).unwrap();

        let ids = rs.cap_ids();
        assert_eq!(ids, vec![1, 50, 100]);
    }
}
