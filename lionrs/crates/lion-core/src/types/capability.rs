// Copyright (C) 2026 HaiyangLi
// SPDX-License-Identifier: Apache-2.0
//! Lion Core Capability and Crypto Types
//!
//! Corresponds to: Lion/Core/Crypto.lean
//!
//! Cryptographic primitives for capability sealing.
//! HMAC-SHA256 for unforgeability.

use super::{CapId, PluginId, ResourceId, Rights};

/// Secret key type (256-bit fixed-size)
///
/// Corresponds to Lean: `def Key := List UInt8`
///
/// SECURITY: This type is intentionally opaque. Keys should never be
/// exposed outside the kernel.
///
/// INVARIANT: Keys are always exactly 32 bytes (256 bits).
/// Using a fixed-size array prevents:
/// - Length-related bugs
/// - Heap allocation (easier to zeroize)
/// - Variable-length timing attacks
#[derive(Clone, PartialEq, Eq)]
#[must_use = "cryptographic keys must be stored securely"]
pub struct Key {
    /// Internal key bytes (exactly 32 bytes)
    ///
    /// SECURITY: pub(crate) to allow kernel access but prevent external leakage
    pub(crate) bytes: [u8; 32],
}

impl Key {
    /// Key size in bytes
    pub const SIZE: usize = 32;

    /// Create a key from a 32-byte array
    ///
    /// SECURITY: This constructor is pub(crate) to prevent arbitrary key creation
    /// outside the kernel module.
    #[allow(dead_code)] // Lean correspondence
    pub(crate) fn from_bytes(bytes: [u8; 32]) -> Self {
        Key { bytes }
    }

    /// Create an empty (zero) key (for default/placeholder purposes only)
    ///
    /// SECURITY: This should only be used for initialization; real keys
    /// should come from secure random sources.
    pub(crate) fn empty() -> Self {
        Key { bytes: [0u8; 32] }
    }

    /// Get the key length in bytes (always 32)
    #[inline]
    pub const fn len(&self) -> usize {
        Self::SIZE
    }

    /// Check if the key is all zeros.
    ///
    /// Uses constant-time comparison to avoid leaking key material
    /// through timing side-channels.
    pub fn is_empty(&self) -> bool {
        use subtle::ConstantTimeEq;
        self.bytes.ct_eq(&[0u8; 32]).into()
    }

    /// Get key bytes (kernel-internal only)
    pub(crate) fn as_bytes(&self) -> &[u8] {
        &self.bytes
    }
}

impl std::fmt::Debug for Key {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        // Never print actual key bytes
        f.debug_struct("Key")
            .field("len", &self.bytes.len())
            .finish_non_exhaustive()
    }
}

// NOTE: No Default impl for Key - empty keys violate cryptographic security.
// Use Key::from_bytes() with proper key material within kernel code.

/// Sealed tag - what plugins actually hold at runtime
///
/// Corresponds to Lean: `def SealedTag := List UInt8`
///
/// The key is NOT part of this type's structure.
/// This is the opaque bytes that plugins see.
/// Fixed 32 bytes (HMAC-SHA256 output size).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct SealedTag {
    /// Internal tag bytes (exactly 32 bytes)
    pub(crate) bytes: [u8; 32],
}

impl SealedTag {
    /// Create a sealed tag from a 32-byte array.
    pub fn from_bytes(bytes: [u8; 32]) -> Self {
        SealedTag { bytes }
    }

    /// Create an empty (zero) sealed tag
    #[allow(dead_code)] // Lean correspondence
    pub(crate) fn empty() -> Self {
        SealedTag { bytes: [0u8; 32] }
    }

    /// Get the tag length in bytes (always 32)
    #[inline]
    pub const fn len(&self) -> usize {
        32
    }

    /// Check if the tag is all zeros
    pub fn is_empty(&self) -> bool {
        self.bytes == [0u8; 32]
    }

    /// Get tag bytes as slice
    pub fn as_bytes(&self) -> &[u8] {
        &self.bytes
    }
}

