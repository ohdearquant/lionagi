// Copyright (C) 2026 HaiyangLi
// SPDX-License-Identifier: Apache-2.0
//! Rights algebra for capability-based access control.
//!
//! 10-right system with intersection as combine.
//! Backed by a u16 bitset for O(1) set operations.

use std::fmt;
use std::hash::{Hash, Hasher};
use std::ops::{BitAnd, BitAndAssign, BitOr, BitOrAssign, Not, Sub};
use std::str::FromStr;

/// Individual access rights.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub enum Right {
    /// Read access to a resource
    Read,
    /// Write/modify access to a resource
    Write,
    /// Execute access (for code resources)
    Execute,
    /// Create new child resources
    Create,
    /// Delete/destroy resources
    Delete,
    /// Send messages to actors
    Send,
    /// Receive messages from actors
    Receive,
    /// Delegate capabilities to other plugins
    Delegate,
    /// Revoke delegated capabilities
    Revoke,
    /// Declassify data (controlled downgrading for IFC)
    Declassify,
}

impl Right {
    /// Convert to numeric value for ordering/hashing.
    #[inline]
    pub const fn to_u8(self) -> u8 {
        match self {
            Right::Read => 0,
            Right::Write => 1,
            Right::Execute => 2,
            Right::Create => 3,
            Right::Delete => 4,
            Right::Send => 5,
            Right::Receive => 6,
            Right::Delegate => 7,
            Right::Revoke => 8,
            Right::Declassify => 9,
        }
    }

    /// All possible rights as a static array.
    pub const ALL: [Right; 10] = [
        Right::Read,
        Right::Write,
        Right::Execute,
        Right::Create,
        Right::Delete,
        Right::Send,
        Right::Receive,
        Right::Delegate,
        Right::Revoke,
        Right::Declassify,
    ];

    /// Try to convert from u8.
    pub const fn from_u8(value: u8) -> Option<Self> {
        match value {
            0 => Some(Right::Read),
            1 => Some(Right::Write),
            2 => Some(Right::Execute),
            3 => Some(Right::Create),
            4 => Some(Right::Delete),
            5 => Some(Right::Send),
            6 => Some(Right::Receive),
            7 => Some(Right::Delegate),
            8 => Some(Right::Revoke),
            9 => Some(Right::Declassify),
            _ => None,
        }
    }

    /// Get the full name of this right.
    #[inline]
    pub const fn name(self) -> &'static str {
        match self {
            Right::Read => "read",
            Right::Write => "write",
            Right::Execute => "execute",
            Right::Create => "create",
            Right::Delete => "delete",
            Right::Send => "send",
            Right::Receive => "receive",
            Right::Delegate => "delegate",
            Right::Revoke => "revoke",
            Right::Declassify => "declassify",
        }
    }

    /// Get the short name (single char) for this right.
    #[inline]
    pub const fn short_name(self) -> char {
        match self {
            Right::Read => 'r',
            Right::Write => 'w',
            Right::Execute => 'x',
            Right::Create => 'c',
            Right::Delete => 'd',
            Right::Send => 's',
            Right::Receive => 'v',
            Right::Delegate => 'g',
            Right::Revoke => 'k',
            Right::Declassify => 'y',
        }
    }

    /// Bit mask for this right.
    #[inline]
    pub const fn as_mask(self) -> u16 {
        1u16 << self.to_u8()
    }
}

impl fmt::Display for Right {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.name())
    }
}

/// Error parsing a Right from string.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ParseRightError {
    input: String,
}

impl fmt::Display for ParseRightError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "invalid right: '{}'", self.input)
    }
}

impl std::error::Error for ParseRightError {}

impl FromStr for Right {
    type Err = ParseRightError;

    fn from_str(s: &str) -> Result<Self, Self::Err> {
        match s.to_lowercase().as_str() {
            "read" | "r" => Ok(Right::Read),
            "write" | "w" => Ok(Right::Write),
            "execute" | "x" => Ok(Right::Execute),
            "create" | "c" => Ok(Right::Create),
            "delete" | "d" => Ok(Right::Delete),
            "send" | "s" => Ok(Right::Send),
            "receive" | "v" => Ok(Right::Receive),
            "delegate" | "g" => Ok(Right::Delegate),
            "revoke" | "k" => Ok(Right::Revoke),
            "declassify" | "y" => Ok(Right::Declassify),
            _ => Err(ParseRightError {
                input: s.to_string(),
            }),
        }
    }
}

#[cfg(feature = "serde")]
impl serde::Serialize for Right {
    fn serialize<S: serde::Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        serializer.serialize_str(self.name())
    }
}

