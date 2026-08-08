// Copyright (C) 2026 HaiyangLi
// SPDX-License-Identifier: Apache-2.0
//! Thread Control Block and Scheduler State
//!
//! Corresponds to: Lion/State/Thread.lean, Lion/State/Scheduler.lean
//!
//! ## Design
//!
//! - No BTreeMap/HashMap (use Vec<(K,V)> patterns)
//! - No closures or dyn Trait
//! - No iterator combinators (use index-based loops)
//! - No && in while conditions (use nested if)
//! - Concrete types only (no generics with trait bounds)
//!
//! ## Lean Correspondence
//!
//! | Rust Type          | Lean Type              | Notes                    |
//! |--------------------|------------------------|--------------------------|
//! | ThreadState        | Lion.ThreadState       | 6-variant enum           |
//! | IPCBlockReason     | Lion.IPCBlockReason    | 5-variant enum           |
//! | FaultType          | Lion.FaultType         | 6-variant enum           |
//! | Registers          | Lion.Registers         | gpr[32] + pc + sp + flags|
//! | TCB                | Lion.TCB               | Thread control block     |
//! | DomainScheduleEntry| Lion.DomainScheduleEntry| domain + duration       |
//! | SchedulerAction    | Lion.SchedulerAction   | 3-variant enum           |
//! | SchedulerState     | Lion.SchedulerState    | Scheduler with queues    |

use crate::SecurityLevel;

/// Thread execution state
///
/// Corresponds to: Lion.ThreadState
#[derive(Clone, Copy, PartialEq, Eq)]
pub enum ThreadState {
    /// Not yet started or terminated
    Inactive,
    /// Currently executing on CPU
    Running,
    /// Runnable, waiting in ready queue
    Ready,
    /// Blocked on IPC/fault/notification
    Blocked,
    /// About to be restarted
    Restart,
    /// Special idle thread state
    IdleThread,
}

/// IPC blocking reason
///
/// Corresponds to: Lion.IPCBlockReason
#[derive(Clone, Copy, PartialEq, Eq)]
pub enum IPCBlockReason {
    /// Blocked waiting to send on endpoint
    SendBlocked(u64),
    /// Blocked waiting to receive from endpoint
    RecvBlocked(u64),
    /// Blocked waiting for reply
    ReplyBlocked,
    /// Blocked on notification
    NotificationBlocked(u64),
    /// Blocked on call (send + recv)
    CallBlocked(u64),
}

/// Fault type for exception handling
///
/// Corresponds to: Lion.FaultType
#[derive(Clone, Copy, PartialEq, Eq)]
pub enum FaultType {
    /// Virtual memory fault
    VmFault {
        /// Faulting memory address
        addr: u64,
        /// Whether the fault was a write attempt
        is_write: bool,
    },
    /// Capability fault
    CapFault(u64),
    /// Unknown syscall
    UnknownSyscall(u64),
    /// User-level exception
    UserException {
        /// Exception number
        num: u64,
        /// Error code associated with the exception
        err_code: u64,
    },
    /// Debug trap
    DebugException,
    /// No fault (placeholder)
    NullFault,
}

/// Processor register state
///
/// Corresponds to: Lion.Registers
///
/// Fixed-size arrays cause universe constraint issues in Lean extraction.
/// The length invariant (32 registers) is maintained at the spec level.
pub struct Registers {
    /// General purpose registers (should have 32 entries)
    pub gpr: Vec<u64>,
    /// Program counter
    pub pc: u64,
    /// Stack pointer
    pub sp: u64,
    /// Flags/status register
    pub flags: u64,
}

/// Thread Control Block
///
/// Corresponds to: Lion.TCB
///
/// Contains all state for a schedulable thread of execution.
/// Thread operations are capability-mediated.
pub struct TCB {
    /// Unique thread identifier
    pub id: u64,
    /// Current execution state (named thread_state to avoid Lean field collision)
    pub thread_state: ThreadState,
    /// Thread priority (0-255, higher = more priority)
    pub priority: u64,
    /// Maximum control priority (limits priority changes)
    pub mcp: u64,
    /// Remaining time slice in ticks
    pub timeslice: u64,
    /// Scheduling domain membership
    pub domain: u64,
    /// Bound CSpace root capability (if any)
    pub bound_cspace: Option<u64>,
    /// Bound VSpace identifier (if any)
    pub bound_vspace: Option<u64>,
    /// IPC buffer address in thread's address space
    pub ipc_buffer: Option<u64>,
    /// Stored reply capability for seL4-style IPC
    pub reply_slot: Option<u64>,
    /// Fault handler endpoint
    pub fault_endpoint: Option<u64>,
    /// Current blocking reason (if blocked)
    pub blocked: Option<IPCBlockReason>,
    /// Pending fault (if any)
    pub fault: Option<FaultType>,
    /// Saved register state
    pub registers: Registers,
    /// Security level for information flow
    pub security_level: SecurityLevel,
}

