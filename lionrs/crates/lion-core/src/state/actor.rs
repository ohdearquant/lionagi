// Copyright (C) 2026 HaiyangLi
// SPDX-License-Identifier: Apache-2.0
//! Lion State Actor
//!
//! Corresponds to: Lion/State/Actor.lean
//!
//! Actor runtime for message-passing concurrency.

use std::collections::VecDeque;

use crate::types::{ActorId, MsgId, PluginId, SecurityLevel};

/// Message queue (FIFO)
///
/// Corresponds to Lean: `abbrev Queue (a : Type) := List a`
///
/// Using VecDeque for O(1) push_back / pop_front.
pub type Queue<T> = VecDeque<T>;

/// Error type for actor operations
#[derive(Debug)]
pub enum ActorError {
    /// Mailbox is full (capacity, current)
    MailboxFull(usize, usize),
    /// No message available
    NoMessage,
    /// Actor is blocked
    Blocked(ActorId),
}

/// Opaque message payload
///
/// Corresponds to Lean: `opaque Data : Type`
///
/// In Rust, we use a Vec<u8> as the concrete representation.
pub type Data = Vec<u8>;

/// Message between actors
///
/// Corresponds to Lean: `structure Message`
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Message {
    /// Message identifier
    ///
    /// Corresponds to Lean: `id : MsgId`
    pub(crate) id: MsgId,

    /// Source plugin
    ///
    /// Corresponds to Lean: `src : PluginId`
    pub(crate) src: PluginId,

    /// Destination plugin
    ///
    /// Corresponds to Lean: `dst : PluginId`
    pub(crate) dst: PluginId,

    /// Security level of the message
    ///
    /// Corresponds to Lean: `level : SecurityLevel`
    pub(crate) level: SecurityLevel,

    /// Message body
    ///
    /// Corresponds to Lean: `body : Data`
    pub(crate) body: Data,
}

impl Message {
    /// Create a new message
    pub fn new(id: MsgId, src: PluginId, dst: PluginId, level: SecurityLevel, body: Data) -> Self {
        Message {
            id,
            src,
            dst,
            level,
            body,
        }
    }

    /// Get the message ID
    #[inline]
    pub fn id(&self) -> MsgId {
        self.id
    }

    /// Get the source plugin
    #[inline]
    pub fn src(&self) -> PluginId {
        self.src
    }

    /// Get the destination plugin
    #[inline]
    pub fn dst(&self) -> PluginId {
        self.dst
    }

    /// Get the security level
    #[inline]
    pub fn level(&self) -> SecurityLevel {
        self.level
    }

    /// Get the message body
    #[inline]
    pub fn body(&self) -> &Data {
        &self.body
    }

    /// Consume the message and return the body
    pub fn into_body(self) -> Data {
        self.body
    }
}

/// Runtime state of an actor
///
/// Corresponds to Lean: `structure ActorRuntime`
#[derive(Debug, Clone)]
pub struct ActorRuntime {
    /// Router queue (in transit)
    ///
    /// Corresponds to Lean: `pending : Queue Message`
    pub(crate) pending: Queue<Message>,

    /// Delivered, ready to process
    ///
    /// Corresponds to Lean: `mailbox : Queue Message`
    pub(crate) mailbox: Queue<Message>,

    /// Mailbox capacity (bounded)
    ///
    /// Corresponds to Lean: `capacity : Nat`
    pub(crate) capacity: usize,

    /// Wait-for graph edge (for deadlock analysis)
    ///
    /// Corresponds to Lean: `blockedOn : Option ActorId`
    pub(crate) blocked_on: Option<ActorId>,

    /// Message waiting to be delivered
    ///
    /// Corresponds to Lean: `pendingSend : Option Message`
    pub(crate) pending_send: Option<Message>,
}

impl ActorRuntime {
    /// Create empty actor runtime
    ///
    /// Corresponds to Lean: `def ActorRuntime.empty (capacity : Nat) : ActorRuntime`
    pub fn empty(capacity: usize) -> Self {
        ActorRuntime {
            pending: VecDeque::new(),
            mailbox: VecDeque::new(),
            capacity,
            blocked_on: None,
            pending_send: None,
        }
    }

    /// Check if actor can receive (mailbox not empty)
    ///
    /// Corresponds to Lean: `def ActorRuntime.can_receive (ar : ActorRuntime) : Bool`
    #[inline]
    pub fn can_receive(&self) -> bool {
        !self.mailbox.is_empty()
    }