#[cfg(feature = "serde")]
impl<'de> serde::Deserialize<'de> for Right {
    fn deserialize<D: serde::Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        let s = String::deserialize(deserializer)?;
        s.parse().map_err(serde::de::Error::custom)
    }
}

/// Error type for Rights operations.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RightsError {
    /// Attempted to create Rights from empty set when non-empty required
    EmptyRights,
    /// Invalid right value
    InvalidRight(u8),
}

impl std::fmt::Display for RightsError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            RightsError::EmptyRights => write!(f, "Rights set cannot be empty"),
            RightsError::InvalidRight(v) => write!(f, "Invalid right value: {v}"),
        }
    }
}

impl std::error::Error for RightsError {}

/// Mask covering all 10 valid right bits.
const ALL_RIGHTS_MASK: u16 = 0x03FF;

/// Set of rights backed by a u16 bitset.
///
/// Each of the 10 rights maps to one bit (bit 0 = Read, ..., bit 9 = Declassify).
/// All set operations are O(1) bitwise ops.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[repr(transparent)]
#[must_use = "rights sets must be checked or applied"]
pub struct Rights {
    pub(crate) bits: u16,
}

impl Rights {
    /// Create an empty rights set.
    #[inline]
    pub const fn empty() -> Self {
        Rights { bits: 0 }
    }

    /// Create the full rights set containing all 10 rights.
    #[inline]
    pub const fn all() -> Self {
        Rights {
            bits: ALL_RIGHTS_MASK,
        }
    }

    /// Create from a single right.
    #[inline]
    pub const fn singleton(right: Right) -> Self {
        Rights {
            bits: right.as_mask(),
        }
    }

    /// Create from a slice of rights.
    pub fn from_slice(rights: &[Right]) -> Self {
        let mut bits = 0u16;
        let mut i = 0;
        while i < rights.len() {
            bits |= rights[i].as_mask();
            i += 1;
        }
        Rights { bits }
    }

    /// Create from bitmap representation.
    #[inline]
    pub const fn from_bits(bits: u16) -> Self {
        Rights {
            bits: bits & ALL_RIGHTS_MASK,
        }
    }

    /// Read-only access rights.
    #[inline]
    pub const fn read_only() -> Self {
        Rights::singleton(Right::Read)
    }

    /// Read-write access rights.
    pub fn read_write() -> Self {
        Rights::from_slice(&[Right::Read, Right::Write])
    }

    /// Read-write-execute access rights.
    pub fn read_write_execute() -> Self {
        Rights::from_slice(&[Right::Read, Right::Write, Right::Execute])
    }

    /// Check if rights set is empty.
    #[inline]
    #[must_use]
    pub const fn is_empty(&self) -> bool {
        self.bits == 0
    }

    /// Get the number of rights in the set.
    #[inline]
    #[must_use]
    pub const fn len(&self) -> usize {
        self.bits.count_ones() as usize
    }

    /// Check if a specific right is present.
    #[inline]
    #[must_use]
    pub const fn contains(&self, right: Right) -> bool {
        (self.bits & right.as_mask()) != 0
    }

    /// Insert a right into the set. Returns true if the right was not already present.
    #[inline]
    pub fn insert(&mut self, right: Right) -> bool {
        let mask = right.as_mask();
        let was_absent = (self.bits & mask) == 0;
        self.bits |= mask;
        was_absent
    }

    /// Remove a right from the set. Returns true if the right was present.
    #[inline]
    pub fn remove(&mut self, right: Right) -> bool {
        let mask = right.as_mask();
        let was_present = (self.bits & mask) != 0;
        self.bits &= !mask;
        was_present
    }

    /// Subset check: r1 <= r2 iff r1 is subset of r2.
    #[inline]
    #[must_use]
    pub const fn is_subset_of(&self, other: &Rights) -> bool {
        (self.bits & !other.bits) == 0
    }

    /// Superset check.
    #[inline]
    pub const fn is_superset_of(&self, other: &Rights) -> bool {
        other.is_subset_of(self)
    }

    /// Combine rights via intersection (for delegation -- confinement property).
    #[inline]
    pub const fn combine(&self, other: &Rights) -> Rights {
        Rights {
            bits: self.bits & other.bits,
        }
    }

    /// Alias for `combine` -- intersection of two rights sets.
    #[inline]
    pub const fn intersection(&self, other: &Rights) -> Rights {
        self.combine(other)
    }

    /// Union of two rights sets.
    #[inline]
    pub const fn union(&self, other: &Rights) -> Rights {
        Rights {
            bits: self.bits | other.bits,
        }
    }

    /// Difference of two rights sets (self - other).
    #[inline]
    pub const fn difference(&self, other: &Rights) -> Rights {
        Rights {
            bits: self.bits & !other.bits,
        }
    }

