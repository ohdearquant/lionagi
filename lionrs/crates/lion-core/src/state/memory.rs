// Copyright (C) 2026 HaiyangLi
// SPDX-License-Identifier: Apache-2.0
//! Lion State Memory
//!
//! Corresponds to: Lion/State/Memory.lean
//!
//! Linear memory model for WASM sandboxes and ghost state for verification.
//! Uses 4KiB page-granularity storage for efficient sparse memory.

use std::collections::BTreeMap;

use crate::types::{MemAddr, PluginId, Size};

/// Page size bits (4KiB pages)
const PAGE_BITS: u32 = 12;
/// Page size in bytes
const PAGE_SIZE: usize = 1 << PAGE_BITS; // 4096

/// Get the page number for an address.
#[inline]
fn page_of(addr: u64) -> u64 {
    addr >> PAGE_BITS
}

/// Get the byte offset within a page.
#[inline]
fn offset_in_page(addr: u64) -> usize {
    (addr & ((1u64 << PAGE_BITS) - 1)) as usize
}

/// Error type for memory operations
#[derive(Debug)]
pub enum MemoryError {
    /// Address is out of bounds (addr, len, bounds)
    OutOfBounds(MemAddr, Size, Size),
    /// Double free detected
    DoubleFree(MemAddr),
    /// Use after free detected
    UseAfterFree(MemAddr),
    /// Address was never allocated
    NotAllocated(MemAddr),
}

/// Linear memory region for a plugin (WASM model)
///
/// Corresponds to Lean: `structure LinearMemory`
///
/// INVARIANTS:
/// - All reads from uninitialized addresses return 0
/// - Writes only modify the specified address
/// - Bounds are immutable after creation
///
/// Uses 4KiB paged storage -- only touched pages are allocated.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LinearMemory {
    /// Page storage (sparse: only touched pages are allocated)
    pub(crate) pages: BTreeMap<u64, Box<[u8; PAGE_SIZE]>>,

    /// Memory bounds in bytes
    ///
    /// Corresponds to Lean: `bounds : Nat`
    pub(crate) bounds: Size,
}

impl LinearMemory {
    /// Create empty linear memory with given size
    ///
    /// Corresponds to Lean: `def LinearMemory.empty (size : Nat) : LinearMemory`
    pub fn empty(size: Size) -> Self {
        LinearMemory {
            pages: BTreeMap::new(),
            bounds: size,
        }
    }

    /// Check if address is within bounds
    ///
    /// Corresponds to Lean: `def LinearMemory.addr_in_bounds`
    pub fn addr_in_bounds(&self, addr: MemAddr, len: Size) -> bool {
        // Overflow-safe check
        match addr.checked_add(len) {
            Some(end) => end <= self.bounds,
            None => false,
        }
    }

    /// Read byte at address (returns 0 for uninitialized / missing pages)
    ///
    /// Corresponds to Lean: `def LinearMemory.read (mem : LinearMemory) (addr : MemAddr) : UInt8`
    ///
    /// Note: Lean uses `mem.bytes.getD addr 0` which returns 0 for missing keys.
    pub fn read(&self, addr: MemAddr) -> u8 {
        match self.pages.get(&page_of(addr)) {
            Some(page) => page[offset_in_page(addr)],
            None => 0,
        }
    }

    /// Write byte at address
    ///
    /// Corresponds to Lean: `def LinearMemory.write`
    ///
    /// Returns the new memory state with the byte written.
    pub fn write(&self, addr: MemAddr, val: u8) -> Self {
        let mut new = self.clone();
        new.write_mut(addr, val);
        new
    }

    /// Write byte at address (mutating version)
    ///
    /// This is an optimization for when we own the memory.
    pub fn write_mut(&mut self, addr: MemAddr, val: u8) {
        let pg = page_of(addr);
        let off = offset_in_page(addr);
        let page = self
            .pages
            .entry(pg)
            .or_insert_with(|| Box::new([0u8; PAGE_SIZE]));
        page[off] = val;
    }

    /// Get the memory bounds
    #[inline]
    pub fn bounds(&self) -> Size {
        self.bounds
    }

    /// Get the number of allocated bytes (pages * PAGE_SIZE)
    #[inline]
    pub fn written_bytes(&self) -> usize {
        self.pages.len() * PAGE_SIZE
    }

    /// Check if memory is empty (no pages allocated)
    #[inline]
    pub fn is_empty(&self) -> bool {
        self.pages.is_empty()
    }

