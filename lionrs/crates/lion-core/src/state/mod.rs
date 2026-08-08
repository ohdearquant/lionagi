// Copyright (C) 2026 HaiyangLi
// SPDX-License-Identifier: Apache-2.0
//! Lion State Module
//!
//! Complete state machine for the Lion microkernel.

mod actor;
mod kernel;
mod memory;
mod plugin;
#[allow(clippy::module_inception)]
mod state;
mod thread;
mod workflow;

pub use memory::{LinearMemory, MemoryError, MetaState, ResourceStatus};

pub use kernel::{
    kernel_dispatch, CapTable, ChildrenIndex, KernelError, KernelRequest, KernelResult,
    KernelState, KernelStateCore, KeyState, PluginMeta, ResourceMeta, RevocationState,
};

pub use plugin::{PluginError, PluginLocal, PluginState};

pub use actor::{ActorError, ActorRuntime, Message, Queue};

pub use workflow::{
    Edge, NodeState, NodeStateEntry, RetryEntry, WorkflowDef, WorkflowError, WorkflowInstance,
    WorkflowStatus,
};

pub use thread::{
    DomainScheduleEntry, FaultType, IPCBlockReason, IdleThreadEntry, KernelEntryReason,
    ReadyQueueEntry, Registers, ReleaseQueueEntry, SchedulerAction, SchedulerState, ThreadState,
    ThreadTable, ThreadTableEntry, TCB,
};

pub use state::{ResourceInfo, State, StateError};