    /// Symmetric difference of two rights sets.
    #[inline]
    pub const fn symmetric_difference(&self, other: &Rights) -> Rights {
        Rights {
            bits: self.bits ^ other.bits,
        }
    }

    /// Complement (all rights NOT in this set).
    #[inline]
    pub const fn complement(&self) -> Rights {
        Rights {
            bits: (!self.bits) & ALL_RIGHTS_MASK,
        }
    }

    /// Check if this set contains all rights from another set.
    #[inline]
    #[must_use]
    pub const fn contains_all(&self, other: &Rights) -> bool {
        other.is_subset_of(self)
    }

    /// Check if this set contains any right from another set.
    #[inline]
    #[must_use]
    pub const fn contains_any(&self, other: &Rights) -> bool {
        (self.bits & other.bits) != 0
    }

    /// Return a new set with the given right added.
    #[inline]
    pub const fn with(&self, right: Right) -> Rights {
        Rights {
            bits: self.bits | right.as_mask(),
        }
    }

    /// Return a new set with the given right removed.
    #[inline]
    pub const fn without(&self, right: Right) -> Rights {
        Rights {
            bits: self.bits & !right.as_mask(),
        }
    }

    /// Get rights as a Vec (sorted by discriminant order).
    pub fn to_vec(&self) -> Vec<Right> {
        let mut v = Vec::with_capacity(self.len());
        let mut remaining = self.bits;
        while remaining != 0 {
            let bit = remaining.trailing_zeros() as u8;
            if let Some(r) = Right::from_u8(bit) {
                v.push(r);
            }
            remaining &= remaining - 1; // clear lowest set bit
        }
        v
    }

    /// Iterate over the rights in the set (sorted by discriminant order).
    #[inline]
    pub const fn iter(&self) -> RightsIter {
        RightsIter { bits: self.bits }
    }

    /// Convert to bitmap representation.
    #[inline]
    #[must_use]
    pub const fn to_bits(&self) -> u16 {
        self.bits
    }
}

impl Default for Rights {
    fn default() -> Self {
        Rights::empty()
    }
}

impl Hash for Rights {
    #[inline]
    fn hash<H: Hasher>(&self, state: &mut H) {
        self.bits.hash(state);
    }
}

impl fmt::Display for Rights {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{{")?;
        let mut first = true;
        for right in self.iter() {
            if !first {
                write!(f, ", ")?;
            }
            write!(f, "{right}")?;
            first = false;
        }
        write!(f, "}}")
    }
}

impl FromIterator<Right> for Rights {
    fn from_iter<I: IntoIterator<Item = Right>>(iter: I) -> Self {
        let mut bits = 0u16;
        for right in iter {
            bits |= right.as_mask();
        }
        Rights { bits }
    }
}

/// Iterator over rights in a `Rights` set, yielding them in discriminant order.
#[derive(Debug, Clone)]
pub struct RightsIter {
    bits: u16,
}

impl Iterator for RightsIter {
    type Item = Right;

    #[inline]
    fn next(&mut self) -> Option<Right> {
        if self.bits == 0 {
            return None;
        }
        let bit = self.bits.trailing_zeros() as u8;
        self.bits &= self.bits - 1; // clear lowest set bit
        Right::from_u8(bit)
    }

    #[inline]
    fn size_hint(&self) -> (usize, Option<usize>) {
        let n = self.bits.count_ones() as usize;
        (n, Some(n))
    }
}

impl ExactSizeIterator for RightsIter {}

impl IntoIterator for Rights {
    type Item = Right;
    type IntoIter = RightsIter;

    #[inline]
    fn into_iter(self) -> RightsIter {
        self.iter()
    }
}

impl IntoIterator for &Rights {
    type Item = Right;
    type IntoIter = RightsIter;

    #[inline]
    fn into_iter(self) -> RightsIter {
        self.iter()
    }
}

impl From<Right> for Rights {
    fn from(right: Right) -> Self {
        Rights::singleton(right)
    }
}

impl BitAnd for Rights {
    type Output = Rights;
    fn bitand(self, rhs: Rights) -> Rights {
        self.intersection(&rhs)
    }
}

impl BitAnd for &Rights {
    type Output = Rights;
    fn bitand(self, rhs: &Rights) -> Rights {
        self.intersection(rhs)
    }
}

impl BitOr for Rights {
    type Output = Rights;
    fn bitor(self, rhs: Rights) -> Rights {
        self.union(&rhs)
    }
}

impl BitOr for &Rights {
    type Output = Rights;
    fn bitor(self, rhs: &Rights) -> Rights {
        self.union(rhs)
    }
}

impl Sub for Rights {
    type Output = Rights;
    fn sub(self, rhs: Rights) -> Rights {
        self.difference(&rhs)
    }
}

impl Sub for &Rights {
    type Output = Rights;
    fn sub(self, rhs: &Rights) -> Rights {
        self.difference(rhs)
    }
}

impl BitAndAssign for Rights {
    fn bitand_assign(&mut self, rhs: Rights) {
        self.bits &= rhs.bits;
    }
}

impl BitOrAssign for Rights {
    fn bitor_assign(&mut self, rhs: Rights) {
        self.bits |= rhs.bits;
    }
}

impl Not for Rights {
    type Output = Rights;
    fn not(self) -> Rights {
        self.complement()
    }
}

impl Not for &Rights {
    type Output = Rights;
    fn not(self) -> Rights {
        self.complement()
    }
}

#[cfg(feature = "serde")]
impl serde::Serialize for Rights {
    fn serialize<S: serde::Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        serializer.serialize_u16(self.to_bits())
    }
}

