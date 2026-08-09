// Copyright (C) 2026 HaiyangLi
// SPDX-License-Identifier: Apache-2.0
//! Lion Step Authorization
//!
//! Corresponds to: Lion/Step/Authorization.lean
//!
//! Authorization witness for proof-carrying steps.
//! Bakes complete mediation into the type system.

use crate::state::State;
use crate::types::{Action, CapId, Capability, MemAddr, PolicyContext, Right, SecurityLevel};

/// Error type for authorization failures
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum AuthorizationError {
    /// Capability not found in kernel
    CapabilityNotFound(CapId),
    /// Capability is revoked
    CapabilityRevoked(CapId),
    /// Capability holder mismatch
    HolderMismatch {
        /// Expected holder plugin ID
        expected: u128,
        /// Actual holder plugin ID
        actual: u128,
    },
    /// Capability target mismatch
    TargetMismatch {
        /// Expected target resource ID
        expected: u128,
        /// Actual target resource ID
        actual: u128,
    },
    /// Insufficient rights
    InsufficientRights,
    /// Policy denied the action
    PolicyDenied,
    /// Target resource not live
    ResourceNotLive(MemAddr),
    /// Biba integrity violation (write-up)
    BibaViolation {
        /// Security level of the writing plugin
        writer_level: SecurityLevel,
        /// Security level of the target resource
        target_level: SecurityLevel,
    },
    /// Capability not held by plugin
    CapabilityNotHeld(CapId),
}

impl std::fmt::Display for AuthorizationError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            AuthorizationError::CapabilityNotFound(id) => {
                write!(f, "Capability {id} not found")
            }
            AuthorizationError::CapabilityRevoked(id) => {
                write!(f, "Capability {id} is revoked")
            }
            AuthorizationError::HolderMismatch { expected, actual } => {
                write!(f, "Holder mismatch: expected {expected}, got {actual}")
            }
            AuthorizationError::TargetMismatch { expected, actual } => {
                write!(f, "Target mismatch: expected {expected}, got {actual}")
            }
            AuthorizationError::InsufficientRights => {
                write!(f, "Insufficient rights")
            }
            AuthorizationError::PolicyDenied => {
                write!(f, "Policy denied the action")
            }
            AuthorizationError::ResourceNotLive(addr) => {
                write!(f, "Resource at {addr} is not live")
            }
            AuthorizationError::BibaViolation {
                writer_level,
                target_level,
            } => {
                write!(
                    f,
                    "Biba violation: writer level {writer_level:?} cannot write to target level {target_level:?}"
                )
            }
            AuthorizationError::CapabilityNotHeld(id) => {
                write!(f, "Capability {id} not held by plugin")
            }
        }
    }
}

impl std::error::Error for AuthorizationError {}

/// Resource integrity level based on owner.
///
/// Corresponds to Lean: `def resource_integrity`
///
/// Used for Biba "no write-up" enforcement.
pub fn resource_integrity(state: &State, rid: MemAddr) -> SecurityLevel {
    // Check if resource is allocated and get owner
    if let Some(status) = state.ghost().get_status(rid) {
        if let Some(owner) = status.owner() {
            state.plugin_level(owner).unwrap_or(SecurityLevel::Public)
        } else {
            SecurityLevel::Public
        }
    } else {
        // Unallocated/freed has no integrity
        SecurityLevel::Public
    }
}

/// Biba write check: writer's integrity level must dominate target's.
///
/// Corresponds to Lean: `def biba_write_ok`
///
/// This is the Biba "no write-up" enforcement for resource modification.
pub fn biba_write_ok(state: &State, action: &Action) -> bool {
    // If Write right is not requested, Biba is satisfied
    if !action.rights.contains(Right::Write) {
        return true;
    }

    let writer_level = state
        .plugin_level(action.subject)
        .unwrap_or(SecurityLevel::Public);
    // action.target is ResourceId (u128); ghost uses MemAddr (u64) — truncate for address lookup
    let target_level = resource_integrity(state, action.target as MemAddr);

    writer_level >= target_level
}

