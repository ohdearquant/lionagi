// Copyright (C) 2026 HaiyangLi
// SPDX-License-Identifier: Apache-2.0
//! Lion State Workflow
//!
//! Corresponds to: Lion/State/Workflow.lean
//!
//! Workflow DAG and termination (Theorem 008).
//! Uses precomputed adjacency lists and dense indexed arrays for O(1) node lookups.

use crate::types::Time;

/// Error type for workflow operations
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum WorkflowError {
    /// Node not found in workflow
    NodeNotFound(u64),
    /// Invalid node state transition
    InvalidTransition {
        /// The node that had the invalid transition
        node: u64,
        /// The current state
        from: NodeState,
        /// The attempted state
        to: NodeState,
    },
    /// Workflow has timed out
    TimedOut,
    /// Dependencies not satisfied
    DependenciesNotSatisfied {
        /// The node that has unsatisfied dependencies
        node: u64,
        /// The unsatisfied dependencies
        deps: Vec<u64>,
    },
}

impl std::fmt::Display for WorkflowError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            WorkflowError::NodeNotFound(n) => write!(f, "Node {n} not found"),
            WorkflowError::InvalidTransition { node, from, to } => {
                write!(f, "Invalid transition for node {node}: {from:?} -> {to:?}")
            }
            WorkflowError::TimedOut => write!(f, "Workflow timed out"),
            WorkflowError::DependenciesNotSatisfied { node, deps } => {
                write!(f, "Node {node} has unsatisfied dependencies: {deps:?}")
            }
        }
    }
}

impl std::error::Error for WorkflowError {}

/// Workflow execution status
///
/// Corresponds to Lean: `inductive WorkflowStatus`
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Default)]
pub enum WorkflowStatus {
    /// Workflow is running
    #[default]
    Running,
    /// Workflow completed successfully
    Success,
    /// Workflow failed
    Failure,
}

impl WorkflowStatus {
    /// Check if the workflow is running
    #[inline]
    pub fn is_running(&self) -> bool {
        matches!(self, WorkflowStatus::Running)
    }

    /// Check if the workflow is terminal (success or failure)
    #[inline]
    pub fn is_terminal(&self) -> bool {
        matches!(self, WorkflowStatus::Success | WorkflowStatus::Failure)
    }

    /// Check if the workflow succeeded
    #[inline]
    pub fn is_success(&self) -> bool {
        matches!(self, WorkflowStatus::Success)
    }

    /// Check if the workflow failed
    #[inline]
    pub fn is_failure(&self) -> bool {
        matches!(self, WorkflowStatus::Failure)
    }
}

/// Node execution state
///
/// Corresponds to Lean: `inductive NodeState`
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Default)]
pub enum NodeState {
    /// Node is waiting for dependencies
    #[default]
    Pending,
    /// Node is currently executing
    Active,
    /// Node completed successfully
    Completed,
    /// Node failed
    Failed,
}

impl NodeState {
    /// Check if the node is pending
    #[inline]
    pub fn is_pending(&self) -> bool {
        matches!(self, NodeState::Pending)
    }

    /// Check if the node is active
    #[inline]
    pub fn is_active(&self) -> bool {
        matches!(self, NodeState::Active)
    }

    /// Check if the node is completed
    #[inline]
    pub fn is_completed(&self) -> bool {
        matches!(self, NodeState::Completed)
    }

    /// Check if the node is failed
    #[inline]
    pub fn is_failed(&self) -> bool {
        matches!(self, NodeState::Failed)
    }

    /// Check if the node is terminal (completed or failed)
    #[inline]
    pub fn is_terminal(&self) -> bool {
        matches!(self, NodeState::Completed | NodeState::Failed)
    }
}

/// DAG edge: (source, target) means 'source' must complete before 'target' starts
///
/// Corresponds to Lean: `structure Edge`
/// NOTE: Renamed 'from' to 'source' to avoid Lean keyword conflict
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Default)]
pub struct Edge {
    /// Source node (must complete first)
    ///
    /// Corresponds to Lean: `from_ : Nat`
    pub(crate) source: u64,