#[cfg(feature = "serde")]
impl<'de> serde::Deserialize<'de> for Rights {
    fn deserialize<D: serde::Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        let bits = u16::deserialize(deserializer)?;
        Ok(Rights::from_bits(bits))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    // ---------------------------------------------------------------
    // 1. empty / all basics
    // ---------------------------------------------------------------
    #[test]
    fn test_empty_and_all() {
        let e = Rights::empty();
        assert_eq!(e.to_bits(), 0);
        assert!(e.is_empty());
        assert_eq!(e.len(), 0);

        let a = Rights::all();
        assert_eq!(a.to_bits(), 0x03FF);
        assert!(!a.is_empty());
        assert_eq!(a.len(), 10);

        // default is empty
        assert_eq!(Rights::default(), e);
    }

    // ---------------------------------------------------------------
    // 2. singleton — each right maps to a unique bit
    // ---------------------------------------------------------------
    #[test]
    fn test_singleton_each_right() {
        let mut seen_bits: u16 = 0;
        for (i, &right) in Right::ALL.iter().enumerate() {
            let s = Rights::singleton(right);
            assert_eq!(s.len(), 1, "singleton should have exactly 1 element");
            assert!(s.contains(right));
            // bit position must equal discriminant
            assert_eq!(s.to_bits(), 1u16 << i);
            // no overlap with any previously seen bit
            assert_eq!(seen_bits & s.to_bits(), 0, "bits must be unique");
            seen_bits |= s.to_bits();
        }
        // after all 10, we should have 0x03FF
        assert_eq!(seen_bits, 0x03FF);
    }

    // ---------------------------------------------------------------
    // 3. from_bits masking
    // ---------------------------------------------------------------
    #[test]
    fn test_from_bits_masking() {
        // High bits are stripped
        assert_eq!(Rights::from_bits(u16::MAX), Rights::all());
        assert_eq!(Rights::from_bits(0), Rights::empty());
        // Only lower 10 bits matter
        assert_eq!(Rights::from_bits(0xFC00), Rights::empty());
        // Bits within range preserved exactly
        assert_eq!(Rights::from_bits(0x0005).to_bits(), 0x0005); // Read | Execute
    }

    // ---------------------------------------------------------------
    // 4. from_bits / to_bits roundtrip
    // ---------------------------------------------------------------
    #[test]
    fn test_from_bits_roundtrip() {
        // Exhaustive over all valid 10-bit patterns
        for bits in 0u16..=0x03FF {
            let r = Rights::from_bits(bits);
            assert_eq!(r.to_bits(), bits);
        }
        // Roundtrip through named constructors
        let cases: &[Rights] = &[
            Rights::empty(),
            Rights::all(),
            Rights::singleton(Right::Delegate),
            Rights::read_only(),
            Rights::read_write(),
            Rights::read_write_execute(),
        ];
        for &r in cases {
            assert_eq!(Rights::from_bits(r.to_bits()), r);
        }
    }

    // ---------------------------------------------------------------
    // 5. contains / insert / remove
    // ---------------------------------------------------------------
    #[test]
    fn test_contains_insert_remove() {
        let mut r = Rights::empty();
        assert!(!r.contains(Right::Read));

        // insert returns true the first time (was absent)
        assert!(r.insert(Right::Read));
        assert!(r.contains(Right::Read));
        assert_eq!(r.len(), 1);

        // insert returns false the second time (already present)
        assert!(!r.insert(Right::Read));
        assert_eq!(r.len(), 1);

        // remove returns true (was present)
        assert!(r.remove(Right::Read));
        assert!(!r.contains(Right::Read));
        assert_eq!(r.len(), 0);

        // remove returns false (was absent)
        assert!(!r.remove(Right::Read));
    }

