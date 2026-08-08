// Copyright (C) 2026 HaiyangLi
// SPDX-License-Identifier: Apache-2.0
//! Lion Core Identifiers
//!
//! Core identifier types for Lion microkernel.
//! Identity types (plugin, actor, resource, cap, workflow, message, thread, domain)
//! use u128 for global uniqueness. Physical quantities (time, size, address) use u64.

/// Plugin identifier (WASM module instance)
pub type PluginId = u128;

/// Actor identifier (concurrent execution context)
pub type ActorId = u128;

/// Resource identifier (kernel-managed objects)
pub type ResourceId = u128;

/// Capability identifier
pub type CapId = u128;

/// Workflow instance identifier
pub type WorkflowId = u128;

/// Message identifier
pub type MsgId = u128;

/// Logical time (monotonic counter)
pub type Time = u64;

/// Memory size in bytes
pub type Size = u64;

/// Memory address (linear memory offset)
pub type MemAddr = u64;

/// Thread identifier (seL4-style TCB reference)
pub type ThreadId = u128;

/// Scheduling domain identifier
pub type DomainId = u128;