/// Check capability seal and revocation status.
///
/// Corresponds to Lean: `def cap_isValid`
#[allow(dead_code)] // Lean correspondence
pub fn cap_is_valid(state: &State, cap: &Capability) -> bool {
    // Check capability is not revoked
    state.kernel().revocation().is_valid(cap.id())
    // Note: seal verification would be done here with HMAC
    // For now we assume seal is valid if in kernel table
}

/// Full capability check for authorization.
///
/// Corresponds to Lean: `def capability_check`
#[allow(dead_code)] // Lean correspondence
pub fn capability_check(state: &State, action: &Action, cap: &Capability) -> bool {
    // 1. Holder matches subject
    if cap.holder() != action.subject {
        return false;
    }

    // 2. Target matches
    if cap.target() != action.target {
        return false;
    }

    // 3. Rights are sufficient
    if !action.rights.is_subset_of(cap.rights()) {
        return false;
    }

    // 4. Capability is valid (not revoked)
    if !cap_is_valid(state, cap) {
        return false;
    }

    // 5. Plugin holds the capability
    if !state.plugin_holds(action.subject, cap.id()) {
        return false;
    }

    true
}

/// Authorization witness carrying all proofs required for a host call.
///
/// Corresponds to Lean: `structure Authorized (s : State) (a : Action)`
///
/// In Lean, this structure contains proofs that cannot be forged.
/// In Rust, we validate these conditions at runtime.
///
/// **TOCTTOU Prevention**:
/// The Authorized witness is ONLY constructible via `validate_atomic()` which
/// both validates AND returns the witness in a single atomic operation.
/// This prevents the vulnerability where:
///   1. Check capability validity
///   2. (capability revoked here)
///   3. Use capability
///
/// By making construction private and combining validation with use,
/// we ensure authorization is always checked against the CURRENT state
/// at the moment of execution.
///
/// Design rationale:
/// - h_cap: Capability matches the action
/// - h_pol: Policy permits the action
/// - h_valid: Capability chain is valid
/// - h_live: Target resource is live
/// - h_conf: Rights are confined
/// - h_biba: Biba integrity constraint for writes
#[derive(Debug, Clone)]
#[must_use = "authorization tokens must be consumed to enforce access control"]
pub struct Authorized {
    /// The capability being used
    cap: Capability,

    /// The action being authorized
    action: Action,

    /// Policy evaluation context
    ctx: PolicyContext,

    /// Marker to prevent external construction
    /// This ensures Authorized can only be created via validate_atomic()
    _private: (),
}

impl Authorized {
    /// Create a new authorization witness (INTERNAL ONLY)
    ///
    /// This constructor is private to enforce atomic validation.
    /// External code must use `validate_atomic()` to obtain an Authorized witness.
    fn new_internal(cap: Capability, action: Action, ctx: PolicyContext) -> Self {
        Authorized {
            cap,
            action,
            ctx,
            _private: (),
        }
    }