    /// Target node (starts after source)
    ///
    /// Corresponds to Lean: `to_ : Nat`
    pub(crate) target: u64,
}

impl Edge {
    /// Create a new edge
    pub fn new(source: u64, target: u64) -> Self {
        Edge { source, target }
    }

    /// Get the source node
    /// NOTE: Method kept as 'from()' for API compatibility
    #[inline]
    pub fn from(&self) -> u64 {
        self.source
    }

    /// Get the target node
    /// NOTE: Method kept as 'to()' for API compatibility
    #[inline]
    pub fn to(&self) -> u64 {
        self.target
    }
}

/// Workflow definition (static DAG structure)
///
/// Corresponds to Lean: `structure WorkflowDef`
///
/// Precomputes adjacency lists and a node-to-index map for O(1) lookups.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct WorkflowDef {
    /// Node identifiers (sorted, deduplicated)
    ///
    /// Corresponds to Lean: `nodes : List Nat`
    pub(crate) nodes: Vec<u64>,

    /// DAG edges
    ///
    /// Corresponds to Lean: `edges : List Edge`
    pub(crate) edges: Vec<Edge>,

    /// Precomputed predecessors per node index: predecessors[i] = list of node IDs
    /// that must complete before nodes[i] can start.
    predecessors: Vec<Vec<u64>>,

    /// Precomputed successors per node index: successors[i] = list of node IDs
    /// that depend on nodes[i].
    successors: Vec<Vec<u64>>,

    /// Sorted (node_id, index) pairs for binary-search lookup.
    node_to_index: Vec<(u64, usize)>,
}

impl WorkflowDef {
    /// Build adjacency lists and index map from nodes + edges.
    fn build_adjacency(&mut self) {
        let n = self.nodes.len();
        self.predecessors = vec![Vec::new(); n];
        self.successors = vec![Vec::new(); n];
        self.node_to_index = self
            .nodes
            .iter()
            .enumerate()
            .map(|(i, &id)| (id, i))
            .collect();
        // node_to_index is sorted because nodes is sorted
        self.node_to_index.sort_unstable_by_key(|&(id, _)| id);

        for e in &self.edges {
            if let (Some(src_idx), Some(tgt_idx)) =
                (self.node_index(e.source), self.node_index(e.target))
            {
                self.predecessors[tgt_idx].push(e.source);
                self.successors[src_idx].push(e.target);
            }
        }
    }

    /// Create a new workflow definition (unchecked).
    ///
    /// Prefer `validated()` for production use to ensure all edge endpoints
    /// reference existing nodes.
    pub fn new(nodes: Vec<u64>, edges: Vec<Edge>) -> Self {
        let mut def = WorkflowDef {
            nodes,
            edges,
            predecessors: Vec::new(),
            successors: Vec::new(),
            node_to_index: Vec::new(),
        };
        def.build_adjacency();
        def
    }

    /// Create a validated workflow definition.
    ///
    /// Sorts and deduplicates node IDs, then verifies that all edge
    /// endpoints reference existing nodes.
    ///
    /// # Errors
    ///
    /// Returns `WorkflowError::NodeNotFound` if an edge references a node not in the node list.
    pub fn validated(mut nodes: Vec<u64>, edges: Vec<Edge>) -> Result<Self, WorkflowError> {
        nodes.sort_unstable();
        nodes.dedup();

        for e in &edges {
            if nodes.binary_search(&e.source).is_err() {
                return Err(WorkflowError::NodeNotFound(e.source));
            }
            if nodes.binary_search(&e.target).is_err() {
                return Err(WorkflowError::NodeNotFound(e.target));
            }
        }

        let mut def = WorkflowDef {
            nodes,
            edges,
            predecessors: Vec::new(),
            successors: Vec::new(),
            node_to_index: Vec::new(),
        };
        def.build_adjacency();
        Ok(def)
    }

    /// Look up the dense index for a node ID via binary search.
    #[inline]
    pub fn node_index(&self, node_id: u64) -> Option<usize> {
        self.node_to_index
            .binary_search_by_key(&node_id, |&(id, _)| id)
            .ok()
            .map(|pos| self.node_to_index[pos].1)
    }

