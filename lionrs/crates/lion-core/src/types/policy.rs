// Copyright (C) 2026 HaiyangLi
// SPDX-License-Identifier: Apache-2.0
//! Lion Core Policy
//!
//! Corresponds to: Lion/Core/Policy.lean
//!
//! Policy evaluation for access control.
//! Fail-closed: indeterminate -> deny.

use super::{PluginId, ResourceId, Rights, Time};

/// Policy evaluation result
///
/// Corresponds to Lean: `inductive PolicyDecision`
///
/// Three-valued logic for policy decisions, following Kleene semantics.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Default)]
#[must_use = "policy decisions must be checked to enforce access control"]
pub enum PolicyDecision {
    /// Access is explicitly permitted
    Permit,
    /// Access is explicitly denied
    #[default]
    Deny,
    /// Cannot determine (requires fail-closed handling)
    Indeterminate,
}

impl PolicyDecision {
    /// Deny-absorbing composition
    ///
    /// Corresponds to Lean: `def PolicyDecision.combine`
    ///
    /// - deny + anything = deny
    /// - permit + permit = permit
    /// - otherwise = indeterminate
    pub const fn combine(self, other: PolicyDecision) -> PolicyDecision {
        match (self, other) {
            (PolicyDecision::Deny, _) | (_, PolicyDecision::Deny) => PolicyDecision::Deny,
            (PolicyDecision::Permit, PolicyDecision::Permit) => PolicyDecision::Permit,
            _ => PolicyDecision::Indeterminate,
        }
    }

    /// Policy AND: Both must permit, any deny propagates
    ///
    /// Corresponds to Lean: `def PolicyDecision.and_policy`
    pub const fn and_policy(self, other: PolicyDecision) -> PolicyDecision {
        match (self, other) {
            (PolicyDecision::Permit, PolicyDecision::Permit) => PolicyDecision::Permit,
            (PolicyDecision::Deny, _) | (_, PolicyDecision::Deny) => PolicyDecision::Deny,
            _ => PolicyDecision::Indeterminate,
        }
    }

    /// Policy OR: Either permits, both deny required for deny
    ///
    /// Corresponds to Lean: `def PolicyDecision.or_policy`
    pub const fn or_policy(self, other: PolicyDecision) -> PolicyDecision {
        match (self, other) {
            (PolicyDecision::Permit, _) | (_, PolicyDecision::Permit) => PolicyDecision::Permit,
            (PolicyDecision::Deny, PolicyDecision::Deny) => PolicyDecision::Deny,
            _ => PolicyDecision::Indeterminate,
        }
    }

    /// Policy NOT: Invert permit/deny, indeterminate stays
    ///
    /// Corresponds to Lean: `def PolicyDecision.not_policy`
    pub const fn not_policy(self) -> PolicyDecision {
        match self {
            PolicyDecision::Permit => PolicyDecision::Deny,
            PolicyDecision::Deny => PolicyDecision::Permit,
            PolicyDecision::Indeterminate => PolicyDecision::Indeterminate,
        }
    }

    /// Policy Override: First determinate decision wins
    ///
    /// Corresponds to Lean: `def PolicyDecision.override_policy`
    pub const fn override_policy(self, other: PolicyDecision) -> PolicyDecision {
        match self {
            PolicyDecision::Indeterminate => other,
            _ => self,
        }
    }

    /// Policy Implication: p1 => p2
    ///
    /// Corresponds to Lean: `def PolicyDecision.implies_policy`
    ///
    /// - If p1 is Permit, return p2 (antecedent satisfied)
    /// - If p1 is Deny, return Permit (vacuously true)
    /// - If p1 is Indeterminate, return Indeterminate
    pub const fn implies_policy(self, other: PolicyDecision) -> PolicyDecision {
        match self {
            PolicyDecision::Permit => other,
            PolicyDecision::Deny => PolicyDecision::Permit,
            PolicyDecision::Indeterminate => PolicyDecision::Indeterminate,
        }
    }

    /// Check if this decision is determinate (not indeterminate)
    #[inline]
    pub const fn is_determinate(self) -> bool {
        !matches!(self, PolicyDecision::Indeterminate)
    }

    /// Check if this decision permits access
    #[inline]
    pub const fn is_permit(self) -> bool {
        matches!(self, PolicyDecision::Permit)
    }