    /// Check if actor can send (pending queue not full)
    ///
    /// Corresponds to Lean: `def ActorRuntime.can_send (ar : ActorRuntime) (targetCapacity : Nat) : Bool`
    #[inline]
    pub fn can_send(&self, target_capacity: usize) -> bool {
        self.pending.len() < target_capacity
    }

    /// Check if mailbox has space
    ///
    /// Corresponds to Lean: `def ActorRuntime.mailbox_has_space (ar : ActorRuntime) : Bool`
    #[inline]
    pub fn mailbox_has_space(&self) -> bool {
        self.mailbox.len() < self.capacity
    }

    /// Enqueue message to pending
    ///
    /// Corresponds to Lean: `def ActorRuntime.enqueue_pending`
    pub fn enqueue_pending(&self, msg: Message) -> Self {
        let mut new_pending = self.pending.clone();
        new_pending.push_back(msg);
        ActorRuntime {
            pending: new_pending,
            mailbox: self.mailbox.clone(),
            capacity: self.capacity,
            blocked_on: self.blocked_on,
            pending_send: self.pending_send.clone(),
        }
    }

    /// Enqueue message to pending (mutating version)
    pub fn enqueue_pending_mut(&mut self, msg: Message) {
        self.pending.push_back(msg);
    }

    /// Deliver message from pending to mailbox
    ///
    /// Corresponds to Lean: `def ActorRuntime.deliver`
    ///
    /// Returns the new state. If pending is empty or mailbox is full,
    /// returns unchanged state.
    pub fn deliver(&self) -> Self {
        // Check if pending is empty
        if self.pending.is_empty() {
            return self.clone();
        }
        // Check if mailbox is full
        if self.mailbox.len() >= self.capacity {
            return self.clone();
        }

        // Dequeue from front of pending, push to mailbox
        let mut new_pending = self.pending.clone();
        let first_msg = new_pending.pop_front().expect("checked non-empty");
        let mut new_mailbox = self.mailbox.clone();
        new_mailbox.push_back(first_msg);

        ActorRuntime {
            pending: new_pending,
            mailbox: new_mailbox,
            capacity: self.capacity,
            blocked_on: self.blocked_on,
            pending_send: self.pending_send.clone(),
        }
    }

    /// Deliver message from pending to mailbox (mutating version)
    ///
    /// Returns Ok(true) if delivered, Ok(false) if mailbox full, Err if no pending.
    ///
    /// # Errors
    ///
    /// Returns `ActorError::NoMessage` if the pending queue is empty.
    pub fn deliver_mut(&mut self) -> Result<bool, ActorError> {
        if self.pending.is_empty() {
            return Err(ActorError::NoMessage);
        }

        if self.mailbox.len() < self.capacity {
            let msg = self.pending.pop_front().expect("checked non-empty");
            self.mailbox.push_back(msg);
            Ok(true)
        } else {
            // Mailbox full, don't modify
            Ok(false)
        }
    }

    /// Consume message from mailbox
    ///
    /// Corresponds to Lean: `def ActorRuntime.consume`
    ///
    /// Returns `(new_state, Option<Message>)`.
    pub fn consume(&self) -> (Self, Option<Message>) {
        if self.mailbox.is_empty() {
            return (self.clone(), None);
        }

        // Get first message
        let mut new_mailbox = self.mailbox.clone();
        let msg = new_mailbox.pop_front().expect("checked non-empty");

        (
            ActorRuntime {
                pending: self.pending.clone(),
                mailbox: new_mailbox,
                capacity: self.capacity,
                blocked_on: self.blocked_on,
                pending_send: self.pending_send.clone(),
            },
            Some(msg),
        )
    }

    /// Consume message from mailbox (mutating version)
    pub fn consume_mut(&mut self) -> Option<Message> {
        self.mailbox.pop_front()
    }

    /// Set blocked state (for deadlock analysis)
    ///
    /// Corresponds to Lean: `def ActorRuntime.set_blocked`
    pub fn set_blocked(&self, on: ActorId) -> Self {
        ActorRuntime {
            pending: self.pending.clone(),
            mailbox: self.mailbox.clone(),
            capacity: self.capacity,
            blocked_on: Some(on),
            pending_send: self.pending_send.clone(),
        }
    }

    /// Set blocked state (mutating version)
    pub fn set_blocked_mut(&mut self, on: ActorId) {
        self.blocked_on = Some(on);
    }