    /// Require a node to exist, returning its index or an error.
    pub fn require_node(&self, node_id: u64) -> Result<usize, WorkflowError> {
        self.node_index(node_id)
            .ok_or(WorkflowError::NodeNotFound(node_id))
    }

    /// Check if node has no incoming edges (ready to start)
    ///
    /// Corresponds to Lean: `def WorkflowDef.is_source`
    pub fn is_source(&self, n: u64) -> bool {
        match self.node_index(n) {
            Some(idx) => self.predecessors[idx].is_empty(),
            None => true, // non-existent nodes trivially have no predecessors
        }
    }

    /// Check if node has no outgoing edges (terminal)
    ///
    /// Corresponds to Lean: `def WorkflowDef.is_sink`
    pub fn is_sink(&self, n: u64) -> bool {
        match self.node_index(n) {
            Some(idx) => self.successors[idx].is_empty(),
            None => true,
        }
    }

    /// Get dependencies of a node (predecessors)
    ///
    /// Corresponds to Lean: `def WorkflowDef.dependencies`
    pub fn dependencies(&self, n: u64) -> Vec<u64> {
        match self.node_index(n) {
            Some(idx) => self.predecessors[idx].clone(),
            None => Vec::new(),
        }
    }

    /// Get dependents of a node (successors)
    ///
    /// Corresponds to Lean: `def WorkflowDef.dependents`
    pub fn dependents(&self, n: u64) -> Vec<u64> {
        match self.node_index(n) {
            Some(idx) => self.successors[idx].clone(),
            None => Vec::new(),
        }
    }

    /// Get the number of nodes
    #[inline]
    pub fn node_count(&self) -> usize {
        self.nodes.len()
    }

    /// Get the number of edges
    #[inline]
    pub fn edge_count(&self) -> usize {
        self.edges.len()
    }

    /// Check if a node exists in the workflow
    #[inline]
    pub fn contains_node(&self, n: u64) -> bool {
        self.node_index(n).is_some()
    }

    /// Get all nodes
    pub fn nodes(&self) -> &[u64] {
        &self.nodes
    }

    /// Get all edges
    pub fn edges(&self) -> &[Edge] {
        &self.edges
    }
}

/// Entry in node state map (node_id, state)
///
/// Kept for API compatibility. WorkflowInstance uses dense arrays internally.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct NodeStateEntry {
    /// Node identifier
    pub(crate) node_id: u64,
    /// Current state of the node
    pub(crate) state: NodeState,
}

/// Entry in retry count map (node_id, retry_count)
///
/// Kept for API compatibility. WorkflowInstance uses dense arrays internally.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct RetryEntry {
    /// Node identifier
    pub(crate) node_id: u64,
    /// Remaining retry count
    pub(crate) count: u64,
}

/// Workflow instance (runtime state)
///
/// Corresponds to Lean: `structure WorkflowInstance`
///
/// Uses dense arrays indexed by node position for O(1) state access.
/// Maintains counters for O(1) aggregate queries.
#[derive(Debug, Clone, Default)]
pub struct WorkflowInstance {
    /// Static workflow definition
    ///
    /// Corresponds to Lean: `definition : WorkflowDef`
    pub(crate) workflow_def: WorkflowDef,

    /// Current workflow status
    ///
    /// Corresponds to Lean: `status : WorkflowStatus`
    pub(crate) status: WorkflowStatus,

    /// Per-node state (dense, indexed by node position in workflow_def.nodes)
    ///
    /// Corresponds to Lean: `node_states : Nat -> NodeState`
    pub(crate) node_states: Vec<NodeState>,

    /// Per-node retry count (dense, indexed by node position)
    ///
    /// Corresponds to Lean: `retries : Nat -> Nat`
    pub(crate) retries: Vec<u16>,

    /// Timeout deadline
    ///
    /// Corresponds to Lean: `timeout_at : Time`
    pub(crate) timeout_at: Time,

    /// Count of nodes in Pending state
    pending_ct: u32,

    /// Count of nodes in Active state
    active_ct: u32,

    /// Count of nodes in Failed state
    failed_ct: u32,
}