    /// Read a range of bytes
    ///
    /// Returns Err if the range would exceed bounds.
    ///
    /// # Errors
    ///
    /// Returns `MemoryError::OutOfBounds` if the address plus length exceeds the memory bounds.
    pub fn read_range(&self, addr: MemAddr, len: Size) -> Result<Vec<u8>, MemoryError> {
        if !self.addr_in_bounds(addr, len) {
            return Err(MemoryError::OutOfBounds(addr, len, self.bounds));
        }

        let mut result = Vec::with_capacity(len as usize);
        let mut pos = addr;
        let end = addr + len;
        while pos < end {
            let pg = page_of(pos);
            let off = offset_in_page(pos);
            // How many bytes left in this page?
            let page_remaining = PAGE_SIZE - off;
            let needed = (end - pos) as usize;
            let chunk = page_remaining.min(needed);

            match self.pages.get(&pg) {
                Some(page) => result.extend_from_slice(&page[off..off + chunk]),
                None => result.resize(result.len() + chunk, 0),
            }
            pos += chunk as u64;
        }
        Ok(result)
    }

    /// Write a range of bytes
    ///
    /// Returns the new memory state with all bytes written.
    ///
    /// # Errors
    ///
    /// Returns `MemoryError::OutOfBounds` if the address plus data length exceeds the memory bounds.
    pub fn write_range(&self, addr: MemAddr, data: &[u8]) -> Result<Self, MemoryError> {
        let len = data.len() as Size;
        if !self.addr_in_bounds(addr, len) {
            return Err(MemoryError::OutOfBounds(addr, len, self.bounds));
        }

        let mut new = self.clone();
        new.write_range_mut(addr, data);
        Ok(new)
    }

    /// Write a range of bytes (mutating version)
    pub fn write_range_mut(&mut self, addr: MemAddr, data: &[u8]) {
        let mut pos = addr;
        let mut src_off = 0usize;
        while src_off < data.len() {
            let pg = page_of(pos);
            let off = offset_in_page(pos);
            let page_remaining = PAGE_SIZE - off;
            let remaining_data = data.len() - src_off;
            let chunk = page_remaining.min(remaining_data);

            let page = self
                .pages
                .entry(pg)
                .or_insert_with(|| Box::new([0u8; PAGE_SIZE]));
            page[off..off + chunk].copy_from_slice(&data[src_off..src_off + chunk]);

            pos += chunk as u64;
            src_off += chunk;
        }
    }
}

impl Default for LinearMemory {
    /// Default: empty memory with zero bounds
    fn default() -> Self {
        LinearMemory::empty(0)
    }
}

/// Resource lifecycle status
///
/// Corresponds to Lean: `inductive ResourceStatus`
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub enum ResourceStatus {
    /// Resource has not been allocated
    ///
    /// Corresponds to Lean: `| unallocated`
    #[default]
    Unallocated,

    /// Resource is allocated with an owner
    ///
    /// Corresponds to Lean: `| allocated (owner : PluginId)`
    Allocated {
        /// The plugin that owns this resource
        owner: PluginId,
    },

    /// Resource has been freed
    ///
    /// Corresponds to Lean: `| freed`
    Freed,
}

impl ResourceStatus {
    /// Check if the resource is allocated
    #[inline]
    pub fn is_allocated(&self) -> bool {
        matches!(self, ResourceStatus::Allocated { .. })
    }

    /// Check if the resource is freed
    #[inline]
    pub fn is_freed(&self) -> bool {
        matches!(self, ResourceStatus::Freed)
    }

    /// Check if the resource is unallocated
    #[inline]
    pub fn is_unallocated(&self) -> bool {
        matches!(self, ResourceStatus::Unallocated)
    }

    /// Get the owner if allocated
    #[inline]
    pub fn owner(&self) -> Option<PluginId> {
        match self {
            ResourceStatus::Allocated { owner } => Some(*owner),
            _ => None,
        }
    }
}

/// Ghost state for verification (not runtime)
///
/// Corresponds to Lean: `structure MetaState`
///
/// This tracks resource lifecycle for proofs about temporal safety.
/// At runtime, only the freed_set is used for double-free prevention.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MetaState {
    /// Resource status tracking
    ///
    /// Corresponds to Lean: `resources : Std.HashMap MemAddr ResourceStatus`
    /// NOTE: Using BTreeMap for deterministic iteration.
    pub(crate) resources: BTreeMap<MemAddr, ResourceStatus>,

    /// Set of freed addresses
    ///
    /// Corresponds to Lean: `freed_set : Finset MemAddr`
    /// NOTE: Using Vec with sorted uniqueness.
    pub(crate) freed_set: Vec<MemAddr>,
}

impl MetaState {
    /// Create empty meta state
    ///
    /// Corresponds to Lean: `def MetaState.empty : MetaState`
    pub fn empty() -> Self {
        MetaState {
            resources: BTreeMap::new(),
            freed_set: Vec::new(),
        }
    }