/// Domain schedule entry: time allocation for a domain
///
/// Corresponds to: Lion.DomainScheduleEntry
#[derive(Clone, Copy, PartialEq, Eq)]
pub struct DomainScheduleEntry {
    /// Domain identifier
    pub domain: u64,
    /// Duration in ticks
    pub duration: u64,
}

/// Pending scheduler action
///
/// Corresponds to: Lion.SchedulerAction
#[derive(Clone, Copy, PartialEq, Eq)]
pub enum SchedulerAction {
    /// Continue executing current thread
    ResumeCurrentThread,
    /// Pick highest priority ready thread
    ChooseNewThread,
    /// Switch to specific thread
    SwitchToThread(u64),
}

/// Why did we enter the kernel?
///
/// Corresponds to: Lion.KernelEntryReason
#[derive(Clone, Copy, PartialEq, Eq)]
pub enum KernelEntryReason {
    /// Thread issued a system call
    Syscall,
    /// Hardware interrupt received
    Interrupt,
    /// Virtual memory fault during execution
    VmFault,
    /// Processor exception occurred
    Exception,
}

/// Ready queue entry: (domain, priority, thread_id)
///
/// Represents a thread in a ready queue.
///
/// In Lean, readyQueues is `DomainId → Priority → List ThreadId`.
/// In Rust, we flatten to `Vec<ReadyQueueEntry>` for extractability.
#[derive(Clone, Copy, PartialEq, Eq)]
pub struct ReadyQueueEntry {
    /// Scheduling domain
    pub domain: u64,
    /// Thread priority
    pub priority: u64,
    /// Thread identifier
    pub thread_id: u64,
}

/// Release queue entry: (thread_id, wake_time)
///
/// Threads with delayed wake, sorted by wake time.
#[derive(Clone, Copy, PartialEq, Eq)]
pub struct ReleaseQueueEntry {
    /// Thread identifier
    pub thread_id: u64,
    /// Wake time (tick count)
    pub wake_time: u64,
}

/// Idle thread mapping entry: (domain, idle_thread_id)
#[derive(Clone, Copy, PartialEq, Eq)]
pub struct IdleThreadEntry {
    /// Domain identifier
    pub domain: u64,
    /// Idle thread identifier
    pub idle_thread_id: u64,
}

/// Scheduler state
///
/// Corresponds to: Lion.SchedulerState
///
/// - Lean `readyQueues : DomainId → Priority → List ThreadId`
///   → Rust `ready_queues : Vec<ReadyQueueEntry>` (flat representation)
/// - Lean `idleThreads : DomainId → ThreadId`
///   → Rust `idle_threads : Vec<IdleThreadEntry>`
pub struct SchedulerState {
    /// Currently running thread (if any)
    pub current_thread: Option<u64>,
    /// Current scheduling domain
    pub current_domain: u64,
    /// Remaining ticks in current domain
    pub domain_ticks_left: u64,
    /// Domain schedule (cyclic list)
    pub domain_schedule: Vec<DomainScheduleEntry>,
    /// Current position in domain schedule
    pub domain_schedule_idx: u64,
    /// Ready queues (flat: domain × priority × thread_id)
    pub ready_queues: Vec<ReadyQueueEntry>,
    /// Release queue: threads with delayed wake
    pub release_queue: Vec<ReleaseQueueEntry>,
    /// Pending scheduler action
    pub scheduler_action: SchedulerAction,
    /// Idle thread for each domain
    pub idle_threads: Vec<IdleThreadEntry>,
    /// Preemption pending flag
    pub preemption_pending: bool,
    /// Kernel entry reason tracking
    pub kernel_entry_reason: Option<KernelEntryReason>,
}

/// Thread table entry: (thread_id, tcb)
///
/// Lean uses `ThreadTable = ThreadId → TCB` (function type).
pub struct ThreadTableEntry {
    /// Thread identifier
    pub thread_id: u64,
    /// Thread control block
    pub tcb: TCB,
}

/// Thread table
///
/// Corresponds to: Lion.ThreadTable
pub struct ThreadTable {
    /// Entries (thread_id → TCB)
    pub entries: Vec<ThreadTableEntry>,
}