impl WorkflowInstance {
    /// Create a new workflow instance
    pub fn new(workflow_def: WorkflowDef, timeout_at: Time, max_retries: u64) -> Self {
        let n = workflow_def.nodes.len();
        let node_states = vec![NodeState::Pending; n];
        let retry_val = if max_retries > 65535u64 {
            65535u16
        } else {
            max_retries as u16
        };
        let retries = vec![retry_val; n];

        WorkflowInstance {
            workflow_def,
            status: WorkflowStatus::Running,
            node_states,
            retries,
            timeout_at,
            pending_ct: n as u32,
            active_ct: 0,
            failed_ct: 0,
        }
    }

    /// Create a simple running workflow instance with specified timeout
    ///
    /// Useful for tests where we just need a running workflow.
    pub fn running(timeout_at: Time) -> Self {
        WorkflowInstance {
            workflow_def: WorkflowDef::default(),
            status: WorkflowStatus::Running,
            node_states: Vec::new(),
            retries: Vec::new(),
            timeout_at,
            pending_ct: 0,
            active_ct: 0,
            failed_ct: 0,
        }
    }

    /// Check if this workflow is running
    ///
    /// Convenience method that delegates to status().is_running()
    #[inline]
    pub fn is_running(&self) -> bool {
        self.status.is_running()
    }

    /// Check that a node exists and return its dense index.
    ///
    /// # Errors
    ///
    /// Returns `WorkflowError::NodeNotFound` if the node is not in the definition.
    #[inline]
    fn require_node(&self, n: u64) -> Result<usize, WorkflowError> {
        self.workflow_def.require_node(n)
    }

    /// Get node state by node ID.
    ///
    /// Returns `Pending` for nodes that exist in the definition but have no
    /// explicit state entry yet. For nodes NOT in the definition, also returns Pending
    /// (callers should use `require_node()` first for validation).
    pub fn get_node_state(&self, n: u64) -> NodeState {
        match self.workflow_def.node_index(n) {
            Some(idx) => self.node_states[idx],
            None => NodeState::Pending,
        }
    }

    /// Check if node is in pending state
    ///
    /// Corresponds to Lean: `def WorkflowInstance.is_pending`
    #[inline]
    pub fn is_pending(&self, n: u64) -> bool {
        self.get_node_state(n).is_pending()
    }

    /// Check if node is in active state
    ///
    /// Corresponds to Lean: `def WorkflowInstance.is_active`
    #[inline]
    pub fn is_active(&self, n: u64) -> bool {
        self.get_node_state(n).is_active()
    }

    /// Count pending nodes (O(1))
    ///
    /// Corresponds to Lean: `def WorkflowInstance.pending_count`
    #[inline]
    pub fn pending_count(&self) -> usize {
        self.pending_ct as usize
    }

    /// Count active nodes (O(1))
    ///
    /// Corresponds to Lean: `def WorkflowInstance.active_count`
    #[inline]
    pub fn active_count(&self) -> usize {
        self.active_ct as usize
    }

    /// Check if all nodes are terminal (completed or failed) (O(1))
    ///
    /// Corresponds to Lean: `def WorkflowInstance.is_terminal`
    #[inline]
    pub fn is_terminal(&self) -> bool {
        self.pending_ct == 0 && self.active_ct == 0
    }

    /// Check if at least one node failed (O(1))
    ///
    /// Corresponds to Lean: `def WorkflowInstance.has_failure`
    #[inline]
    pub fn has_failure(&self) -> bool {
        self.failed_ct > 0
    }

    /// Get the workflow status
    #[inline]
    pub fn status(&self) -> WorkflowStatus {
        self.status
    }

    /// Get the timeout deadline
    #[inline]
    pub fn timeout_at(&self) -> Time {
        self.timeout_at
    }

    /// Get retry count for a node
    pub fn get_retries(&self, n: u64) -> u64 {
        match self.workflow_def.node_index(n) {
            Some(idx) => self.retries[idx] as u64,
            None => 0,
        }
    }

    /// Get a reference to the definition
    #[inline]
    pub fn definition(&self) -> &WorkflowDef {
        &self.workflow_def
    }

