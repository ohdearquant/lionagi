// Copyright (C) 2026 HaiyangLi
// SPDX-License-Identifier: Apache-2.0
//! Lion Core Runtime Types
//!
//! Corresponds to: Lion/Core/RuntimeTrustBundleCore.lean
//!
//! Runtime isolation and message delivery types.

use super::PluginId;

/// Memory region classification for isolation proofs
///
/// Corresponds to Lean: `inductive MemRegion`
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum MemRegion {
    /// Kernel's protected memory region
    Kernel,
    /// Plugin's linear memory region
    Plugin(PluginId),
}

impl MemRegion {
    /// Kernel memory region.
    pub const KERNEL: Self = MemRegion::Kernel;

    /// Plugin memory region with given ID.
    #[inline]
    pub const fn plugin(id: u128) -> Self {
        MemRegion::Plugin(id)
    }

    /// Check if this is the kernel region
    #[inline]
    pub const fn is_kernel(&self) -> bool {
        matches!(self, MemRegion::Kernel)
    }

    /// Check if this is a plugin region
    #[inline]
    pub const fn is_plugin(&self) -> bool {
        matches!(self, MemRegion::Plugin(_))
    }

    /// Get the plugin ID if this is a plugin region
    #[inline]
    pub const fn plugin_id(&self) -> Option<PluginId> {
        match self {
            MemRegion::Plugin(pid) => Some(*pid),
            MemRegion::Kernel => None,
        }
    }
}

impl std::fmt::Display for MemRegion {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            MemRegion::Kernel => write!(f, "kernel"),
            MemRegion::Plugin(id) => write!(f, "plugin:{id}"),
        }
    }
}

/// Error parsing a MemRegion from string.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ParseMemRegionError {
    input: String,
}

impl std::fmt::Display for ParseMemRegionError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(
            f,
            "invalid mem region: '{}' (expected \"kernel\" or \"plugin:ID\")",
            self.input
        )
    }
}

impl std::error::Error for ParseMemRegionError {}

impl std::str::FromStr for MemRegion {
    type Err = ParseMemRegionError;

    fn from_str(s: &str) -> Result<Self, Self::Err> {
        let lower = s.trim().to_lowercase();
        if lower == "kernel" {
            return Ok(MemRegion::Kernel);
        }
        if let Some(id_str) = lower.strip_prefix("plugin:") {
            if let Ok(id) = id_str.parse::<u128>() {
                return Ok(MemRegion::Plugin(id));
            }
        }
        Err(ParseMemRegionError {
            input: s.to_string(),
        })
    }
}

#[cfg(feature = "serde")]
impl serde::Serialize for MemRegion {
    fn serialize<S: serde::Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        match self {
            MemRegion::Kernel => serializer.serialize_str("kernel"),
            MemRegion::Plugin(id) => {
                use serde::ser::SerializeStruct;
                let mut s = serializer.serialize_struct("MemRegion", 1)?;
                s.serialize_field("plugin", id)?;
                s.end()
            }
        }
    }
}

#[cfg(feature = "serde")]
impl<'de> serde::Deserialize<'de> for MemRegion {
    fn deserialize<D: serde::Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        use serde::de::{self, MapAccess, Visitor};

        struct MemRegionVisitor;

        impl<'de> Visitor<'de> for MemRegionVisitor {
            type Value = MemRegion;

            fn expecting(&self, formatter: &mut std::fmt::Formatter) -> std::fmt::Result {
                formatter.write_str("\"kernel\" or {\"plugin\": u128}")
            }

            fn visit_str<E: de::Error>(self, v: &str) -> Result<Self::Value, E> {
                if v == "kernel" {
                    Ok(MemRegion::KERNEL)
                } else {
                    Err(E::custom(format!("invalid mem region: {}", v)))
                }
            }

            fn visit_map<M: MapAccess<'de>>(self, mut map: M) -> Result<Self::Value, M::Error> {
                let key: String = map
                    .next_key()?
                    .ok_or_else(|| de::Error::missing_field("plugin"))?;
                if key == "plugin" {
                    let id: u128 = map.next_value()?;
                    Ok(MemRegion::plugin(id))
                } else {
                    Err(de::Error::unknown_field(&key, &["plugin"]))
                }
            }
        }

        deserializer.deserialize_any(MemRegionVisitor)
    }
}

/// Message delivery states
///
/// Corresponds to Lean: `inductive MsgState`
///
/// Tracks the lifecycle of a message through the system:
/// Sent -> Queued -> Delivered (or Dropped)
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Default)]
pub enum MsgState {
    /// Message created, in sender's pending queue
    #[default]
    Sent,
    /// Message in router, waiting for recipient's mailbox
    Queued,
    /// Message consumed by recipient
    Delivered,
    /// Message explicitly dropped (e.g., mailbox full)
    Dropped,
}

impl MsgState {
    /// All message states.
    pub const SENT: Self = MsgState::Sent;
    /// Queued message state.
    pub const QUEUED: Self = MsgState::Queued;
    /// Delivered message state.
    pub const DELIVERED: Self = MsgState::Delivered;
    /// Dropped message state.
    pub const DROPPED: Self = MsgState::Dropped;