// NOTE: No Default impl for SealedTag - empty tags are not valid seals.
// Sealed tags must be created through proper sealing operations.

/// Runtime tag type used in capabilities (key NOT visible)
///
/// Corresponds to Lean: `abbrev RuntimeTag := SealedTag`
pub type RuntimeTag = SealedTag;

/// Hash32 type: 32-byte hash
///
/// Corresponds to Lean: `def Hash32 := List UInt8`
///
/// BLAKE3 output for content-addressed storage and event chain integrity.
///
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct Hash32 {
    /// Internal hash bytes (exactly 32 bytes)
    pub(crate) bytes: [u8; 32],
}

impl Hash32 {
    /// Hash size in bytes
    pub const SIZE: usize = 32;

    /// Create a hash from a slice (kernel-only)
    ///
    /// SECURITY: pub(crate) to prevent external code from creating arbitrary hashes
    /// without computing them. Hashes should only come from actual hash computations.
    ///
    /// Returns `None` if `slice.len() != 32`.
    #[allow(dead_code)] // Lean correspondence
    pub(crate) fn from_slice(slice: &[u8]) -> Option<Self> {
        if slice.len() != 32 {
            return None;
        }
        let mut bytes = [0u8; 32];
        let mut i = 0;
        while i < 32 {
            bytes[i] = slice[i];
            i += 1;
        }
        Some(Hash32 { bytes })
    }

    /// Create the zero hash (genesis block)
    ///
    /// Corresponds to Lean: `def Hash32.zero : Hash32`
    pub fn zero() -> Self {
        Hash32 { bytes: [0u8; 32] }
    }

    /// Check if this is the zero hash
    pub fn is_zero(&self) -> bool {
        self.bytes == [0u8; 32]
    }

    /// Get hash bytes as slice
    pub fn as_bytes(&self) -> &[u8] {
        &self.bytes
    }

    /// Get the hash length in bytes (always 32)
    pub fn len(&self) -> usize {
        Self::SIZE
    }

    /// Check if the hash is empty (always false for Hash32)
    pub fn is_empty(&self) -> bool {
        false // Hash32 always has 32 bytes
    }
}

impl Default for Hash32 {
    fn default() -> Self {
        Hash32::zero()
    }
}

/// Symbolic HMAC tag - for proof purposes only
///
/// Corresponds to Lean: `inductive SymbolicTag`
///
/// This type represents "the tag that would be produced by HMAC(key, payload)".
/// Plugins NEVER see this type; at runtime capabilities contain opaque SealedTag bytes.
///
/// Injectivity holds by construction: if SymbolicTag { key: k1, payload: p1 } ==
/// SymbolicTag { key: k2, payload: p2 }, then k1 == k2 and p1 == p2.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SymbolicTag {
    /// The key used for this tag (proof-internal, never exposed to plugins)
    pub(crate) key: Key,
    /// The payload that was signed
    pub(crate) payload: Vec<u8>,
}

impl SymbolicTag {
    /// Create a new symbolic tag (proof-internal only)
    #[allow(dead_code)] // Lean correspondence
    pub(crate) fn new(key: Key, payload: Vec<u8>) -> Self {
        SymbolicTag { key, payload }
    }
}

// NOTE: No Default impl for SymbolicTag - a tag with empty key/payload
// is cryptographically meaningless. SymbolicTag represents hmac(key, payload)
// and must be created through SymbolicTag::new() with valid inputs.

/// BLAKE3 hash (symbolic representation)
///
/// Corresponds to Lean: `inductive Blake3Hash`
///
/// Like HMAC, we use a data-carrying representation for proof purposes.
#[derive(Debug, Clone, PartialEq, Eq, Hash, Default)]
pub struct Blake3Hash {
    /// The data that was hashed
    pub(crate) data: Vec<u8>,
}

impl Blake3Hash {
    /// Create a new Blake3Hash representing hash(data) (kernel-only)
    ///
    /// SECURITY: pub(crate) to prevent external code from creating arbitrary hashes.
    /// Blake3Hash represents a hash computation and should only be created internally.
    #[allow(dead_code)] // Lean correspondence
    pub(crate) fn new(data: Vec<u8>) -> Self {
        Blake3Hash { data }
    }

