// Copyright (C) 2026 HaiyangLi
// SPDX-License-Identifier: Apache-2.0
//! Lion Core Cryptographic Operations
//!
//! Corresponds to: Lion/Core/Crypto.lean
//!
//! Real HMAC-SHA256 implementation for capability sealing.

use hmac::{Hmac, Mac};
use sha2::Sha256;
use subtle::ConstantTimeEq;

use crate::types::{CapPayload, Key, SealedTag};

type HmacSha256 = Hmac<Sha256>;

/// Seal a capability payload with HMAC-SHA256
///
/// Corresponds to Lean: `def seal_cap`
#[allow(dead_code)] // Lean correspondence
#[allow(clippy::expect_used)] // HMAC-SHA256 accepts any key length; infallible
pub(crate) fn seal_payload(key: &Key, payload: &CapPayload) -> SealedTag {
    let mut mac = HmacSha256::new_from_slice(key.as_bytes()).expect("HMAC accepts any key length");

    // Serialize payload deterministically
    let data = serialize_payload(payload);
    mac.update(&data);

    let result = mac.finalize();
    SealedTag::from_bytes(result.into_bytes().into())
}

/// Verify a sealed tag against a payload
///
/// Corresponds to Lean: `def verify_seal`
///
/// Uses constant-time comparison to prevent timing attacks.
#[allow(dead_code)] // Lean correspondence
#[allow(clippy::expect_used)] // HMAC-SHA256 accepts any key length; infallible
pub(crate) fn verify_seal(key: &Key, payload: &CapPayload, tag: &SealedTag) -> bool {
    let mut mac = HmacSha256::new_from_slice(key.as_bytes()).expect("HMAC accepts any key length");
    let data = serialize_payload(payload);
    mac.update(&data);
    let result = mac.finalize();
    constant_time_eq(&result.into_bytes(), tag.as_bytes())
}

/// Constant-time byte comparison
#[inline]
fn constant_time_eq(a: &[u8], b: &[u8]) -> bool {
    if a.len() != b.len() {
        return false;
    }
    a.ct_eq(b).into()
}

/// Serialize payload to fixed-size byte array (deterministic, zero-alloc)
///
/// Layout (64 bytes total):
///   holder  : u128 LE  (bytes  0..16)
///   target  : u128 LE  (bytes 16..32)
///   epoch   : u64  LE  (bytes 32..40)
///   parent  : u128 LE  (bytes 40..56)  — 0 = None, id+1 = Some(id)
///   rights  : u64  LE  (bytes 56..64)  — u16 bitmap widened to u64
#[allow(clippy::expect_used)] // SEC-001: deliberate panic for domain separation violation
fn serialize_payload(payload: &CapPayload) -> [u8; 64] {
    let mut data = [0u8; 64];

    data[0..16].copy_from_slice(&payload.holder().to_le_bytes());
    data[16..32].copy_from_slice(&payload.target().to_le_bytes());
    data[32..40].copy_from_slice(&payload.epoch().to_le_bytes());

    // Parent: 0 for None, id+1 for Some(id) to distinguish None from Some(0).
    // Use checked_add to prevent collision at u128::MAX (two distinct payloads
    // must never produce the same serialized bytes).
    let parent_val = match payload.parent() {
        None => 0u128,
        Some(p) => p
            .checked_add(1)
            .expect("parent_id must be < u128::MAX for unique HMAC domain separation"),
    };
    data[40..56].copy_from_slice(&parent_val.to_le_bytes());

    // Rights as u64 bitmap (to_bits returns u16; widen to u64 to preserve wire format)
    data[56..64].copy_from_slice(&u64::from(payload.rights().to_bits()).to_le_bytes());

    data
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::types::{Right, Rights};

    #[test]
    fn test_seal_verify_roundtrip() {
        let key = Key::from_bytes([1u8; 32]);
        let payload = CapPayload::new(1, 100, Rights::singleton(Right::Read), None, 0);

        let tag = seal_payload(&key, &payload);
        assert!(verify_seal(&key, &payload, &tag));
    }

    #[test]
    fn test_wrong_key_fails() {
        let key1 = Key::from_bytes([1u8; 32]);
        let key2 = Key::from_bytes([2u8; 32]);
        let payload = CapPayload::new(1, 100, Rights::singleton(Right::Read), None, 0);

        let tag = seal_payload(&key1, &payload);
        assert!(!verify_seal(&key2, &payload, &tag));
    }

    #[test]
    fn test_modified_payload_fails() {
        let key = Key::from_bytes([1u8; 32]);
        let payload1 = CapPayload::new(1, 100, Rights::singleton(Right::Read), None, 0);
        let payload2 = CapPayload::new(1, 101, Rights::singleton(Right::Read), None, 0);

        let tag = seal_payload(&key, &payload1);
        assert!(!verify_seal(&key, &payload2, &tag));
    }

    #[test]
    fn test_different_payloads_different_tags() {
        let key = Key::from_bytes([1u8; 32]);
        let p1 = CapPayload::new(1, 100, Rights::singleton(Right::Read), None, 0);
        let p2 = CapPayload::new(2, 100, Rights::singleton(Right::Read), None, 0);

        let t1 = seal_payload(&key, &p1);
        let t2 = seal_payload(&key, &p2);

        assert_ne!(t1.as_bytes(), t2.as_bytes());
    }

    #[test]
    fn test_serialize_payload_length_stable() {
        // Regression: layout is holder(16) + target(16) + epoch(8) + parent(16) + rights(8) = 64
        let payload = CapPayload::new(1, 100, Rights::singleton(Right::Read), None, 0);
        let data = serialize_payload(&payload);
        assert_eq!(
            data.len(),
            64,
            "Payload serialization must be exactly 64 bytes (holder:16 + target:16 + epoch:8 + parent:16 + rights:8)"
        );
    }

    #[test]
    fn test_parent_none_vs_some_zero_distinct() {
        // SEC-001: parent=None and parent=Some(0) must produce distinct seals
        let key = Key::from_bytes([1u8; 32]);
        let p_none = CapPayload::new(1, 100, Rights::singleton(Right::Read), None, 0);
        let p_zero = CapPayload::new(1, 100, Rights::singleton(Right::Read), Some(0), 0);

        let t_none = seal_payload(&key, &p_none);
        let t_zero = seal_payload(&key, &p_zero);

        assert_ne!(
            t_none.as_bytes(),
            t_zero.as_bytes(),
            "parent=None and parent=Some(0) must produce distinct seals"
        );
    }

    #[test]
    fn test_parent_adjacent_ids_distinct() {
        // SEC-001: adjacent parent IDs must produce distinct seals
        let key = Key::from_bytes([1u8; 32]);
        let p1 = CapPayload::new(1, 100, Rights::singleton(Right::Read), Some(42), 0);
        let p2 = CapPayload::new(1, 100, Rights::singleton(Right::Read), Some(43), 0);

        let t1 = seal_payload(&key, &p1);
        let t2 = seal_payload(&key, &p2);

        assert_ne!(t1.as_bytes(), t2.as_bytes());
    }

    #[test]
    #[should_panic(expected = "parent_id must be < u128::MAX")]
    fn test_parent_u128_max_panics() {
        // SEC-001: parent=u128::MAX must panic (not silently collide)
        let payload = CapPayload::new(1, 100, Rights::singleton(Right::Read), Some(u128::MAX), 0);
        serialize_payload(&payload);
    }
}