    /// Check if this decision denies access
    #[inline]
    pub const fn is_deny(self) -> bool {
        matches!(self, PolicyDecision::Deny)
    }

    /// Get the name of this decision.
    #[inline]
    pub const fn name(self) -> &'static str {
        match self {
            PolicyDecision::Permit => "permit",
            PolicyDecision::Deny => "deny",
            PolicyDecision::Indeterminate => "indeterminate",
        }
    }
}

impl std::fmt::Display for PolicyDecision {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.name())
    }
}

#[cfg(feature = "serde")]
impl serde::Serialize for PolicyDecision {
    fn serialize<S: serde::Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        serializer.serialize_str(self.name())
    }
}

/// Error parsing a PolicyDecision from string.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ParsePolicyDecisionError {
    input: String,
}

impl std::fmt::Display for ParsePolicyDecisionError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "invalid policy decision: '{}'", self.input)
    }
}

impl std::error::Error for ParsePolicyDecisionError {}

impl std::str::FromStr for PolicyDecision {
    type Err = ParsePolicyDecisionError;

    fn from_str(s: &str) -> Result<Self, Self::Err> {
        match s.trim().to_lowercase().as_str() {
            "permit" => Ok(PolicyDecision::Permit),
            "deny" => Ok(PolicyDecision::Deny),
            "indeterminate" => Ok(PolicyDecision::Indeterminate),
            _ => Err(ParsePolicyDecisionError {
                input: s.to_string(),
            }),
        }
    }
}

#[cfg(feature = "serde")]
impl<'de> serde::Deserialize<'de> for PolicyDecision {
    fn deserialize<D: serde::Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        let s = String::deserialize(deserializer)?;
        s.parse().map_err(serde::de::Error::custom)
    }
}

/// Error type for Policy operations
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum PolicyError {
    /// Invalid action (e.g., empty kind)
    InvalidAction(String),
    /// Policy evaluation failed
    EvaluationFailed(String),
}

impl std::fmt::Display for PolicyError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            PolicyError::InvalidAction(msg) => write!(f, "Invalid action: {msg}"),
            PolicyError::EvaluationFailed(msg) => write!(f, "Policy evaluation failed: {msg}"),
        }
    }
}

impl std::error::Error for PolicyError {}

/// Action to be authorized
///
/// Corresponds to Lean: `structure Action`
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Action {
    /// The plugin requesting access
    pub(crate) subject: PluginId,
    /// The resource being accessed
    pub(crate) target: ResourceId,
    /// The rights being requested
    pub(crate) rights: Rights,
    /// The type of action (e.g., "read", "invoke", "delegate")
    pub(crate) kind: String,
}

impl Action {
    /// Create a new action
    ///
    /// # Errors
    ///
    /// Returns `PolicyError::InvalidAction` if the `kind` string is empty.
    pub fn new(
        subject: PluginId,
        target: ResourceId,
        rights: Rights,
        kind: String,
    ) -> Result<Self, PolicyError> {
        if kind.is_empty() {
            return Err(PolicyError::InvalidAction("kind cannot be empty".into()));
        }
        Ok(Action {
            subject,
            target,
            rights,
            kind,
        })
    }

    /// Get the subject (requestor) of the action
    #[inline]
    pub fn subject(&self) -> PluginId {
        self.subject
    }

    /// Get the target resource of the action
    #[inline]
    pub fn target(&self) -> ResourceId {
        self.target
    }

    /// Get the rights being requested
    #[inline]
    pub fn rights(&self) -> &Rights {
        &self.rights
    }

    /// Get the kind of action
    #[inline]
    pub fn kind(&self) -> &str {
        &self.kind
    }
}

/// Context for policy evaluation
///
/// Corresponds to Lean: `structure PolicyContext`
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct PolicyContext {
    /// Current logical time
    pub(crate) now: Time,
    /// Origin of the call chain (for confused deputy prevention)
    pub(crate) call_origin: Option<PluginId>,
}

impl PolicyContext {
    /// Create a new policy context
    pub fn new(now: Time, call_origin: Option<PluginId>) -> Self {
        PolicyContext { now, call_origin }
    }

    /// Get the current time
    #[inline]
    pub fn now(&self) -> Time {
        self.now
    }