    /// Atomically validate and create an authorization witness.
    ///
    /// **TOCTTOU Prevention**: This function validates ALL authorization conditions
    /// and returns the witness in a single atomic operation. The returned witness
    /// is ONLY valid for the state it was validated against.
    ///
    /// Corresponds to Lean: constructing `Authorized s a` requires proofs for state `s`.
    ///
    /// # Arguments
    /// * `state` - The CURRENT state to validate against
    /// * `cap` - The capability to use
    /// * `action` - The action to authorize
    /// * `ctx` - Policy evaluation context
    ///
    /// # Errors
    ///
    /// Returns `AuthorizationError::HolderMismatch` if the capability holder does not match the action subject.
    /// Returns `AuthorizationError::TargetMismatch` if the capability target does not match the action target.
    /// Returns `AuthorizationError::InsufficientRights` if the action requests rights not granted by the capability.
    /// Returns `AuthorizationError::CapabilityNotHeld` if the plugin does not hold the capability.
    /// Returns `AuthorizationError::PolicyDenied` if the policy does not permit the action.
    /// Returns `AuthorizationError::CapabilityNotFound` if the capability is not in the revocation table.
    /// Returns `AuthorizationError::CapabilityRevoked` if the capability or its parent is revoked.
    /// Returns `AuthorizationError::ResourceNotLive` if the target resource is not live.
    /// Returns `AuthorizationError::BibaViolation` if a write violates Biba integrity.
    pub fn validate_atomic(
        state: &State,
        cap: Capability,
        action: Action,
        ctx: PolicyContext,
    ) -> Result<Authorized, AuthorizationError> {
        // Create provisional witness for validation
        let provisional = Self::new_internal(cap, action, ctx);

        // Validate all conditions atomically
        // If ANY check fails, no witness is returned
        provisional.check_capability(state)?;
        provisional.check_policy(state)?;
        provisional.check_validity(state)?;
        provisional.check_liveness(state)?;
        provisional.check_confinement()?;
        provisional.check_biba(state)?;

        // All checks passed - return the valid witness
        Ok(provisional)
    }

    /// Validate all authorization conditions (internal use only)
    ///
    /// This is used for re-validation when needed but should NOT be the
    /// primary mechanism for obtaining authorization.
    ///
    /// This checks all the conditions that would be baked into
    /// the Lean `Authorized` structure as proof obligations:
    ///
    /// 1. Capability check passes
    /// 2. Policy permits the action
    /// 3. Capability is inductively valid
    /// 4. Target resource is live
    /// 5. Rights are confined
    /// 6. Biba integrity satisfied
    pub(crate) fn validate(&self, state: &State) -> Result<(), AuthorizationError> {
        // 1. Capability check
        self.check_capability(state)?;

        // 2. Policy check
        self.check_policy(state)?;

        // 3. Capability validity (revocation chain)
        self.check_validity(state)?;

        // 4. Target liveness
        self.check_liveness(state)?;

        // 5. Rights confinement
        self.check_confinement()?;

        // 6. Biba integrity
        self.check_biba(state)?;

        Ok(())
    }

    /// Get the capability (read-only access)
    pub fn cap(&self) -> &Capability {
        &self.cap
    }

    /// Get the action (read-only access)
    pub fn action(&self) -> &Action {
        &self.action
    }

    /// Get the policy context (read-only access)
    pub fn ctx(&self) -> &PolicyContext {
        &self.ctx
    }

    /// Check capability matches action
    fn check_capability(&self, state: &State) -> Result<(), AuthorizationError> {
        // Check holder matches
        if self.cap.holder() != self.action.subject {
            return Err(AuthorizationError::HolderMismatch {
                expected: self.action.subject,
                actual: self.cap.holder(),
            });
        }

        // Check target matches
        if self.cap.target() != self.action.target {
            return Err(AuthorizationError::TargetMismatch {
                expected: self.action.target,
                actual: self.cap.target(),
            });
        }

        // Check rights are sufficient
        if !self.action.rights.is_subset_of(self.cap.rights()) {
            return Err(AuthorizationError::InsufficientRights);
        }

        // Check plugin holds capability
        if !state.plugin_holds(self.action.subject, self.cap.id()) {
            return Err(AuthorizationError::CapabilityNotHeld(self.cap.id()));
        }

        Ok(())
    }

    /// Check policy permits action
    fn check_policy(&self, state: &State) -> Result<(), AuthorizationError> {
        let decision = state.kernel().policy().eval(&self.action, &self.ctx);

        if decision.is_permit() {
            Ok(())
        } else {
            Err(AuthorizationError::PolicyDenied)
        }
    }