    /// Check if dependencies are satisfied for a node
    pub fn dependencies_satisfied(&self, n: u64) -> bool {
        match self.workflow_def.node_index(n) {
            Some(idx) => {
                let preds = &self.workflow_def.predecessors[idx];
                let mut j = 0;
                while j < preds.len() {
                    let pred_id = preds[j];
                    if let Some(pred_idx) = self.workflow_def.node_index(pred_id) {
                        if !self.node_states[pred_idx].is_completed() {
                            return false;
                        }
                    }
                    j += 1;
                }
                true
            }
            None => true,
        }
    }

    /// Transition a node's state, updating counters.
    fn transition(&mut self, idx: usize, new_state: NodeState) {
        let old = self.node_states[idx];
        // Decrement old counter
        match old {
            NodeState::Pending => {
                debug_assert!(self.pending_ct > 0, "pending_ct underflow");
                self.pending_ct = self.pending_ct.saturating_sub(1);
            }
            NodeState::Active => {
                debug_assert!(self.active_ct > 0, "active_ct underflow");
                self.active_ct = self.active_ct.saturating_sub(1);
            }
            NodeState::Failed => {
                debug_assert!(self.failed_ct > 0, "failed_ct underflow");
                self.failed_ct = self.failed_ct.saturating_sub(1);
            }
            NodeState::Completed => {}
        }
        // Increment new counter
        match new_state {
            NodeState::Pending => self.pending_ct += 1,
            NodeState::Active => self.active_ct += 1,
            NodeState::Failed => self.failed_ct += 1,
            NodeState::Completed => {}
        }
        self.node_states[idx] = new_state;
    }

    /// Start a pending node (transition to active)
    ///
    /// Returns Err if node not in definition, not pending, or dependencies not satisfied.
    ///
    /// # Errors
    ///
    /// Returns `WorkflowError::NodeNotFound` if the node is not in the workflow definition.
    /// Returns `WorkflowError::InvalidTransition` if the node is not in the `Pending` state.
    /// Returns `WorkflowError::DependenciesNotSatisfied` if upstream nodes are not yet completed.
    pub fn start_node(&mut self, n: u64) -> Result<(), WorkflowError> {
        let idx = self.require_node(n)?;
        let state = self.node_states[idx];
        if !state.is_pending() {
            return Err(WorkflowError::InvalidTransition {
                node: n,
                from: state,
                to: NodeState::Active,
            });
        }

        if !self.dependencies_satisfied(n) {
            let preds = &self.workflow_def.predecessors[idx];
            let mut unsatisfied = Vec::new();
            let mut j = 0;
            while j < preds.len() {
                let pred_id = preds[j];
                if let Some(pi) = self.workflow_def.node_index(pred_id) {
                    if !self.node_states[pi].is_completed() {
                        unsatisfied.push(pred_id);
                    }
                } else {
                    unsatisfied.push(pred_id);
                }
                j += 1;
            }
            return Err(WorkflowError::DependenciesNotSatisfied {
                node: n,
                deps: unsatisfied,
            });
        }

        self.transition(idx, NodeState::Active);
        Ok(())
    }

    /// Complete an active node (transition to completed)
    ///
    /// # Errors
    ///
    /// Returns `WorkflowError::NodeNotFound` if the node is not in the workflow definition.
    /// Returns `WorkflowError::InvalidTransition` if the node is not in the `Active` state.
    pub fn complete_node(&mut self, n: u64) -> Result<(), WorkflowError> {
        let idx = self.require_node(n)?;
        let state = self.node_states[idx];
        if !state.is_active() {
            return Err(WorkflowError::InvalidTransition {
                node: n,
                from: state,
                to: NodeState::Completed,
            });
        }

        self.transition(idx, NodeState::Completed);
        self.update_workflow_status();
        Ok(())
    }