    // ---------------------------------------------------------------
    // 6. combine = intersection (confinement property)
    // ---------------------------------------------------------------
    #[test]
    fn test_combine_intersection() {
        let rw = Rights::from_slice(&[Right::Read, Right::Write]);
        let rx = Rights::from_slice(&[Right::Read, Right::Execute]);

        let combined = rw.combine(&rx);
        assert_eq!(combined, Rights::singleton(Right::Read));

        // Confinement: subset.combine(superset) == subset
        let subset = Rights::singleton(Right::Read);
        let superset = Rights::all();
        assert_eq!(subset.combine(&superset), subset);

        // combine with empty yields empty
        assert_eq!(rw.combine(&Rights::empty()), Rights::empty());

        // combine with self yields self
        assert_eq!(rw.combine(&rw), rw);

        // combine == intersection (alias)
        assert_eq!(rw.intersection(&rx), rw.combine(&rx));
    }

    // ---------------------------------------------------------------
    // 7. union
    // ---------------------------------------------------------------
    #[test]
    fn test_union() {
        let a = Rights::singleton(Right::Read);
        let b = Rights::singleton(Right::Write);
        let u = a.union(&b);
        assert_eq!(u.len(), 2);
        assert!(u.contains(Right::Read));
        assert!(u.contains(Right::Write));

        // union of all singletons == all()
        let mut acc = Rights::empty();
        for &right in &Right::ALL {
            acc = acc.union(&Rights::singleton(right));
        }
        assert_eq!(acc, Rights::all());

        // union with self is self
        assert_eq!(u.union(&u), u);

        // union with empty is self
        assert_eq!(u.union(&Rights::empty()), u);
    }

    // ---------------------------------------------------------------
    // 8. difference
    // ---------------------------------------------------------------
    #[test]
    fn test_difference() {
        let all = Rights::all();
        let read_only = Rights::singleton(Right::Read);
        let diff = all.difference(&read_only);

        assert!(!diff.contains(Right::Read));
        assert_eq!(diff.len(), 9);
        // Every other right is still present
        for &right in &Right::ALL {
            if right != Right::Read {
                assert!(diff.contains(right));
            }
        }

        // difference with self is empty
        assert_eq!(all.difference(&all), Rights::empty());

        // difference with empty is self
        assert_eq!(all.difference(&Rights::empty()), all);

        // empty minus anything is empty
        assert_eq!(Rights::empty().difference(&all), Rights::empty());
    }

    // ---------------------------------------------------------------
    // 9. symmetric_difference
    // ---------------------------------------------------------------
    #[test]
    fn test_symmetric_difference() {
        let ab = Rights::from_slice(&[Right::Read, Right::Write]);
        let bc = Rights::from_slice(&[Right::Write, Right::Execute]);

        let sd = ab.symmetric_difference(&bc);
        // Read XOR Execute (Write cancels)
        assert!(sd.contains(Right::Read));
        assert!(!sd.contains(Right::Write));
        assert!(sd.contains(Right::Execute));
        assert_eq!(sd.len(), 2);

        // symmetric_difference with self is empty
        assert_eq!(ab.symmetric_difference(&ab), Rights::empty());

        // symmetric_difference with empty is self
        assert_eq!(ab.symmetric_difference(&Rights::empty()), ab);
    }

    // ---------------------------------------------------------------
    // 10. complement
    // ---------------------------------------------------------------
    #[test]
    fn test_complement() {
        // double complement is identity
        for bits in [0u16, 0x03FF, 0x0001, 0x0155, 0x02AA] {
            let r = Rights::from_bits(bits);
            assert_eq!(r.complement().complement(), r);
        }

        assert_eq!(Rights::empty().complement(), Rights::all());
        assert_eq!(Rights::all().complement(), Rights::empty());

        // complement of singleton has 9 elements
        let c = Rights::singleton(Right::Send).complement();
        assert_eq!(c.len(), 9);
        assert!(!c.contains(Right::Send));
    }

    // ---------------------------------------------------------------
    // 11. subset / superset
    // ---------------------------------------------------------------
    #[test]
    fn test_subset_superset() {
        let e = Rights::empty();
        let a = Rights::all();
        let r = Rights::singleton(Right::Read);

        // empty is subset of everything
        assert!(e.is_subset_of(&e));
        assert!(e.is_subset_of(&r));
        assert!(e.is_subset_of(&a));

        // everything is superset of empty
        assert!(a.is_superset_of(&e));
        assert!(r.is_superset_of(&e));

        // all is superset of every singleton
        for &right in &Right::ALL {
            assert!(a.is_superset_of(&Rights::singleton(right)));
            assert!(Rights::singleton(right).is_subset_of(&a));
        }

        // singleton is not a superset of all
        assert!(!r.is_superset_of(&a));

        // disjoint sets: neither is subset of the other
        let w = Rights::singleton(Right::Write);
        assert!(!r.is_subset_of(&w));
        assert!(!w.is_subset_of(&r));
    }

