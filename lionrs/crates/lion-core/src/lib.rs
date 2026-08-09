// Copyright (C) 2026 HaiyangLi
// SPDX-License-Identifier: Apache-2.0
//! Lion Core -- production microkernel types, state machine, and kernel API.
//!
//! This crate provides the canonical Rust implementation of the Lion microkernel.
//!
//! # Features
//!
//! - `serde` -- Enable serde Serialize/Deserialize on all types

#![deny(unsafe_code)]
#![warn(missing_docs)]
#![allow(clippy::all)]

pub(crate) mod crypto;
pub mod error;
pub mod kernel;
pub mod state;
pub mod step;
pub mod types;

// Extraction anchor module — only compiled when extracting to Lean via styx-rustc.
// This module defines what types get translated via reachability from the anchor function.
#[cfg(extract)]
pub mod extract_anchor;

// Re-export unified Error and Kernel at crate root
pub use error::Error;
pub use kernel::Kernel;

// Re-export commonly used types at crate root
pub use types::{
    // Policy
    Action,
    // Identifiers
    ActorId,
    // Capability/Crypto
    Blake3Hash,
    CapId,
    CapPayload,
    Capability,
    CapabilityError,
    DomainId,
    Hash32,
    Key,
    LogEvent,
    MemAddr,
    // Runtime
    MemRegion,
    MsgId,
    MsgState,
    // Parse errors
    ParseMemRegionError,
    ParseMsgStateError,
    ParsePolicyDecisionError,
    ParseRightError,
    ParseSecurityLevelError,
    PluginId,
    PolicyContext,
    PolicyDecision,
    PolicyDecisionFn,
    PolicyError,
    PolicyState,
    ResourceId,
    // Rights
    Right,
    Rights,
    RightsError,
    RuntimeTag,
    SealedTag,
    // Security
    SecurityLevel,
    Size,
    SymbolicTag,
    // Thread / Scheduler
    ThreadId,
    Time,
    WorkflowId,
};

// Re-export step module types
pub use step::{
    AuthorizationError,
    // Authorization
    Authorized,
    // Host calls
    HostCall,
    HostCallPrecondition,
    HostFunction,
    HostResult,
    // Step enum and error
    InvalidTransitionReason,
    // Kernel operations
    KernelOp,
    KernelOpError,
    // Plugin internal
    PluginInternal,
    PluginPrecondition,
    Step,
    StepError,
};