    /// Helper: check if addr is in freed_set
    fn is_freed_addr(&self, addr: MemAddr) -> bool {
        self.freed_set.binary_search(&addr).is_ok()
    }

    /// Helper: insert addr into freed_set (sorted uniqueness)
    fn insert_freed_addr(freed_set: &mut Vec<MemAddr>, addr: MemAddr) {
        match freed_set.binary_search(&addr) {
            Ok(_) => {} // Already present
            Err(pos) => freed_set.insert(pos, addr),
        }
    }

    /// Check if resource is live (allocated and not freed)
    ///
    /// Corresponds to Lean: `def MetaState.is_live (ms : MetaState) (addr : MemAddr) : Prop`
    pub fn is_live(&self, addr: MemAddr) -> bool {
        match self.resources.get(&addr) {
            Some(ResourceStatus::Allocated { .. }) => !self.is_freed_addr(addr),
            _ => false,
        }
    }

    /// Mark resource as allocated
    ///
    /// Corresponds to Lean: `def MetaState.alloc`
    ///
    /// Returns the new MetaState with the resource marked as allocated.
    pub fn alloc(&self, addr: MemAddr, owner: PluginId) -> Self {
        let mut new_resources = self.resources.clone();
        new_resources.insert(addr, ResourceStatus::Allocated { owner });
        MetaState {
            resources: new_resources,
            freed_set: self.freed_set.clone(),
        }
    }

    /// Mark resource as freed
    ///
    /// Corresponds to Lean: `def MetaState.free`
    ///
    /// **Data Refinement Note (from Lean)**: The `resources` field is NOT updated.
    /// Only `freed_set` is used for double-free prevention.
    pub fn free(&self, addr: MemAddr) -> Self {
        let mut new_freed_set = self.freed_set.clone();
        Self::insert_freed_addr(&mut new_freed_set, addr);
        MetaState {
            resources: self.resources.clone(),
            freed_set: new_freed_set,
        }
    }

    /// Mark resource as allocated (mutating version)
    pub fn alloc_mut(&mut self, addr: MemAddr, owner: PluginId) {
        self.resources
            .insert(addr, ResourceStatus::Allocated { owner });
    }

    /// Mark resource as freed (mutating version)
    ///
    /// Requires the resource to be currently allocated. Updates the resource
    /// status to `Freed` and adds the address to the freed set.
    ///
    /// # Errors
    ///
    /// Returns `MemoryError::DoubleFree` if the address has already been freed.
    /// Returns `MemoryError::NotAllocated` if the address was never allocated.
    pub fn free_mut(&mut self, addr: MemAddr) -> Result<(), MemoryError> {
        if self.is_freed_addr(addr) {
            return Err(MemoryError::DoubleFree(addr));
        }

        match self.resources.get_mut(&addr) {
            Some(status @ ResourceStatus::Allocated { .. }) => {
                *status = ResourceStatus::Freed;
                Self::insert_freed_addr(&mut self.freed_set, addr);
                Ok(())
            }
            Some(ResourceStatus::Freed) => Err(MemoryError::DoubleFree(addr)),
            _ => Err(MemoryError::NotAllocated(addr)),
        }
    }

    /// Get the number of allocated resources
    pub fn allocated_count(&self) -> usize {
        self.resources.values().filter(|s| s.is_allocated()).count()
    }

    /// Get the number of freed resources
    #[inline]
    pub fn freed_count(&self) -> usize {
        self.freed_set.len()
    }

    /// Get the number of resources in the map
    #[inline]
    pub fn resource_count(&self) -> usize {
        self.resources.len()
    }

    /// Check if an address has been freed
    #[inline]
    pub fn is_freed(&self, addr: MemAddr) -> bool {
        self.freed_set.binary_search(&addr).is_ok()
    }

    /// Get the status of a resource
    #[inline]
    pub fn get_status(&self, addr: MemAddr) -> Option<&ResourceStatus> {
        self.resources.get(&addr)
    }
}