    /// Get the call origin
    #[inline]
    pub fn call_origin(&self) -> Option<PluginId> {
        self.call_origin
    }
}

/// Extractable policy decision function.
///
/// This replaces the previous closure-based policy representation. Add new enum
/// variants for policy patterns that need extraction rather than storing opaque
/// closures.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Default)]
pub enum PolicyDecisionFn {
    /// Deny every action.
    #[default]
    DenyAll,
    /// Permit every action.
    PermitAll,
    /// Return a fixed decision.
    ///
    /// This keeps constant custom decisions representable without an opaque
    /// closure. Action-dependent custom policies should become explicit enum
    /// variants.
    Custom(PolicyDecision),
}

impl PolicyDecisionFn {
    /// Evaluate the policy pattern for an action and context.
    ///
    /// Corresponds to Lean: `def policy_eval`
    #[inline]
    pub fn eval(&self, _action: &Action, _ctx: &PolicyContext) -> PolicyDecision {
        match self {
            Self::DenyAll => PolicyDecision::Deny,
            Self::PermitAll => PolicyDecision::Permit,
            Self::Custom(decision) => *decision,
        }
    }
}

impl From<PolicyDecision> for PolicyDecisionFn {
    fn from(decision: PolicyDecision) -> Self {
        match decision {
            PolicyDecision::Permit => Self::PermitAll,
            PolicyDecision::Deny => Self::DenyAll,
            PolicyDecision::Indeterminate => Self::Custom(PolicyDecision::Indeterminate),
        }
    }
}

/// Policy state wrapping an extractable decision enum.
///
/// Corresponds to Lean: `structure PolicyState`
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct PolicyState {
    /// The policy decision pattern
    decide: PolicyDecisionFn,
}

impl PolicyState {
    /// Create a new policy state from an extractable decision pattern.
    pub fn new(decide: PolicyDecisionFn) -> Self {
        PolicyState { decide }
    }

    /// Create a deny-all policy (safest default, fail-closed)
    pub fn deny_all() -> Self {
        PolicyState::new(PolicyDecisionFn::DenyAll)
    }

    /// Create a permit-all policy (for testing only)
    pub fn permit_all() -> Self {
        PolicyState::new(PolicyDecisionFn::PermitAll)
    }

    /// Create a policy that always returns the given decision.
    pub fn from_decision(decision: PolicyDecision) -> Self {
        let decide = match decision {
            PolicyDecision::Permit => PolicyDecisionFn::PermitAll,
            PolicyDecision::Deny => PolicyDecisionFn::DenyAll,
            PolicyDecision::Indeterminate => {
                PolicyDecisionFn::Custom(PolicyDecision::Indeterminate)
            }
        };
        PolicyState::new(decide)
    }

    /// Get the extractable decision pattern.
    #[inline]
    pub fn decision_fn(&self) -> PolicyDecisionFn {
        self.decide
    }

    /// Evaluate the policy for an action and context
    ///
    /// Corresponds to Lean: `def policy_eval`
    #[inline]
    pub fn eval(&self, action: &Action, ctx: &PolicyContext) -> PolicyDecision {
        self.decide.eval(action, ctx)
    }

    /// Fail-closed policy check: indeterminate -> deny
    ///
    /// Corresponds to Lean: `def policy_check`
    ///
    /// This ensures no ambiguous access is ever permitted.
    pub fn check(&self, action: &Action, ctx: &PolicyContext) -> PolicyDecision {
        match self.eval(action, ctx) {
            PolicyDecision::Permit => PolicyDecision::Permit,
            // Fail-closed: both Deny and Indeterminate map to Deny
            PolicyDecision::Deny | PolicyDecision::Indeterminate => PolicyDecision::Deny,
        }
    }
}

