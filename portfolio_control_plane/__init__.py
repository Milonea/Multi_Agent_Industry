"""Fail-closed portfolio control-plane primitives.

The package deliberately has no network or host-platform dependencies. External
adapters are injected by callers so planning and validation can be exercised
without implicit writes.
"""

from .contracts import (
    CONTRACT_VERSION,
    STATUS_VALUES,
    ContractError,
    make_run_result,
    validate_run_result,
)
from .baseline import (
    BaselineError,
    DEFAULT_LARGE_FILE_THRESHOLD_BYTES,
    audit_manifest,
    audit_repository,
)
from .dependency import DependencyGraph, DependencyGraphError, validate_graph
from .documentation_proposals import (
    DocumentationProposal,
    apply_documentation_proposal,
    approve_documentation_proposal,
    build_documentation_proposal,
    build_documentation_proposals,
    load_documentation_proposal,
    save_documentation_proposals,
)
from .delegation import (
    DEFAULT_SILENCE_POLL_INTERVAL_SECONDS,
    DEFAULT_WATCHDOG_TIMEOUT_SECONDS,
    DelegationLifecycle,
    DelegationLifecycleError,
    LIFECYCLE_CONTRACT_VERSION,
    MAX_INTERRUPT_REQUESTS,
    advance_after_wait,
    validate_lifecycle_snapshot,
)
from .evidence import EvidenceReport
from .goal import (
    FixtureGoalExecutor,
    GoalExecutionError,
    GoalExecutionResult,
    GoalTask,
)
from .ledger import ProjectSnapshot, StateLedgerError, load_snapshot, validate_snapshot
from .observability import aggregate_observability
from .read_only_goal import (
    ReadOnlyGoalExecutor,
    ReadOnlyGoalResult,
    ReadOnlyGoalTarget,
)
from .reality import (
    LocalGitRealityScanner,
    LocalRepositorySnapshot,
    RealityScanner,
    ReadOnlyRepositoryClient,
)
from .retry import (
    DEFAULT_MAX_FALLBACK_ATTEMPTS,
    DEFAULT_MAX_TOTAL_ATTEMPTS,
    RUNTIME_STATES,
    RETRY_POLICY_VERSION,
    TRANSIENT_RUNTIME_STATES,
    TaskRetryError,
    TaskRetryState,
)
from .registry import (
    CapabilityRegistry,
    ValidationPolicyRegistry,
    default_capability_registry,
    default_validation_policy_registry,
)
from .selection import WorkSelectionEngine
from .workflow import build_cross_domain_plan, approve_plan, execute_plan

__all__ = [
    "CONTRACT_VERSION",
    "STATUS_VALUES",
    "ContractError",
    "make_run_result",
    "validate_run_result",
    "BaselineError",
    "DEFAULT_LARGE_FILE_THRESHOLD_BYTES",
    "audit_manifest",
    "audit_repository",
    "DependencyGraph",
    "DependencyGraphError",
    "validate_graph",
    "DocumentationProposal",
    "apply_documentation_proposal",
    "approve_documentation_proposal",
    "build_documentation_proposal",
    "build_documentation_proposals",
    "load_documentation_proposal",
    "save_documentation_proposals",
    "DEFAULT_SILENCE_POLL_INTERVAL_SECONDS",
    "DEFAULT_WATCHDOG_TIMEOUT_SECONDS",
    "DelegationLifecycle",
    "DelegationLifecycleError",
    "LIFECYCLE_CONTRACT_VERSION",
    "MAX_INTERRUPT_REQUESTS",
    "advance_after_wait",
    "validate_lifecycle_snapshot",
    "EvidenceReport",
    "FixtureGoalExecutor",
    "GoalExecutionError",
    "GoalExecutionResult",
    "GoalTask",
    "ProjectSnapshot",
    "StateLedgerError",
    "load_snapshot",
    "validate_snapshot",
    "aggregate_observability",
    "ReadOnlyGoalExecutor",
    "ReadOnlyGoalResult",
    "ReadOnlyGoalTarget",
    "RealityScanner",
    "ReadOnlyRepositoryClient",
    "LocalGitRealityScanner",
    "LocalRepositorySnapshot",
    "DEFAULT_MAX_FALLBACK_ATTEMPTS",
    "DEFAULT_MAX_TOTAL_ATTEMPTS",
    "RUNTIME_STATES",
    "RETRY_POLICY_VERSION",
    "TRANSIENT_RUNTIME_STATES",
    "TaskRetryError",
    "TaskRetryState",
    "CapabilityRegistry",
    "ValidationPolicyRegistry",
    "default_capability_registry",
    "default_validation_policy_registry",
    "WorkSelectionEngine",
    "build_cross_domain_plan",
    "approve_plan",
    "execute_plan",
]