    /// Get the source data
    pub fn data(&self) -> &[u8] {
        &self.data
    }
}

/// Capability payload (data to be sealed)
///
/// Corresponds to Lean: `structure CapPayload`
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CapPayload {
    /// The plugin holding this capability
    pub(crate) holder: PluginId,
    /// The resource this capability grants access to
    pub(crate) target: ResourceId,
    /// The rights granted by this capability
    pub(crate) rights: Rights,
    /// Parent capability (for delegation tracking)
    pub(crate) parent: Option<CapId>,
    /// Epoch for revocation tracking
    pub(crate) epoch: u64,
}

impl CapPayload {
    /// Create a new capability payload (kernel-only)
    ///
    /// SECURITY: pub(crate) to prevent external code from creating arbitrary payloads.
    /// Capability payloads must be created through kernel operations to maintain
    /// the integrity of the capability system.
    #[allow(dead_code)] // Lean correspondence
    pub(crate) fn new(
        holder: PluginId,
        target: ResourceId,
        rights: Rights,
        parent: Option<CapId>,
        epoch: u64,
    ) -> Self {
        CapPayload {
            holder,
            target,
            rights,
            parent,
            epoch,
        }
    }

    /// Get the holder
    #[inline]
    pub fn holder(&self) -> PluginId {
        self.holder
    }

    /// Get the target
    #[inline]
    pub fn target(&self) -> ResourceId {
        self.target
    }

    /// Get the rights
    #[inline]
    pub fn rights(&self) -> &Rights {
        &self.rights
    }

    /// Get the parent capability ID
    #[inline]
    pub fn parent(&self) -> Option<CapId> {
        self.parent
    }

    /// Get the epoch
    #[inline]
    pub fn epoch(&self) -> u64 {
        self.epoch
    }
}

/// Error type for Capability operations
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CapabilityError {
    /// Invalid holder (must be > 0)
    InvalidHolder,
    /// Empty rights set
    EmptyRights,
    /// Invalid signature
    InvalidSignature,
    /// Capability has been revoked
    Revoked,
    /// Seal verification failed
    SealVerificationFailed,
}

impl std::fmt::Display for CapabilityError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            CapabilityError::InvalidHolder => write!(f, "Holder must be > 0"),
            CapabilityError::EmptyRights => write!(f, "Rights cannot be empty"),
            CapabilityError::InvalidSignature => write!(f, "Invalid capability signature"),
            CapabilityError::Revoked => write!(f, "Capability has been revoked"),
            CapabilityError::SealVerificationFailed => write!(f, "Seal verification failed"),
        }
    }
}

impl std::error::Error for CapabilityError {}

/// Sealed capability with HMAC signature
///
/// Corresponds to Lean: `structure Capability`
///
/// SECURITY: All fields are pub(crate) to prevent forgery.
/// Capabilities can only be created through kernel operations.
///
/// IMPORTANT: signature is RuntimeTag (opaque), NOT SymbolicTag.
/// This prevents key leakage when comparing plugin states.
#[derive(Debug, Clone, PartialEq, Eq)]
#[must_use = "capabilities are security credentials that must be stored or used"]
pub struct Capability {
    /// Unique identifier for this capability
    pub(crate) id: CapId,
    /// The plugin holding this capability
    pub(crate) holder: PluginId,
    /// The resource this capability grants access to
    pub(crate) target: ResourceId,
    /// The rights granted by this capability
    pub(crate) rights: Rights,
    /// Parent capability (for delegation tracking)
    pub(crate) parent: Option<CapId>,
    /// Epoch for revocation tracking
    pub(crate) epoch: u64,
    /// Runtime revocation flag
    pub(crate) valid: bool,
    /// Opaque seal (key NOT visible)
    pub(crate) signature: RuntimeTag,
}