    /// Clear blocked state
    ///
    /// Corresponds to Lean: `def ActorRuntime.unblock`
    pub fn unblock(&self) -> Self {
        ActorRuntime {
            pending: self.pending.clone(),
            mailbox: self.mailbox.clone(),
            capacity: self.capacity,
            blocked_on: None,
            pending_send: self.pending_send.clone(),
        }
    }

    /// Clear blocked state (mutating version)
    pub fn unblock_mut(&mut self) {
        self.blocked_on = None;
    }

    /// Get the blocked-on actor (if any)
    #[inline]
    pub fn blocked_on(&self) -> Option<ActorId> {
        self.blocked_on
    }

    /// Check if actor is blocked
    #[inline]
    pub fn is_blocked(&self) -> bool {
        self.blocked_on.is_some()
    }

    /// Get the capacity
    #[inline]
    pub fn capacity(&self) -> usize {
        self.capacity
    }

    /// Get the pending queue length
    #[inline]
    pub fn pending_len(&self) -> usize {
        self.pending.len()
    }

    /// Get the mailbox length
    #[inline]
    pub fn mailbox_len(&self) -> usize {
        self.mailbox.len()
    }

    /// Get the pending send message
    #[inline]
    pub fn pending_send(&self) -> Option<&Message> {
        self.pending_send.as_ref()
    }

    /// Set the pending send message
    pub fn set_pending_send(&mut self, msg: Option<Message>) {
        self.pending_send = msg;
    }
}