impl Default for MetaState {
    fn default() -> Self {
        MetaState::empty()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_linear_memory_empty() {
        let mem = LinearMemory::empty(1024);
        assert_eq!(mem.bounds(), 1023);
        assert!(mem.is_empty());
    }

    #[test]
    fn test_linear_memory_read_uninitialized() {
        let mem = LinearMemory::empty(1024);
        // All uninitialized reads return 0
        assert_eq!(mem.read(0), 0);
        assert_eq!(mem.read(100), 0);
        assert_eq!(mem.read(1023), 0);
    }

    #[test]
    fn test_linear_memory_write_read() {
        let mem = LinearMemory::empty(1024);
        let mem = mem.write(100, 42);
        assert_eq!(mem.read(100), 42);
        // Other addresses still 0
        assert_eq!(mem.read(99), 0);
        assert_eq!(mem.read(101), 0);
    }

    #[test]
    fn test_linear_memory_bounds_check() {
        let mem = LinearMemory::empty(1024);
        assert!(mem.addr_in_bounds(0, 1024));
        assert!(mem.addr_in_bounds(1023, 1));
        assert!(!mem.addr_in_bounds(1024, 1));
        assert!(!mem.addr_in_bounds(0, 1025));
        // Overflow protection
        assert!(!mem.addr_in_bounds(u64::MAX, 1));
    }

    #[test]
    fn test_linear_memory_read_range() {
        let mem = LinearMemory::empty(1024);
        let mem = mem.write(10, 1);
        let mem = mem.write(11, 2);
        let mem = mem.write(12, 3);

        let result = mem.read_range(10, 3);
        assert!(result.is_ok());
        assert_eq!(result.ok(), Some(vec![1, 2, 3]));
    }

    #[test]
    fn test_linear_memory_read_range_out_of_bounds() {
        let mem = LinearMemory::empty(100);
        let result = mem.read_range(90, 20);
        assert!(matches!(result, Err(MemoryError::OutOfBounds(_, _, _))));
    }

    #[test]
    fn test_resource_status() {
        let unalloc = ResourceStatus::Unallocated;
        assert!(unalloc.is_unallocated());
        assert!(!unalloc.is_allocated());
        assert!(!unalloc.is_freed());
        assert_eq!(unalloc.owner(), None);

        let alloc = ResourceStatus::Allocated { owner: 42 };
        assert!(!alloc.is_unallocated());
        assert!(alloc.is_allocated());
        assert!(!alloc.is_freed());
        assert_eq!(alloc.owner(), Some(42));

        let freed = ResourceStatus::Freed;
        assert!(!freed.is_unallocated());
        assert!(!freed.is_allocated());
        assert!(freed.is_freed());
        assert_eq!(freed.owner(), None);
    }

    #[test]
    fn test_meta_state_empty() {
        let ms = MetaState::empty();
        assert_eq!(ms.allocated_count(), 0);
        assert_eq!(ms.freed_count(), 0);
        assert!(!ms.is_live(0));
    }

    #[test]
    fn test_meta_state_alloc_makes_live() {
        let ms = MetaState::empty();
        let ms = ms.alloc(100, 1);
        assert!(ms.is_live(100));
        assert_eq!(ms.allocated_count(), 1);
    }

    #[test]
    fn test_meta_state_free_makes_not_live() {
        let ms = MetaState::empty();
        let ms = ms.alloc(100, 1);
        assert!(ms.is_live(100));

        let ms = ms.free(100);
        assert!(!ms.is_live(100));
        assert!(ms.is_freed(100));
    }

    #[test]
    fn test_meta_state_alloc_preserves_live() {
        // Corresponds to Lean theorem: alloc_preserves_is_live
        let ms = MetaState::empty();
        let ms = ms.alloc(100, 1);
        let ms = ms.alloc(200, 2);

        // Both should be live
        assert!(ms.is_live(100));
        assert!(ms.is_live(200));
    }

    #[test]
    fn test_meta_state_free_preserves_live() {
        // Corresponds to Lean theorem: free_preserves_is_live
        let ms = MetaState::empty();
        let ms = ms.alloc(100, 1);
        let ms = ms.alloc(200, 2);

        // Free one
        let ms = ms.free(100);

        // 100 not live, 200 still live
        assert!(!ms.is_live(100));
        assert!(ms.is_live(200));
    }

    #[test]
    fn test_meta_state_double_free_detection() {
        let mut ms = MetaState::empty();
        ms.alloc_mut(100, 1);

        // First free succeeds
        assert!(ms.free_mut(100).is_ok());

        // Second free fails
        let result = ms.free_mut(100);
        assert!(matches!(result, Err(MemoryError::DoubleFree(100))));
    }

    #[test]
    fn test_linear_memory_page_spanning_write_read() {
        // Write data that spans a page boundary
        let mem = LinearMemory::empty(8192);
        let boundary = PAGE_SIZE as u64 - 2; // 2 bytes before page boundary
        let data = vec![0xAA, 0xBB, 0xCC, 0xDD]; // 4 bytes spanning boundary
        let mem = mem.write_range(boundary, &data).unwrap();

        let read_back = mem.read_range(boundary, 4).unwrap();
        assert_eq!(read_back, data);

        // Verify individual bytes
        assert_eq!(mem.read(boundary), 0xAA);
        assert_eq!(mem.read(boundary + 1), 0xBB);
        assert_eq!(mem.read(boundary + 2), 0xCC); // on next page
        assert_eq!(mem.read(boundary + 3), 0xDD);
    }
}