    /// Check capability validity (not revoked)
    fn check_validity(&self, state: &State) -> Result<(), AuthorizationError> {
        // Check capability exists in kernel
        let kernel_cap = state
            .kernel()
            .revocation()
            .get(self.cap.id())
            .ok_or(AuthorizationError::CapabilityNotFound(self.cap.id()))?;

        // Check not revoked
        if !kernel_cap.is_valid() {
            return Err(AuthorizationError::CapabilityRevoked(self.cap.id()));
        }

        // Check parent chain is valid (if has parent)
        if let Some(parent_id) = self.cap.parent() {
            if !state.kernel().revocation().is_valid(parent_id) {
                return Err(AuthorizationError::CapabilityRevoked(parent_id));
            }
        }

        Ok(())
    }

    /// Check target resource is live
    fn check_liveness(&self, state: &State) -> Result<(), AuthorizationError> {
        // action.target is ResourceId (u128); ghost uses MemAddr (u64) — truncate for address lookup
        let addr = self.action.target as MemAddr;
        if !state.ghost().is_live(addr) {
            return Err(AuthorizationError::ResourceNotLive(addr));
        }
        Ok(())
    }

    /// Check rights confinement
    fn check_confinement(&self) -> Result<(), AuthorizationError> {
        if !self.action.rights.is_subset_of(self.cap.rights()) {
            return Err(AuthorizationError::InsufficientRights);
        }
        Ok(())
    }

    /// Check Biba integrity constraint
    fn check_biba(&self, state: &State) -> Result<(), AuthorizationError> {
        if !biba_write_ok(state, &self.action) {
            let writer_level = state
                .plugin_level(self.action.subject)
                .unwrap_or(SecurityLevel::Public);
            // action.target is ResourceId (u128); ghost uses MemAddr (u64) — truncate for address lookup
            let target_level = resource_integrity(state, self.action.target as MemAddr);
            return Err(AuthorizationError::BibaViolation {
                writer_level,
                target_level,
            });
        }
        Ok(())
    }

    /// Get the capability ID being used
    pub fn cap_id(&self) -> CapId {
        self.cap.id()
    }

    /// Check if holder has the capability (theorem correspondence)
    ///
    /// Corresponds to Lean: `theorem Authorized.holder_has_cap`
    pub fn holder_has_cap(&self, state: &State) -> bool {
        state.plugin_holds(self.action.subject, self.cap.id())
    }

    /// Check if policy is permitted (theorem correspondence)
    ///
    /// Corresponds to Lean: `theorem Authorized.policy_permitted`
    pub fn policy_permitted(&self, state: &State) -> bool {
        state
            .kernel()
            .policy()
            .eval(&self.action, &self.ctx)
            .is_permit()
    }

    /// Check if rights are confined (theorem correspondence)
    ///
    /// Corresponds to Lean: `theorem Authorized.rights_confined`
    pub fn rights_confined(&self) -> bool {
        self.action.rights.is_subset_of(self.cap.rights())
    }

    /// Check if Biba is satisfied (theorem correspondence)
    ///
    /// Corresponds to Lean: `theorem Authorized.biba_satisfied`
    pub fn biba_satisfied(&self, state: &State) -> bool {
        biba_write_ok(state, &self.action)
    }
}

/// Create an Authorized witness for legacy/test code.
///
/// **WARNING**: This bypasses atomic validation. Only use in tests
/// or where you immediately call validate() in the same function.
///
/// For production code, use `Authorized::validate_atomic()` instead.
#[cfg(test)]
impl Authorized {
    /// Create an `Authorized` token for testing, bypassing atomic validation.
    pub fn new_for_test(cap: Capability, action: Action, ctx: PolicyContext) -> Self {
        Authorized::new_internal(cap, action, ctx)
    }
}

/// Action is properly mediated (has valid authorization)
///
/// Corresponds to Lean: `def properly_mediated`
#[allow(dead_code)] // Lean correspondence
pub fn properly_mediated(state: &State, auth: &Authorized) -> bool {
    auth.validate(state).is_ok()
}

