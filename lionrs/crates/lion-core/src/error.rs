// Copyright (C) 2026 HaiyangLi
// SPDX-License-Identifier: Apache-2.0
//! Unified error type for lion-core operations.

/// Unified error type for lion-core operations.
///
/// Collects all domain error types so callers only need
/// to handle a single error type.
#[derive(Debug)]
pub enum Error {
    /// Capability operation error (invalid holder, empty rights, etc.)
    Capability(crate::CapabilityError),

    /// Policy operation error
    Policy(crate::PolicyError),

    /// Rights operation error
    Rights(crate::RightsError),

    /// Step execution error
    Step(crate::StepError),

    /// State operation error
    State(crate::state::StateError),

    /// Kernel operation error
    Kernel(crate::state::KernelError),
}

impl std::fmt::Display for Error {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Error::Capability(e) => write!(f, "capability: {e}"),
            Error::Policy(e) => write!(f, "policy: {e}"),
            Error::Rights(e) => write!(f, "rights: {e}"),
            Error::Step(e) => write!(f, "step: {e}"),
            Error::State(e) => write!(f, "state: {e}"),
            Error::Kernel(e) => write!(f, "kernel: {e}"),
        }
    }
}

#[cfg(not(extract))]
impl std::error::Error for Error {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Error::Capability(source) => Some(source),
            Error::Policy(source) => Some(source),
            _ => None,
        }
    }
}

impl From<crate::CapabilityError> for Error {
    fn from(e: crate::CapabilityError) -> Self {
        Error::Capability(e)
    }
}

impl From<crate::PolicyError> for Error {
    fn from(e: crate::PolicyError) -> Self {
        Error::Policy(e)
    }
}

impl From<crate::RightsError> for Error {
    fn from(e: crate::RightsError) -> Self {
        Error::Rights(e)
    }
}

impl From<crate::StepError> for Error {
    fn from(e: crate::StepError) -> Self {
        Error::Step(e)
    }
}

impl From<crate::state::StateError> for Error {
    fn from(e: crate::state::StateError) -> Self {
        Error::State(e)
    }
}

impl From<crate::state::KernelError> for Error {
    fn from(e: crate::state::KernelError) -> Self {
        Error::Kernel(e)
    }
}