    /// Fail an active node (transition to failed)
    ///
    /// # Errors
    ///
    /// Returns `WorkflowError::NodeNotFound` if the node is not in the workflow definition.
    /// Returns `WorkflowError::InvalidTransition` if the node is not in the `Active` state.
    pub fn fail_node(&mut self, n: u64) -> Result<(), WorkflowError> {
        let idx = self.require_node(n)?;
        let state = self.node_states[idx];
        if !state.is_active() {
            return Err(WorkflowError::InvalidTransition {
                node: n,
                from: state,
                to: NodeState::Failed,
            });
        }

        // Check if we have retries
        let retries = self.retries[idx];
        if retries > 0 {
            // Retry: go back to pending and decrement retries
            self.transition(idx, NodeState::Pending);
            self.retries[idx] = retries - 1;
        } else {
            // No retries left: fail
            self.transition(idx, NodeState::Failed);
            self.status = WorkflowStatus::Failure;
        }

        Ok(())
    }

    /// Apply timeout
    pub fn apply_timeout(&mut self) {
        self.status = WorkflowStatus::Failure;
    }

    /// Update workflow status based on node states
    fn update_workflow_status(&mut self) {
        if self.is_terminal() {
            if self.has_failure() {
                self.status = WorkflowStatus::Failure;
            } else {
                self.status = WorkflowStatus::Success;
            }
        }
    }

