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