    // ---------------------------------------------------------------
    // 12. contains_all / contains_any
    // ---------------------------------------------------------------
    #[test]
    fn test_contains_all_contains_any() {
        let rw = Rights::from_slice(&[Right::Read, Right::Write]);
        let r = Rights::singleton(Right::Read);
        let x = Rights::singleton(Right::Execute);
        let e = Rights::empty();

        // contains_all
        assert!(rw.contains_all(&r));
        assert!(rw.contains_all(&rw));
        assert!(!r.contains_all(&rw));
        assert!(!rw.contains_all(&x));
        // everything contains_all of empty
        assert!(rw.contains_all(&e));
        assert!(e.contains_all(&e));

        // contains_any
        assert!(rw.contains_any(&r));
        assert!(rw.contains_any(&rw));
        assert!(!rw.contains_any(&x));
        // empty contains_any of nothing
        assert!(!e.contains_any(&rw));
        assert!(!e.contains_any(&e));
    }

    // ---------------------------------------------------------------
    // 13. iter — sorted by discriminant
    // ---------------------------------------------------------------
    #[test]
    fn test_iter_sorted() {
        let all = Rights::all();
        let collected: Vec<Right> = all.iter().collect();
        assert_eq!(collected, Right::ALL.to_vec());

        // Partial set: Read(0), Execute(2), Send(5)
        let partial = Rights::from_slice(&[Right::Send, Right::Read, Right::Execute]);
        let collected: Vec<Right> = partial.iter().collect();
        assert_eq!(collected, vec![Right::Read, Right::Execute, Right::Send]);

        // empty iterator yields nothing
        assert_eq!(Rights::empty().iter().count(), 0);
    }

    // ---------------------------------------------------------------
    // 14. ExactSizeIterator contract
    // ---------------------------------------------------------------
    #[test]
    fn test_iter_count_matches_len() {
        for bits in 0u16..=0x03FF {
            let r = Rights::from_bits(bits);
            let mut iter = r.iter();
            assert_eq!(iter.len(), r.len());
            // consume one element at a time and check size_hint shrinks
            let mut remaining = r.len();
            while let Some(_) = iter.next() {
                remaining -= 1;
                assert_eq!(iter.len(), remaining);
            }
            assert_eq!(remaining, 0);
        }
    }

    // ---------------------------------------------------------------
    // 15. to_vec matches iter
    // ---------------------------------------------------------------
    #[test]
    fn test_to_vec_matches_iter() {
        for bits in [0u16, 1, 0x03FF, 0x0155, 0x02AA, 0x0321] {
            let r = Rights::from_bits(bits);
            let from_vec = r.to_vec();
            let from_iter: Vec<Right> = r.iter().collect();
            assert_eq!(from_vec, from_iter);
        }
    }

    // ---------------------------------------------------------------
    // 16. from_slice — duplicates are implicit no-ops
    // ---------------------------------------------------------------
    #[test]
    fn test_from_slice() {
        // duplicates silently merge (bitwise OR is idempotent)
        let r = Rights::from_slice(&[Right::Read, Right::Read, Right::Write, Right::Write]);
        assert_eq!(r.len(), 2);
        assert!(r.contains(Right::Read));
        assert!(r.contains(Right::Write));

        // empty slice
        assert_eq!(Rights::from_slice(&[]), Rights::empty());

        // all rights
        assert_eq!(Rights::from_slice(&Right::ALL), Rights::all());
    }

    // ---------------------------------------------------------------
    // 17. operator impls match named methods
    // ---------------------------------------------------------------
    #[test]
    fn test_operator_impls() {
        let a = Rights::from_slice(&[Right::Read, Right::Write, Right::Execute]);
        let b = Rights::from_slice(&[Right::Write, Right::Execute, Right::Create]);

        // BitAnd (owned) == intersection
        assert_eq!(a & b, a.intersection(&b));
        // BitAnd (ref) == intersection
        assert_eq!(&a & &b, a.intersection(&b));

        // BitOr (owned) == union
        assert_eq!(a | b, a.union(&b));
        // BitOr (ref) == union
        assert_eq!(&a | &b, a.union(&b));

        // Sub (owned) == difference
        assert_eq!(a - b, a.difference(&b));
        // Sub (ref) == difference
        assert_eq!(&a - &b, a.difference(&b));

        // Not (owned) == complement
        assert_eq!(!a, a.complement());
        // Not (ref) == complement
        assert_eq!(!&a, a.complement());

        // BitAndAssign
        let mut x = a;
        x &= b;
        assert_eq!(x, a & b);

        // BitOrAssign
        let mut y = a;
        y |= b;
        assert_eq!(y, a | b);
    }