    /// Calculate the termination measure
    ///
    /// Corresponds to Lean: `def workflow_measure`
    ///
    /// A natural number that decreases on each workflow step.
    pub fn measure(&self, now: Time) -> u64 {
        let time_left = self.timeout_at.saturating_sub(now);

        let pending = self.pending_ct as u64;
        let active = self.active_ct as u64;

        let retries_remaining: u64 = self
            .retries
            .iter()
            .fold(0u64, |acc, &r| acc.saturating_add(r as u64));

        // Weighted sum for strict decrease (from Lean)
        // Weights: time (x1), pending (x1000), active (x100), retries_remaining (x1000)
        // Saturating arithmetic: an overflow here must clamp, never wrap
        time_left
            .saturating_add(pending.saturating_mul(1000))
            .saturating_add(active.saturating_mul(100))
            .saturating_add(retries_remaining.saturating_mul(1000))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn make_linear_workflow() -> WorkflowDef {
        // 1 -> 2 -> 3
        WorkflowDef::new(vec![1, 2, 3], vec![Edge::new(1, 2), Edge::new(2, 3)])
    }

    fn make_parallel_workflow() -> WorkflowDef {
        // 1 -> 3, 2 -> 3 (both 1 and 2 must complete before 3)
        WorkflowDef::new(vec![1, 2, 3], vec![Edge::new(1, 3), Edge::new(2, 3)])
    }

    #[test]
    fn test_workflow_status() {
        assert!(WorkflowStatus::Running.is_running());
        assert!(!WorkflowStatus::Running.is_terminal());

        assert!(WorkflowStatus::Success.is_terminal());
        assert!(WorkflowStatus::Success.is_success());

        assert!(WorkflowStatus::Failure.is_terminal());
        assert!(WorkflowStatus::Failure.is_failure());
    }

    #[test]
    fn test_node_state() {
        assert!(NodeState::Pending.is_pending());
        assert!(NodeState::Active.is_active());
        assert!(NodeState::Completed.is_completed());
        assert!(NodeState::Failed.is_failed());

        assert!(NodeState::Completed.is_terminal());
        assert!(NodeState::Failed.is_terminal());
        assert!(!NodeState::Pending.is_terminal());
        assert!(!NodeState::Active.is_terminal());
    }

    #[test]
    fn test_workflow_def_dependencies() {
        let wf = make_linear_workflow();

        // Node 1 has no dependencies (source)
        assert!(wf.dependencies(1).is_empty());
        assert!(wf.is_source(1));

        // Node 2 depends on 1
        assert_eq!(wf.dependencies(2), vec![1]);

        // Node 3 depends on 2 (sink)
        assert_eq!(wf.dependencies(3), vec![2]);
        assert!(wf.is_sink(3));
    }

    #[test]
    fn test_workflow_def_dependents() {
        let wf = make_linear_workflow();

        assert_eq!(wf.dependents(1), vec![2]);
        assert_eq!(wf.dependents(2), vec![3]);
        assert!(wf.dependents(3).is_empty());
    }

    #[test]
    fn test_workflow_instance_new() {
        let wf = make_linear_workflow();
        let wi = WorkflowInstance::new(wf, 100, 3);

        assert!(wi.status().is_running());
        assert_eq!(wi.pending_count(), 3);
        assert_eq!(wi.active_count(), 0);
        assert_eq!(wi.get_retries(1), 3);
    }

    #[test]
    fn test_workflow_instance_start_node() {
        let wf = make_linear_workflow();
        let mut wi = WorkflowInstance::new(wf, 100, 0);

        // Start node 1 (source, no deps)
        assert!(wi.start_node(1).is_ok());
        assert!(wi.is_active(1));

        // Cannot start node 2 (deps not satisfied)
        let result = wi.start_node(2);
        assert!(matches!(
            result,
            Err(WorkflowError::DependenciesNotSatisfied { .. })
        ));
    }

    #[test]
    fn test_workflow_instance_complete_flow() {
        let wf = make_linear_workflow();
        let mut wi = WorkflowInstance::new(wf, 100, 0);

        // Execute 1 -> 2 -> 3
        wi.start_node(1).expect("start 1");
        wi.complete_node(1).expect("complete 1");

        wi.start_node(2).expect("start 2");
        wi.complete_node(2).expect("complete 2");

        wi.start_node(3).expect("start 3");
        wi.complete_node(3).expect("complete 3");

        assert!(wi.is_terminal());
        assert!(wi.status().is_success());
    }

    #[test]
    fn test_workflow_instance_failure() {
        let wf = make_linear_workflow();
        let mut wi = WorkflowInstance::new(wf, 100, 0);

        wi.start_node(1).expect("start 1");
        wi.fail_node(1).expect("fail 1");

        assert!(wi.status().is_failure());
    }

    #[test]
    fn test_workflow_instance_retry() {
        let wf = make_linear_workflow();
        let mut wi = WorkflowInstance::new(wf, 100, 2);

        wi.start_node(1).expect("start 1");
        assert_eq!(wi.get_retries(1), 2);

        // First fail - should retry
        wi.fail_node(1).expect("fail 1");
        assert!(wi.is_pending(1)); // Back to pending
        assert_eq!(wi.get_retries(1), 1);
        assert!(wi.status().is_running()); // Still running

        // Second fail - should retry again
        wi.start_node(1).expect("start 1 again");
        wi.fail_node(1).expect("fail 1 again");
        assert!(wi.is_pending(1));
        assert_eq!(wi.get_retries(1), 0);

        // Third fail - no more retries
        wi.start_node(1).expect("start 1 final");
        wi.fail_node(1).expect("fail 1 final");
        assert!(wi.get_node_state(1).is_failed());
        assert!(wi.status().is_failure());
    }

    #[test]
    fn test_workflow_measure_decreases_on_complete() {
        let wf = make_linear_workflow();
        let mut wi = WorkflowInstance::new(wf, 100, 0);

        let m1 = wi.measure(0);

        wi.start_node(1).expect("start 1");
        let m2 = wi.measure(0);

        // start_node: pending decreases, active increases
        // net: -1000 + 100 = -900
        assert!(m2 < m1);

        wi.complete_node(1).expect("complete 1");
        let m3 = wi.measure(0);

        // complete_node: active decreases
        // net: -100
        assert!(m3 < m2);
    }

    #[test]
    fn test_workflow_parallel() {
        let wf = make_parallel_workflow();
        let mut wi = WorkflowInstance::new(wf, 100, 0);

        // Both 1 and 2 can start (they're sources)
        wi.start_node(1).expect("start 1");
        wi.start_node(2).expect("start 2");

        // Cannot start 3 yet
        let result = wi.start_node(3);
        assert!(matches!(
            result,
            Err(WorkflowError::DependenciesNotSatisfied { .. })
        ));

        // Complete 1
        wi.complete_node(1).expect("complete 1");

        // Still cannot start 3
        let result = wi.start_node(3);
        assert!(matches!(
            result,
            Err(WorkflowError::DependenciesNotSatisfied { .. })
        ));

        // Complete 2
        wi.complete_node(2).expect("complete 2");

        // Now can start 3
        wi.start_node(3).expect("start 3");
        wi.complete_node(3).expect("complete 3");

        assert!(wi.status().is_success());
    }
}
