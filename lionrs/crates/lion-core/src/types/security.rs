// Copyright (C) 2026 HaiyangLi
// SPDX-License-Identifier: Apache-2.0
//! Security classification levels forming a lattice.
//!
//! 4-level lattice: Public < Internal < Confidential < Secret.
//! Information can only flow from lower to higher levels (no leakage).

use std::fmt;
use std::str::FromStr;

/// Security classification levels forming a lattice.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
#[must_use = "security levels must be checked or applied"]
pub enum SecurityLevel {
    /// Publicly accessible data
    Public,
    /// Internal use only (within organization)
    Internal,
    /// Confidential data (restricted access)
    Confidential,
    /// Secret data (highest classification)
    Secret,
}

impl SecurityLevel {
    /// Convert security level to numeric value for ordering.
    #[inline]
    pub const fn to_u8(self) -> u8 {
        match self {
            SecurityLevel::Public => 0,
            SecurityLevel::Internal => 1,
            SecurityLevel::Confidential => 2,
            SecurityLevel::Secret => 3,
        }
    }

    /// Try to convert from u8.
    pub const fn from_u8(value: u8) -> Option<Self> {
        match value {
            0 => Some(SecurityLevel::Public),
            1 => Some(SecurityLevel::Internal),
            2 => Some(SecurityLevel::Confidential),
            3 => Some(SecurityLevel::Secret),
            _ => None,
        }
    }

    /// Information flow relation: data at `self` can flow to `other`.
    #[inline]
    pub const fn flows_to(self, other: SecurityLevel) -> bool {
        self.to_u8() <= other.to_u8()
    }

    /// Check if information can flow in either direction.
    pub const fn can_communicate(self, other: SecurityLevel) -> bool {
        self.flows_to(other) || other.flows_to(self)
    }

    /// Least upper bound (join) -- the higher of two levels.
    #[inline]
    pub const fn join(self, other: SecurityLevel) -> SecurityLevel {
        if self.to_u8() >= other.to_u8() {
            self
        } else {
            other
        }
    }

    /// Greatest lower bound (meet) -- the lower of two levels.
    #[inline]
    pub const fn meet(self, other: SecurityLevel) -> SecurityLevel {
        if self.to_u8() <= other.to_u8() {
            self
        } else {
            other
        }
    }

    /// Top element of the lattice.
    pub const TOP: SecurityLevel = SecurityLevel::Secret;

    /// Bottom element of the lattice.
    pub const BOT: SecurityLevel = SecurityLevel::Public;

    /// Get the top (highest) security level.
    #[inline]
    pub const fn top() -> SecurityLevel {
        Self::TOP
    }

    /// Get the bottom (lowest) security level.
    #[inline]
    pub const fn bot() -> SecurityLevel {
        Self::BOT
    }

    /// Check if this is the top level.
    #[inline]
    pub const fn is_top(self) -> bool {
        matches!(self, SecurityLevel::Secret)
    }

    /// Check if this is the bottom level.
    #[inline]
    pub const fn is_bot(self) -> bool {
        matches!(self, SecurityLevel::Public)
    }

    /// All levels from lowest to highest.
    pub const ALL: [SecurityLevel; 4] = [
        SecurityLevel::Public,
        SecurityLevel::Internal,
        SecurityLevel::Confidential,
        SecurityLevel::Secret,
    ];

    /// Get the name of this level.
    #[inline]
    pub const fn name(self) -> &'static str {
        match self {
            SecurityLevel::Public => "public",
            SecurityLevel::Internal => "internal",
            SecurityLevel::Confidential => "confidential",
            SecurityLevel::Secret => "secret",
        }
    }
}

impl fmt::Display for SecurityLevel {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.name())
    }
}

/// Error parsing a SecurityLevel from string.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ParseSecurityLevelError {
    input: String,
}

impl fmt::Display for ParseSecurityLevelError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "invalid security level: '{}'", self.input)
    }
}

impl std::error::Error for ParseSecurityLevelError {}

impl FromStr for SecurityLevel {
    type Err = ParseSecurityLevelError;

    fn from_str(s: &str) -> Result<Self, Self::Err> {
        match s.to_lowercase().as_str() {
            "public" | "0" => Ok(SecurityLevel::Public),
            "internal" | "1" => Ok(SecurityLevel::Internal),
            "confidential" | "2" => Ok(SecurityLevel::Confidential),
            "secret" | "3" => Ok(SecurityLevel::Secret),
            _ => Err(ParseSecurityLevelError {
                input: s.to_string(),
            }),
        }
    }
}

#[cfg(feature = "serde")]
impl serde::Serialize for SecurityLevel {
    fn serialize<S: serde::Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        serializer.serialize_str(self.name())
    }
}

#[cfg(feature = "serde")]
impl<'de> serde::Deserialize<'de> for SecurityLevel {
    fn deserialize<D: serde::Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        let s = String::deserialize(deserializer)?;
        s.parse().map_err(serde::de::Error::custom)
    }
}

impl PartialOrd for SecurityLevel {
    #[inline]
    fn partial_cmp(&self, other: &Self) -> Option<std::cmp::Ordering> {
        Some(self.cmp(other))
    }
}

impl Ord for SecurityLevel {
    #[inline]
    fn cmp(&self, other: &Self) -> std::cmp::Ordering {
        self.to_u8().cmp(&other.to_u8())
    }
}

impl Default for SecurityLevel {
    fn default() -> Self {
        SecurityLevel::Public
    }
}