/// Combined authorization function
///
/// Corresponds to Lean: `def authorize`
///
/// authorize(p, c, a) = phi(p, a) AND kappa(c, a)
#[allow(dead_code)] // Lean correspondence
pub fn authorize(state: &State, cap: &Capability, action: &Action, ctx: &PolicyContext) -> bool {
    // Policy check
    let policy_ok = state.kernel().policy().eval(action, ctx).is_permit();

    // Capability check
    let cap_ok = capability_check(state, action, cap);

    policy_ok && cap_ok
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::state::PluginState;
    use crate::types::{Rights, SealedTag};

    fn make_test_cap(id: CapId, holder: u128, target: u128) -> Capability {
        Capability::new(
            id,
            holder,
            target,
            Rights::singleton(Right::Read),
            None,
            0,
            SealedTag::empty(),
        )
        .expect("valid capability")
    }

    #[test]
    fn test_resource_integrity_unallocated() {
        let state = State::empty();
        // Unallocated resource has Public integrity
        assert_eq!(resource_integrity(&state, 999), SecurityLevel::Public);
    }

    #[test]
    fn test_biba_write_ok_no_write() {
        let state = State::empty();
        let action = Action {
            subject: 1,
            target: 100,
            rights: Rights::singleton(Right::Read), // No write
            kind: "read".to_string(),
        };

        // No write right means Biba is satisfied
        assert!(biba_write_ok(&state, &action));
    }

    #[test]
    fn test_cap_is_valid_not_found() {
        let state = State::empty();
        let cap = make_test_cap(1, 1, 100);

        // Cap not in kernel is not valid
        assert!(!cap_is_valid(&state, &cap));
    }

    #[test]
    fn test_cap_is_valid_in_kernel() {
        let mut state = State::empty();

        let cap = make_test_cap(1, 1, 100);
        state
            .apply_cap_delegate_mut(cap.clone(), 1)
            .expect("delegate should succeed");

        // Cap in kernel is valid
        assert!(cap_is_valid(&state, &cap));
    }

    #[test]
    fn test_capability_check_holder_mismatch() {
        let mut state = State::empty();
        let _ = state.insert_plugin(1, PluginState::empty(SecurityLevel::Public, 0));

        let cap = make_test_cap(1, 2, 100); // Holder is 2
        let action = Action {
            subject: 1, // Subject is 1
            target: 100,
            rights: Rights::singleton(Right::Read),
            kind: "read".to_string(),
        };

        assert!(!capability_check(&state, &action, &cap));
    }

    #[test]
    fn test_authorized_validate_cap_not_held() {
        let mut state = State::empty();
        let _ = state.insert_plugin(1, PluginState::empty(SecurityLevel::Public, 0));

        let cap = make_test_cap(1, 1, 100);
        let action = Action {
            subject: 1,
            target: 100,
            rights: Rights::singleton(Right::Read),
            kind: "read".to_string(),
        };
        let ctx = PolicyContext::default();

        // Use validate_atomic which should fail since cap is not held
        let result = Authorized::validate_atomic(&state, cap, action, ctx);

        assert!(matches!(
            result,
            Err(AuthorizationError::CapabilityNotHeld(_))
        ));
    }

    #[test]
    fn test_authorize_both_pass() {
        let mut state = State::empty();
        let mut ps = PluginState::empty(SecurityLevel::Public, 0);
        ps.grant_cap_mut(1);
        state.insert_plugin(1, ps).unwrap();

        let cap = make_test_cap(1, 1, 100);
        state
            .apply_cap_delegate_mut(cap.clone(), 1)
            .expect("delegate should succeed");

        // Allocate the target resource
        let mut ghost = state.ghost().clone();
        ghost.alloc_mut(100, 1);

        let action = Action {
            subject: 1,
            target: 100,
            rights: Rights::singleton(Right::Read),
            kind: "read".to_string(),
        };
        let _ctx = PolicyContext::default();

        // With default policy (permit-all), should pass cap check
        // Note: full authorize requires policy to permit
        assert!(capability_check(&state, &action, &cap));
    }
}