impl Capability {
    /// Create a new capability (kernel-only operation)
    ///
    /// Returns Err if invariants violated.
    ///
    /// Corresponds to Lean: `Capability.WellFormed` requirements:
    /// - h_holder_valid: holder > 0
    /// - h_rights_nonempty: rights != {}
    pub fn new(
        id: CapId,
        holder: PluginId,
        target: ResourceId,
        rights: Rights,
        parent: Option<CapId>,
        epoch: u64,
        signature: RuntimeTag,
    ) -> Result<Self, CapabilityError> {
        // Validate: holder must be > 0
        if holder == 0 {
            return Err(CapabilityError::InvalidHolder);
        }

        // Validate: rights must not be empty
        if rights.is_empty() {
            return Err(CapabilityError::EmptyRights);
        }

        Ok(Capability {
            id,
            holder,
            target,
            rights,
            parent,
            epoch,
            valid: true,
            signature,
        })
    }

    /// Extract payload for verification
    ///
    /// Corresponds to Lean: `def Capability.payload`
    pub fn payload(&self) -> CapPayload {
        CapPayload {
            holder: self.holder,
            target: self.target,
            rights: self.rights.clone(),
            parent: self.parent,
            epoch: self.epoch,
        }
    }

    /// Get the capability ID
    #[inline]
    pub fn id(&self) -> CapId {
        self.id
    }

    /// Get the holder
    #[inline]
    pub fn holder(&self) -> PluginId {
        self.holder
    }

    /// Get the target
    #[inline]
    pub fn target(&self) -> ResourceId {
        self.target
    }

    /// Get the rights
    #[inline]
    pub fn rights(&self) -> &Rights {
        &self.rights
    }

    /// Get the parent capability ID
    #[inline]
    pub fn parent(&self) -> Option<CapId> {
        self.parent
    }

    /// Get the epoch
    #[inline]
    pub fn epoch(&self) -> u64 {
        self.epoch
    }

    /// Check if the capability is valid (not revoked)
    #[inline]
    pub fn is_valid(&self) -> bool {
        self.valid
    }

    /// Get the signature (for verification)
    #[inline]
    pub fn signature(&self) -> &RuntimeTag {
        &self.signature
    }

    /// Revoke this capability (kernel-only operation)
    pub(crate) fn revoke(&mut self) {
        self.valid = false;
    }

    /// Check if this capability has a specific right
    #[inline]
    pub fn has_right(&self, right: super::Right) -> bool {
        self.rights.contains(right)
    }
}

/// Event in an append-only log
///
/// Corresponds to Lean: `structure LogEvent`
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LogEvent {
    /// Event identifier
    pub(crate) id: u64,
    /// Type of event (e.g., "cap_created", "cap_revoked")
    pub(crate) event_type: String,
    /// Event payload data
    pub(crate) payload: Vec<u8>,
    /// Timestamp in microseconds
    pub(crate) timestamp_us: u64,
    /// Event version
    pub(crate) version: u64,
    /// Hash of previous event (for chain integrity)
    pub(crate) prev_hash: Hash32,
}

impl LogEvent {
    /// Create a new log event (kernel-only)
    ///
    /// SECURITY: pub(crate) to prevent external code from creating arbitrary log events.
    /// Log events must be created through kernel audit operations to maintain
    /// integrity of the audit trail.
    #[allow(dead_code)] // Lean correspondence
    pub(crate) fn new(
        id: u64,
        event_type: String,
        payload: Vec<u8>,
        timestamp_us: u64,
        version: u64,
        prev_hash: Hash32,
    ) -> Self {
        LogEvent {
            id,
            event_type,
            payload,
            timestamp_us,
            version,
            prev_hash,
        }
    }

    /// Get the event ID
    #[inline]
    pub fn id(&self) -> u64 {
        self.id
    }

    /// Get the event type
    #[inline]
    pub fn event_type(&self) -> &str {
        &self.event_type
    }

    /// Get the payload
    #[inline]
    pub fn payload(&self) -> &[u8] {
        &self.payload
    }

    /// Get the timestamp
    #[inline]
    pub fn timestamp_us(&self) -> u64 {
        self.timestamp_us
    }