impl Default for ActorRuntime {
    fn default() -> Self {
        ActorRuntime::empty(0)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn make_test_message(id: MsgId) -> Message {
        Message::new(id, 1, 2, SecurityLevel::Public, vec![1, 2, 3])
    }

    #[test]
    fn test_actor_runtime_empty() {
        let ar = ActorRuntime::empty(10);
        assert_eq!(ar.capacity(), 10);
        assert_eq!(ar.pending_len(), 0);
        assert_eq!(ar.mailbox_len(), 0);
        assert!(!ar.can_receive());
        assert!(!ar.is_blocked());
    }

    #[test]
    fn test_actor_runtime_enqueue_pending() {
        let mut ar = ActorRuntime::empty(10);
        let msg = make_test_message(1);

        ar.enqueue_pending_mut(msg);
        assert_eq!(ar.pending_len(), 1);
    }

    #[test]
    fn test_actor_runtime_enqueue_pending_increases() {
        // Corresponds to Lean theorem: ActorRuntime.enqueue_pending_increases
        let ar = ActorRuntime::empty(10);
        let msg = make_test_message(1);

        let ar2 = ar.enqueue_pending(msg);
        assert_eq!(ar2.pending_len(), ar.pending_len() + 1);
    }

    #[test]
    fn test_actor_runtime_enqueue_pending_preserves_mailbox() {
        // Corresponds to Lean theorem: ActorRuntime.enqueue_pending_preserves_mailbox
        let ar = ActorRuntime::empty(10);
        let msg = make_test_message(1);

        let ar2 = ar.enqueue_pending(msg);
        assert_eq!(ar2.mailbox_len(), ar.mailbox_len());
    }

    #[test]
    fn test_actor_runtime_deliver() {
        let mut ar = ActorRuntime::empty(10);
        let msg = make_test_message(1);

        ar.enqueue_pending_mut(msg);
        assert_eq!(ar.pending_len(), 1);
        assert_eq!(ar.mailbox_len(), 0);

        let result = ar.deliver_mut();
        assert!(matches!(result, Ok(true)));
        assert_eq!(ar.pending_len(), 0);
        assert_eq!(ar.mailbox_len(), 1);
    }

    #[test]
    fn test_actor_runtime_deliver_decreases_pending() {
        // Corresponds to Lean theorem: ActorRuntime.deliver_decreases_pending
        let ar = ActorRuntime::empty(10);
        let msg = make_test_message(1);

        let ar = ar.enqueue_pending(msg);
        let ar2 = ar.deliver();

        assert!(ar2.pending_len() < ar.pending_len());
    }

    #[test]
    fn test_actor_runtime_deliver_increases_mailbox() {
        // Corresponds to Lean theorem: ActorRuntime.deliver_increases_mailbox
        let ar = ActorRuntime::empty(10);
        let msg = make_test_message(1);

        let ar = ar.enqueue_pending(msg);
        let ar2 = ar.deliver();

        assert_eq!(ar2.mailbox_len(), ar.mailbox_len() + 1);
    }

    #[test]
    fn test_actor_runtime_consume() {
        let mut ar = ActorRuntime::empty(10);
        let msg = make_test_message(42);

        ar.enqueue_pending_mut(msg);
        ar.deliver_mut().expect("deliver should succeed");

        let consumed = ar.consume_mut();
        assert!(consumed.is_some());
        assert_eq!(consumed.map(|m| m.id()), Some(42));
        assert_eq!(ar.mailbox_len(), 0);
    }

    #[test]
    fn test_actor_runtime_consume_decreases_mailbox() {
        // Corresponds to Lean theorem: ActorRuntime.consume_decreases_mailbox
        let ar = ActorRuntime::empty(10);
        let msg = make_test_message(1);

        let ar = ar.enqueue_pending(msg);
        let ar = ar.deliver();
        let (ar2, _) = ar.consume();

        assert!(ar2.mailbox_len() < ar.mailbox_len());
    }

    #[test]
    fn test_actor_runtime_consume_preserves_capacity() {
        // Corresponds to Lean theorem: ActorRuntime.consume_preserves_capacity
        let ar = ActorRuntime::empty(10);
        let msg = make_test_message(1);

        let ar = ar.enqueue_pending(msg);
        let ar = ar.deliver();
        let (ar2, _) = ar.consume();

        assert_eq!(ar2.capacity(), ar.capacity());
    }

    #[test]
    fn test_actor_runtime_consume_preserves_blocked_on() {
        // Corresponds to Lean theorem: ActorRuntime.consume_preserves_blockedOn
        let ar = ActorRuntime::empty(10);
        let msg = make_test_message(1);

        let ar = ar.enqueue_pending(msg);
        let ar = ar.deliver();
        let ar = ar.set_blocked(99);
        let (ar2, _) = ar.consume();

        assert_eq!(ar2.blocked_on(), ar.blocked_on());
    }

    #[test]
    fn test_actor_runtime_set_blocked() {
        // Corresponds to Lean theorem: ActorRuntime.set_blocked_sets
        let ar = ActorRuntime::empty(10);
        let ar2 = ar.set_blocked(42);

        assert_eq!(ar2.blocked_on(), Some(42));
    }

    #[test]
    fn test_actor_runtime_set_blocked_preserves_mailbox() {
        // Corresponds to Lean theorem: ActorRuntime.set_blocked_preserves_mailbox
        let ar = ActorRuntime::empty(10);
        let ar2 = ar.set_blocked(42);

        assert_eq!(ar2.mailbox_len(), ar.mailbox_len());
    }

    #[test]
    fn test_actor_runtime_unblock_clears() {
        // Corresponds to Lean theorem: ActorRuntime.unblock_clears
        let ar = ActorRuntime::empty(10);
        let ar = ar.set_blocked(42);
        let ar2 = ar.unblock();

        assert_eq!(ar2.blocked_on(), None);
    }

    #[test]
    fn test_actor_runtime_unblock_preserves_mailbox() {
        // Corresponds to Lean theorem: ActorRuntime.unblock_preserves_mailbox
        let ar = ActorRuntime::empty(10);
        let ar = ar.set_blocked(42);
        let ar2 = ar.unblock();

        assert_eq!(ar2.mailbox_len(), ar.mailbox_len());
    }

    #[test]
    fn test_actor_runtime_mailbox_full() {
        let mut ar = ActorRuntime::empty(2);

        // Fill mailbox
        ar.enqueue_pending_mut(make_test_message(1));
        ar.deliver_mut().expect("deliver 1");
        ar.enqueue_pending_mut(make_test_message(2));
        ar.deliver_mut().expect("deliver 2");

        // Mailbox should be full
        assert!(!ar.mailbox_has_space());
        assert_eq!(ar.mailbox_len(), 2);

        // Try to deliver one more
        ar.enqueue_pending_mut(make_test_message(3));
        let result = ar.deliver_mut();
        assert!(matches!(result, Ok(false))); // Not delivered (full)
    }

    #[test]
    fn test_message_accessors() {
        let msg = Message::new(42, 1, 2, SecurityLevel::Confidential, vec![10, 20, 30]);

        assert_eq!(msg.id(), 42);
        assert_eq!(msg.src(), 1);
        assert_eq!(msg.dst(), 2);
        assert_eq!(msg.level(), SecurityLevel::Confidential);
        assert_eq!(msg.body(), &vec![10, 20, 30]);
    }
}