    // ---------------------------------------------------------------
    // 18. with / without (functional insert / remove)
    // ---------------------------------------------------------------
    #[test]
    fn test_with_without() {
        let r = Rights::singleton(Right::Read);

        // with adds a right without mutating
        let rw = r.with(Right::Write);
        assert!(rw.contains(Right::Read));
        assert!(rw.contains(Right::Write));
        assert_eq!(rw.len(), 2);
        // original unchanged
        assert_eq!(r.len(), 1);

        // with on already-present is no-op
        assert_eq!(rw.with(Right::Read), rw);

        // without removes a right
        let back = rw.without(Right::Write);
        assert_eq!(back, r);
        // without on absent is no-op
        assert_eq!(r.without(Right::Execute), r);

        // chaining
        let built = Rights::empty()
            .with(Right::Send)
            .with(Right::Receive)
            .without(Right::Send);
        assert_eq!(built, Rights::singleton(Right::Receive));
    }

    // ---------------------------------------------------------------
    // 19. Right::to_u8 / from_u8 roundtrip
    // ---------------------------------------------------------------
    #[test]
    fn test_right_to_u8_from_u8_roundtrip() {
        for &right in &Right::ALL {
            assert_eq!(Right::from_u8(right.to_u8()), Some(right));
        }
        // out of range
        assert_eq!(Right::from_u8(10), None);
        assert_eq!(Right::from_u8(255), None);
    }

    // ---------------------------------------------------------------
    // 20. Right::as_mask consistency
    // ---------------------------------------------------------------
    #[test]
    fn test_right_as_mask() {
        for &right in &Right::ALL {
            assert_eq!(right.as_mask(), 1u16 << right.to_u8());
            // singleton bits must match as_mask
            assert_eq!(Rights::singleton(right).to_bits(), right.as_mask());
        }
    }

    // ---------------------------------------------------------------
    // 21. From<Right> for Rights
    // ---------------------------------------------------------------
    #[test]
    fn test_from_right_for_rights() {
        for &right in &Right::ALL {
            let r: Rights = right.into();
            assert_eq!(r, Rights::singleton(right));
        }
    }

    // ---------------------------------------------------------------
    // 22. FromIterator<Right> for Rights
    // ---------------------------------------------------------------
    #[test]
    fn test_from_iterator() {
        let r: Rights = [Right::Read, Right::Write, Right::Read]
            .into_iter()
            .collect();
        assert_eq!(r.len(), 2);
        assert!(r.contains(Right::Read));
        assert!(r.contains(Right::Write));

        let empty: Rights = std::iter::empty::<Right>().collect();
        assert_eq!(empty, Rights::empty());
    }

    // ---------------------------------------------------------------
    // 23. IntoIterator for Rights and &Rights
    // ---------------------------------------------------------------
    #[test]
    fn test_into_iterator() {
        let rw = Rights::from_slice(&[Right::Read, Right::Write]);

        // owned
        let mut count = 0;
        for _right in rw {
            count += 1;
        }
        assert_eq!(count, 2);

        // ref
        let mut count_ref = 0;
        for _right in &rw {
            count_ref += 1;
        }
        assert_eq!(count_ref, 2);
    }

    // ---------------------------------------------------------------
    // 24. Display
    // ---------------------------------------------------------------
    #[test]
    fn test_display() {
        assert_eq!(format!("{}", Rights::empty()), "{}");
        assert_eq!(format!("{}", Rights::singleton(Right::Read)), "{read}");
        let rw = Rights::from_slice(&[Right::Read, Right::Write]);
        assert_eq!(format!("{}", rw), "{read, write}");
    }

    // ---------------------------------------------------------------
    // 25. Right::name / short_name / Display / FromStr roundtrip
    // ---------------------------------------------------------------
    #[test]
    fn test_right_name_parse_roundtrip() {
        for &right in &Right::ALL {
            // name() parses back
            let parsed: Right = right.name().parse().unwrap();
            assert_eq!(parsed, right);

            // short_name() parses back
            let short: Right = right.short_name().to_string().parse().unwrap();
            assert_eq!(short, right);

            // Display roundtrips
            let displayed = format!("{}", right);
            let reparsed: Right = displayed.parse().unwrap();
            assert_eq!(reparsed, right);
        }

        // invalid string
        assert!("invalid".parse::<Right>().is_err());
    }

