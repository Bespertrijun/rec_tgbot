from __future__ import annotations

from enum import StrEnum


class BindingStatus(StrEnum):
    BOUND = "BOUND"
    UNBOUND = "UNBOUND"
    DISPUTED = "DISPUTED"


class UserStatus(StrEnum):
    ACTIVE = "ACTIVE"
    BANNED = "BANNED"


class BaselineStatus(StrEnum):
    UNKNOWN = "UNKNOWN"
    VERIFIED = "VERIFIED"
    NEEDS_REVIEW = "NEEDS_REVIEW"


class CycleStatus(StrEnum):
    INITIALIZING = "INITIALIZING"
    VERIFIED = "VERIFIED"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    EXPIRED = "EXPIRED"


class QuotaRevocationStatus(StrEnum):
    PENDING_REVOKE = "PENDING_REVOKE"
    REVOKED = "REVOKED"
    PENDING_RESTORE = "PENDING_RESTORE"
    RESTORED = "RESTORED"


class ManagedGroupStatus(StrEnum):
    PENDING = "PENDING"
    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"
    REJECTED = "REJECTED"


class GroupMembershipState(StrEnum):
    RESTRICT_PENDING = "RESTRICT_PENDING"
    MUTED = "MUTED"
    UNMUTE_PENDING = "UNMUTE_PENDING"
    ACTIVE = "ACTIVE"
    REMOVE_PENDING = "REMOVE_PENDING"
    REMOVED = "REMOVED"
    LEFT = "LEFT"
    EXEMPT = "EXEMPT"