    /// Get the name of this state.
    #[inline]
    pub const fn name(self) -> &'static str {
        match self {
            MsgState::Sent => "sent",
            MsgState::Queued => "queued",
            MsgState::Delivered => "delivered",
            MsgState::Dropped => "dropped",
        }
    }

    /// Check if the message is still in transit
    #[inline]
    pub const fn is_in_transit(&self) -> bool {
        matches!(self, MsgState::Sent | MsgState::Queued)
    }

    /// Check if the message has been delivered
    #[inline]
    pub const fn is_delivered(&self) -> bool {
        matches!(self, MsgState::Delivered)
    }

    /// Check if the message was dropped
    #[inline]
    pub const fn is_dropped(&self) -> bool {
        matches!(self, MsgState::Dropped)
    }

    /// Check if the message reached a terminal state
    #[inline]
    pub const fn is_terminal(&self) -> bool {
        matches!(self, MsgState::Delivered | MsgState::Dropped)
    }
}

impl std::fmt::Display for MsgState {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.name())
    }
}

/// Error parsing a MsgState from string.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ParseMsgStateError {
    input: String,
}

impl std::fmt::Display for ParseMsgStateError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "invalid message state: '{}'", self.input)
    }
}

impl std::error::Error for ParseMsgStateError {}

impl std::str::FromStr for MsgState {
    type Err = ParseMsgStateError;

    fn from_str(s: &str) -> Result<Self, Self::Err> {
        match s.trim().to_lowercase().as_str() {
            "sent" => Ok(MsgState::Sent),
            "queued" => Ok(MsgState::Queued),
            "delivered" => Ok(MsgState::Delivered),
            "dropped" => Ok(MsgState::Dropped),
            _ => Err(ParseMsgStateError {
                input: s.to_string(),
            }),
        }
    }
}

#[cfg(feature = "serde")]
impl serde::Serialize for MsgState {
    fn serialize<S: serde::Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        serializer.serialize_str(self.name())
    }
}

#[cfg(feature = "serde")]
impl<'de> serde::Deserialize<'de> for MsgState {
    fn deserialize<D: serde::Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        let s = String::deserialize(deserializer)?;
        s.parse().map_err(serde::de::Error::custom)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_mem_region_kernel() {
        let region = MemRegion::Kernel;
        assert!(region.is_kernel());
        assert!(!region.is_plugin());
        assert_eq!(region.plugin_id(), None);
    }

    #[test]
    fn test_mem_region_plugin() {
        let region = MemRegion::Plugin(42);
        assert!(!region.is_kernel());
        assert!(region.is_plugin());
        assert_eq!(region.plugin_id(), Some(42));
    }

    #[test]
    fn test_msg_state_transitions() {
        // Sent is in transit
        assert!(MsgState::Sent.is_in_transit());
        assert!(!MsgState::Sent.is_terminal());

        // Queued is in transit
        assert!(MsgState::Queued.is_in_transit());
        assert!(!MsgState::Queued.is_terminal());

        // Delivered is terminal
        assert!(!MsgState::Delivered.is_in_transit());
        assert!(MsgState::Delivered.is_terminal());
        assert!(MsgState::Delivered.is_delivered());

        // Dropped is terminal
        assert!(!MsgState::Dropped.is_in_transit());
        assert!(MsgState::Dropped.is_terminal());
        assert!(MsgState::Dropped.is_dropped());
    }

    #[test]
    fn test_mem_region_from_str() {
        assert_eq!("kernel".parse::<MemRegion>().unwrap(), MemRegion::Kernel);
        assert_eq!("KERNEL".parse::<MemRegion>().unwrap(), MemRegion::Kernel);
        assert_eq!(
            "plugin:42".parse::<MemRegion>().unwrap(),
            MemRegion::Plugin(42)
        );
        assert_eq!(
            "Plugin:0".parse::<MemRegion>().unwrap(),
            MemRegion::Plugin(0)
        );
        assert!("plugin:".parse::<MemRegion>().is_err());
        assert!("plugin:abc".parse::<MemRegion>().is_err());
        assert!("invalid".parse::<MemRegion>().is_err());
    }

    #[test]
    fn test_mem_region_from_str_whitespace() {
        assert_eq!(" kernel ".parse::<MemRegion>().unwrap(), MemRegion::Kernel);
        assert_eq!(
            " plugin:1 ".parse::<MemRegion>().unwrap(),
            MemRegion::Plugin(1)
        );
    }

    #[test]
    fn test_msg_state_from_str() {
        assert_eq!("sent".parse::<MsgState>().unwrap(), MsgState::Sent);
        assert_eq!("QUEUED".parse::<MsgState>().unwrap(), MsgState::Queued);
        assert_eq!(
            "Delivered".parse::<MsgState>().unwrap(),
            MsgState::Delivered
        );
        assert_eq!("dropped".parse::<MsgState>().unwrap(), MsgState::Dropped);
        assert!("invalid".parse::<MsgState>().is_err());
    }

    #[test]
    fn test_msg_state_from_str_whitespace() {
        assert_eq!(" sent ".parse::<MsgState>().unwrap(), MsgState::Sent);
        assert_eq!("queued\n".parse::<MsgState>().unwrap(), MsgState::Queued);
    }
}