    // ---------------------------------------------------------------
    // 26. Convenience constructors
    // ---------------------------------------------------------------
    #[test]
    fn test_convenience_constructors() {
        let ro = Rights::read_only();
        assert_eq!(ro.len(), 1);
        assert!(ro.contains(Right::Read));

        let rw = Rights::read_write();
        assert_eq!(rw.len(), 2);
        assert!(rw.contains(Right::Read));
        assert!(rw.contains(Right::Write));

        let rwx = Rights::read_write_execute();
        assert_eq!(rwx.len(), 3);
        assert!(rwx.contains(Right::Read));
        assert!(rwx.contains(Right::Write));
        assert!(rwx.contains(Right::Execute));
    }

    // ---------------------------------------------------------------
    // 27. Algebraic laws (security-critical identities)
    // ---------------------------------------------------------------
    #[test]
    fn test_algebraic_laws() {
        let a = Rights::from_slice(&[Right::Read, Right::Write, Right::Execute]);
        let b = Rights::from_slice(&[Right::Write, Right::Execute, Right::Create]);
        let c = Rights::from_slice(&[Right::Execute, Right::Create, Right::Delete]);

        // Commutativity
        assert_eq!(a.union(&b), b.union(&a));
        assert_eq!(a.combine(&b), b.combine(&a));
        assert_eq!(a.symmetric_difference(&b), b.symmetric_difference(&a));

        // Associativity
        assert_eq!(a.union(&b).union(&c), a.union(&b.union(&c)));
        assert_eq!(a.combine(&b).combine(&c), a.combine(&b.combine(&c)));

        // Distributivity: a & (b | c) == (a & b) | (a & c)
        assert_eq!(a.combine(&b.union(&c)), a.combine(&b).union(&a.combine(&c)));

        // Absorption: a | (a & b) == a
        assert_eq!(a.union(&a.combine(&b)), a);

        // Absorption: a & (a | b) == a
        assert_eq!(a.combine(&a.union(&b)), a);

        // De Morgan: !(a & b) == !a | !b
        assert_eq!(
            a.combine(&b).complement(),
            a.complement().union(&b.complement())
        );

        // De Morgan: !(a | b) == !a & !b
        assert_eq!(
            a.union(&b).complement(),
            a.complement().combine(&b.complement())
        );

        // Idempotence
        assert_eq!(a.union(&a), a);
        assert_eq!(a.combine(&a), a);

        // Identity
        assert_eq!(a.union(&Rights::empty()), a);
        assert_eq!(a.combine(&Rights::all()), a);

        // Annihilation
        assert_eq!(a.combine(&Rights::empty()), Rights::empty());
        assert_eq!(a.union(&Rights::all()), Rights::all());

        // Complement
        assert_eq!(a.union(&a.complement()), Rights::all());
        assert_eq!(a.combine(&a.complement()), Rights::empty());
    }

    // ---------------------------------------------------------------
    // 28. Confinement property (security-critical)
    // ---------------------------------------------------------------
    #[test]
    fn test_confinement_property() {
        // The confinement property states that delegation via combine()
        // can NEVER increase rights. For all r1, r2:
        //   r1.combine(r2).is_subset_of(r1)
        //   r1.combine(r2).is_subset_of(r2)
        // This is the fundamental security guarantee of capability confinement.

        // Exhaustive over all 1024 x 1024 pairs
        for a_bits in 0u16..=0x03FF {
            let a = Rights::from_bits(a_bits);
            for b_bits in 0u16..=0x03FF {
                let b = Rights::from_bits(b_bits);
                let c = a.combine(&b);
                assert!(
                    c.is_subset_of(&a),
                    "confinement violated: combine({:#06x}, {:#06x}) = {:#06x} not subset of {:#06x}",
                    a_bits, b_bits, c.to_bits(), a_bits
                );
                assert!(
                    c.is_subset_of(&b),
                    "confinement violated: combine({:#06x}, {:#06x}) = {:#06x} not subset of {:#06x}",
                    a_bits, b_bits, c.to_bits(), b_bits
                );
            }
        }
    }

    // ---------------------------------------------------------------
    // 29. Hash consistency
    // ---------------------------------------------------------------
    #[test]
    fn test_hash_consistency() {
        use std::collections::hash_map::DefaultHasher;

        fn hash_of(r: &Rights) -> u64 {
            let mut h = DefaultHasher::new();
            r.hash(&mut h);
            h.finish()
        }

        // Equal values must have equal hashes
        let a = Rights::from_slice(&[Right::Read, Right::Write]);
        let b = Rights::from_bits(0x0003);
        assert_eq!(a, b);
        assert_eq!(hash_of(&a), hash_of(&b));

        // Different construction paths, same result
        let c: Rights = [Right::Write, Right::Read].into_iter().collect();
        assert_eq!(hash_of(&a), hash_of(&c));
    }
}
