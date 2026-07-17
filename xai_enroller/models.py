from dataclasses import dataclass
from enum import Enum
from typing import Optional


@dataclass(frozen=True)
class SourceRecord:
    source_id: str
    sso_token: str
    cookies: tuple[dict, ...] = ()

    def __repr__(self) -> str:
        return "SourceRecord(<redacted>)"


@dataclass(frozen=True)
class DeviceFlow:
    device_code: str
    user_code: str
    verification_url: str
    expires_in: int
    interval: float
    token_endpoint: str = "https://auth.x.ai/oauth2/token"

    def __repr__(self) -> str:
        return "DeviceFlow(<redacted>)"


@dataclass(frozen=True)
class OAuthCredential:
    access_token: str
    refresh_token: str
    id_token: Optional[str]
    token_type: Optional[str]
    expires_in: int
    expires_at: str
    last_refresh: str
    subject: Optional[str]
    token_endpoint: str

    def __repr__(self) -> str:
        return "OAuthCredential(<redacted>)"


@dataclass(frozen=True)
class SinkReceipt:
    fingerprint: str

    def __repr__(self) -> str:
        return "SinkReceipt(<redacted>)"


class AuthorizationStatus(str, Enum):
    AUTHORIZED = "authorized"
    NEEDS_BROWSER = "needs_browser"
    NEEDS_INTERACTION = "needs_interaction"


@dataclass(frozen=True)
class AuthorizationResult:
    status: AuthorizationStatus
    reason_code: str


class JobStatus(str, Enum):
    IMPORTED = "imported"
    NEEDS_BROWSER = "needs_browser"
    NEEDS_INTERACTION = "needs_interaction"
    SOURCE_INVALID = "source_invalid"
    OAUTH_DENIED = "oauth_denied"
    OAUTH_EXPIRED = "oauth_expired"
    OAUTH_REJECTED = "oauth_rejected"
    SINK_FAILED = "sink_failed"
    TRANSPORT_FAILED = "transport_failed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class JobResult:
    source_id: str
    status: JobStatus
    reason_code: str
    attempt_number: int = 1
    sink_receipt_fingerprint: Optional[str] = None

    def __repr__(self) -> str:
        return (
            f"JobResult(source_id=<redacted>, status={self.status.value!r}, "
            f"reason_code={self.reason_code!r}, attempt_number={self.attempt_number})"
        )


class PipelineState(str, Enum):
    QUEUED = "queued"
    PREPARED = "prepared"
    ACTIVE = "active"
    RETRY_WAITING = "retry_waiting"
    IMPORTED = "imported"
    TERMINAL = "terminal"


@dataclass(frozen=True)
class PreparedJob:
    source: SourceRecord
    source_fingerprint: str
    flow: DeviceFlow
    flow_created_monotonic: float
    job_id: int
    attempt_number: int
    task_number: Optional[int] = None

    def __repr__(self) -> str:
        return (
            "PreparedJob(source=<redacted>, flow=<redacted>, "
            f"attempt_number={self.attempt_number})"
        )


@dataclass(frozen=True)
class CompletionJob:
    prepared: PreparedJob

    def __repr__(self) -> str:
        return (
            "CompletionJob(source=<redacted>, flow=<redacted>, "
            f"attempt_number={self.prepared.attempt_number})"
        )