    /// Get the version
    #[inline]
    pub fn version(&self) -> u64 {
        self.version
    }

    /// Get the previous hash
    #[inline]
    pub fn prev_hash(&self) -> &Hash32 {
        &self.prev_hash
    }
}

// NOTE: No Default impl for LogEvent - an event with id=0, empty type,
// and timestamp=0 is not a valid audit record. Log events must be created
// through LogEvent::new() with valid audit data.

#[cfg(test)]
mod tests {
    use super::*;
    use crate::types::Right;

    #[test]
    fn test_key_debug_does_not_leak() {
        let key = Key::from_bytes([
            1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24,
            25, 26, 27, 28, 29, 30, 31, 32,
        ]);
        let debug = format!("{key:?}");
        // Should not contain actual bytes
        assert!(!debug.contains("[1, 2, 3"));
        assert!(debug.contains("len"));
    }

    #[test]
    fn test_hash32_zero() {
        let zero = Hash32::zero();
        assert!(zero.is_zero());
        assert_eq!(zero.len(), 32);
    }

    #[test]
    fn test_capability_validation_holder() {
        // Holder = 0 should fail
        let result = Capability::new(
            1,
            0, // invalid
            1,
            Rights::singleton(Right::Read),
            None,
            0,
            SealedTag::empty(),
        );
        assert_eq!(result, Err(CapabilityError::InvalidHolder));
    }

    #[test]
    fn test_capability_validation_rights() {
        // Empty rights should fail
        let result = Capability::new(
            1,
            1,
            1,
            Rights::empty(), // invalid
            None,
            0,
            SealedTag::empty(),
        );
        assert_eq!(result, Err(CapabilityError::EmptyRights));
    }

    #[test]
    fn test_capability_valid_creation() {
        let result = Capability::new(
            1,
            1,
            1,
            Rights::singleton(Right::Read),
            None,
            0,
            SealedTag::empty(),
        );
        assert!(result.is_ok());

        let cap = result.expect("valid capability");
        assert!(cap.is_valid());
        assert_eq!(cap.id(), 1);
        assert_eq!(cap.holder(), 1);
        assert!(cap.has_right(Right::Read));
    }

    #[test]
    fn test_capability_payload_extraction() {
        let cap = Capability::new(
            1,
            42,
            100,
            Rights::singleton(Right::Write),
            Some(0),
            5,
            SealedTag::empty(),
        )
        .expect("valid capability");

        let payload = cap.payload();
        assert_eq!(payload.holder(), 42);
        assert_eq!(payload.target(), 100);
        assert_eq!(payload.parent(), Some(0));
        assert_eq!(payload.epoch(), 5);
    }

    #[test]
    fn test_capability_revocation() {
        let mut cap = Capability::new(
            1,
            1,
            1,
            Rights::singleton(Right::Read),
            None,
            0,
            SealedTag::empty(),
        )
        .expect("valid capability");

        assert!(cap.is_valid());
        cap.revoke();
        assert!(!cap.is_valid());
    }

    #[test]
    fn test_blake3hash_injectivity_by_construction() {
        // Different data produces different hashes (by construction)
        let h1 = Blake3Hash::new(vec![1, 2, 3]);
        let h2 = Blake3Hash::new(vec![1, 2, 4]);

        assert_ne!(h1, h2);

        // Same data produces equal hashes
        let h3 = Blake3Hash::new(vec![1, 2, 3]);
        assert_eq!(h1, h3);
    }

    #[test]
    fn test_log_event_creation() {
        let event = LogEvent::new(
            1,
            "test_event".into(),
            vec![1, 2, 3],
            1000000,
            1,
            Hash32::zero(),
        );

        assert_eq!(event.id(), 1);
        assert_eq!(event.event_type(), "test_event");
        assert_eq!(event.payload(), &[1, 2, 3]);
        assert_eq!(event.timestamp_us(), 1000000);
        assert_eq!(event.version(), 1);
        assert!(event.prev_hash().is_zero());
    }
}