impl Default for PolicyState {
    /// Default policy: deny-all (safest default)
    fn default() -> Self {
        PolicyState::deny_all()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::types::Right;

    fn make_test_action() -> Action {
        Action::new(1, 2, Rights::singleton(Right::Read), "test".into()).expect("valid action")
    }

    fn make_test_ctx() -> PolicyContext {
        PolicyContext::new(100, Some(1))
    }

    #[test]
    fn test_combine_deny_left() {
        // Corresponds to Lean theorem: combine_deny_left
        for d in [
            PolicyDecision::Permit,
            PolicyDecision::Deny,
            PolicyDecision::Indeterminate,
        ] {
            assert_eq!(PolicyDecision::Deny.combine(d), PolicyDecision::Deny);
        }
    }

    #[test]
    fn test_combine_deny_right() {
        // Corresponds to Lean theorem: combine_deny_right
        for d in [
            PolicyDecision::Permit,
            PolicyDecision::Deny,
            PolicyDecision::Indeterminate,
        ] {
            assert_eq!(d.combine(PolicyDecision::Deny), PolicyDecision::Deny);
        }
    }

    #[test]
    fn test_and_policy_comm() {
        // Corresponds to Lean theorem: and_policy_comm
        for d1 in [
            PolicyDecision::Permit,
            PolicyDecision::Deny,
            PolicyDecision::Indeterminate,
        ] {
            for d2 in [
                PolicyDecision::Permit,
                PolicyDecision::Deny,
                PolicyDecision::Indeterminate,
            ] {
                assert_eq!(d1.and_policy(d2), d2.and_policy(d1));
            }
        }
    }

    #[test]
    fn test_or_policy_comm() {
        // Corresponds to Lean theorem: or_policy_comm
        for d1 in [
            PolicyDecision::Permit,
            PolicyDecision::Deny,
            PolicyDecision::Indeterminate,
        ] {
            for d2 in [
                PolicyDecision::Permit,
                PolicyDecision::Deny,
                PolicyDecision::Indeterminate,
            ] {
                assert_eq!(d1.or_policy(d2), d2.or_policy(d1));
            }
        }
    }

    #[test]
    fn test_and_policy_assoc() {
        // Corresponds to Lean theorem: and_policy_assoc
        for d1 in [
            PolicyDecision::Permit,
            PolicyDecision::Deny,
            PolicyDecision::Indeterminate,
        ] {
            for d2 in [
                PolicyDecision::Permit,
                PolicyDecision::Deny,
                PolicyDecision::Indeterminate,
            ] {
                for d3 in [
                    PolicyDecision::Permit,
                    PolicyDecision::Deny,
                    PolicyDecision::Indeterminate,
                ] {
                    assert_eq!(
                        d1.and_policy(d2).and_policy(d3),
                        d1.and_policy(d2.and_policy(d3))
                    );
                }
            }
        }
    }

    #[test]
    fn test_not_policy_involutive() {
        // Corresponds to Lean theorem: not_policy_involutive
        for d in [
            PolicyDecision::Permit,
            PolicyDecision::Deny,
            PolicyDecision::Indeterminate,
        ] {
            assert_eq!(d.not_policy().not_policy(), d);
        }
    }

    #[test]
    fn test_not_policy_values() {
        // Corresponds to Lean theorems: not_policy_permit, not_policy_deny, not_policy_indeterminate
        assert_eq!(PolicyDecision::Permit.not_policy(), PolicyDecision::Deny);
        assert_eq!(PolicyDecision::Deny.not_policy(), PolicyDecision::Permit);
        assert_eq!(
            PolicyDecision::Indeterminate.not_policy(),
            PolicyDecision::Indeterminate
        );
    }

    #[test]
    fn test_override_policy_indeterminate_left() {
        // Corresponds to Lean theorem: override_policy_indeterminate_left
        for d in [
            PolicyDecision::Permit,
            PolicyDecision::Deny,
            PolicyDecision::Indeterminate,
        ] {
            assert_eq!(PolicyDecision::Indeterminate.override_policy(d), d);
        }
    }

    #[test]
    fn test_override_policy_determinate_absorbs() {
        // Corresponds to Lean theorem: override_policy_determinate_absorbs
        for d1 in [PolicyDecision::Permit, PolicyDecision::Deny] {
            for d2 in [
                PolicyDecision::Permit,
                PolicyDecision::Deny,
                PolicyDecision::Indeterminate,
            ] {
                assert_eq!(d1.override_policy(d2), d1);
            }
        }
    }

    #[test]
    fn test_implies_policy_values() {
        // Corresponds to Lean theorems: implies_policy_permit_left, implies_policy_deny_left, implies_policy_indeterminate_left
        for d in [
            PolicyDecision::Permit,
            PolicyDecision::Deny,
            PolicyDecision::Indeterminate,
        ] {
            assert_eq!(PolicyDecision::Permit.implies_policy(d), d);
            assert_eq!(
                PolicyDecision::Deny.implies_policy(d),
                PolicyDecision::Permit
            );
            assert_eq!(
                PolicyDecision::Indeterminate.implies_policy(d),
                PolicyDecision::Indeterminate
            );
        }
    }

    #[test]
    fn test_demorgan_laws() {
        // Corresponds to Lean theorems: not_and_demorgan, not_or_demorgan
        for p in [
            PolicyDecision::Permit,
            PolicyDecision::Deny,
            PolicyDecision::Indeterminate,
        ] {
            for q in [
                PolicyDecision::Permit,
                PolicyDecision::Deny,
                PolicyDecision::Indeterminate,
            ] {
                // NOT (p AND q) = (NOT p) OR (NOT q)
                assert_eq!(
                    p.and_policy(q).not_policy(),
                    p.not_policy().or_policy(q.not_policy())
                );
                // NOT (p OR q) = (NOT p) AND (NOT q)
                assert_eq!(
                    p.or_policy(q).not_policy(),
                    p.not_policy().and_policy(q.not_policy())
                );
            }
        }
    }

    #[test]
    fn test_policy_check_fail_closed() {
        // Corresponds to Lean theorem: policy_check_fail_closed
        let policy = PolicyState::from_decision(PolicyDecision::Indeterminate);
        let action = make_test_action();
        let ctx = make_test_ctx();

        let result = policy.check(&action, &ctx);
        assert_ne!(result, PolicyDecision::Indeterminate);
        assert_eq!(result, PolicyDecision::Deny); // Fail-closed
    }

    #[test]
    fn test_policy_check_sound() {
        // Corresponds to Lean theorem: policy_check_sound
        for decision in [
            PolicyDecision::Permit,
            PolicyDecision::Deny,
            PolicyDecision::Indeterminate,
        ] {
            let policy = PolicyState::from_decision(decision);
            let action = make_test_action();
            let ctx = make_test_ctx();

            let result = policy.check(&action, &ctx);
            assert!(result == PolicyDecision::Permit || result == PolicyDecision::Deny);
        }
    }

    #[test]
    fn test_deny_all_policy() {
        let policy = PolicyState::deny_all();
        let action = make_test_action();
        let ctx = make_test_ctx();

        assert_eq!(policy.eval(&action, &ctx), PolicyDecision::Deny);
    }

    #[test]
    fn test_permit_all_policy() {
        let policy = PolicyState::permit_all();
        let action = make_test_action();
        let ctx = make_test_ctx();

        assert_eq!(policy.eval(&action, &ctx), PolicyDecision::Permit);
    }

    #[test]
    fn test_policy_decision_fn_custom_policy() {
        let decide = PolicyDecisionFn::Custom(PolicyDecision::Indeterminate);
        let action = make_test_action();
        let ctx = make_test_ctx();

        assert_eq!(decide.eval(&action, &ctx), PolicyDecision::Indeterminate);
    }

    #[test]
    fn test_action_validation() {
        // Empty kind should fail
        let result = Action::new(1, 2, Rights::empty(), String::new());
        assert!(result.is_err());

        // Valid action should succeed
        let result = Action::new(1, 2, Rights::singleton(Right::Read), "read".into());
        assert!(result.is_ok());
    }

    #[test]
    fn test_policy_decision_from_str() {
        assert_eq!(
            "permit".parse::<PolicyDecision>().unwrap(),
            PolicyDecision::Permit
        );
        assert_eq!(
            "DENY".parse::<PolicyDecision>().unwrap(),
            PolicyDecision::Deny
        );
        assert_eq!(
            "Indeterminate".parse::<PolicyDecision>().unwrap(),
            PolicyDecision::Indeterminate
        );
        assert!("invalid".parse::<PolicyDecision>().is_err());
    }

    #[test]
    fn test_policy_decision_from_str_whitespace() {
        assert_eq!(
            " permit ".parse::<PolicyDecision>().unwrap(),
            PolicyDecision::Permit
        );
        assert_eq!(
            "deny\n".parse::<PolicyDecision>().unwrap(),
            PolicyDecision::Deny
        );
        assert_eq!(
            "\tindeterminate\t".parse::<PolicyDecision>().unwrap(),
            PolicyDecision::Indeterminate
        );
    }
}
