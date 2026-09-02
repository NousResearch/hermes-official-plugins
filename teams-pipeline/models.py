"""Normalized models for the Teams meeting pipeline plugin."""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
from typing import Any, Callable, ClassVar, Literal


ArtifactType = Literal["transcript", "recording", "call_record"]


def _parse_datetime(value: Any) -> datetime | None:
    """Parse ISO-8601 (``Z`` accepted); naive values are assumed UTC, aware values keep their offset."""
    if value is None or isinstance(value, datetime):
        return value
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    parsed = datetime.fromisoformat(text)
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def _serialize_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(part.title() for part in rest)


def _pick(payload: dict[str, Any], *keys: str) -> Any:
    """Equivalent of ``payload.get(k1) or payload.get(k2) or ...`` (returns the last value when all are falsy)."""
    value = None
    for key in keys:
        value = payload.get(key)
        if value:
            return value
    return value


def _str(value: Any) -> str:
    return str(value or "").strip()


def _list(value: Any) -> list[Any]:
    return list(value or [])


def _dict(value: Any) -> dict[str, Any]:
    return dict(value or {})


def _nested(model: type["_Model"]) -> Callable[[Any], Any]:
    return lambda value: model.from_dict(value) if value else None


class _Model:
    """Shared snake/camelCase ``from_dict`` and None-dropping ``to_dict`` for the dataclasses below.

    ``_ALIASES`` overrides the default ``(snake_name, camelName)`` lookup keys; ``_CONVERT`` post-processes
    the picked raw value; ``_REQUIRED`` string fields must be non-blank; ``_DATETIMES`` are parsed in place.
    """

    _ALIASES: ClassVar[dict[str, tuple[str, ...]]] = {}
    _CONVERT: ClassVar[dict[str, Callable[[Any], Any]]] = {}
    _REQUIRED: ClassVar[tuple[str, ...]] = ()
    _DATETIMES: ClassVar[tuple[str, ...]] = ()

    @classmethod
    def from_dict(cls, payload: dict[str, Any]):
        kwargs = {}
        for spec in fields(cls):
            value = _pick(payload, *cls._ALIASES.get(spec.name, (spec.name, _camel(spec.name))))
            convert = cls._CONVERT.get(spec.name)
            kwargs[spec.name] = convert(value) if convert else value
        return cls(**kwargs)

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for spec in fields(self):
            value = getattr(self, spec.name)
            if isinstance(value, datetime):
                value = _serialize_datetime(value)
            elif isinstance(value, list):
                value = [item.to_dict() if isinstance(item, _Model) else item for item in value] or None
            elif isinstance(value, dict):
                value = value or None
            elif isinstance(value, _Model):
                value = value.to_dict()
            if value is not None:
                result[spec.name] = value
        return result

    def __post_init__(self) -> None:
        for name in self._REQUIRED:
            if not getattr(self, name).strip():
                raise ValueError(f"{type(self).__name__}.{name} is required.")
        for name in self._DATETIMES:
            setattr(self, name, _parse_datetime(getattr(self, name)))


@dataclass
class GraphSubscription(_Model):
    subscription_id: str
    resource: str
    change_type: str
    notification_url: str
    expiration_datetime: datetime
    client_state: str | None = None
    latest_renewal_at: datetime | None = None
    status: str | None = None

    _ALIASES = {
        "subscription_id": ("subscription_id", "id"),
        "expiration_datetime": ("expiration_datetime", "expirationDateTime"),
    }
    _REQUIRED = ("subscription_id", "resource", "change_type", "notification_url")
    _CONVERT = dict.fromkeys(_REQUIRED, _str)
    _DATETIMES = ("expiration_datetime", "latest_renewal_at")

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.expiration_datetime is None:
            raise ValueError("GraphSubscription.expiration_datetime is required.")


@dataclass
class TeamsMeetingRef(_Model):
    meeting_id: str
    organizer_user_id: str | None = None
    join_web_url: str | None = None
    calendar_event_id: str | None = None
    thread_id: str | None = None
    tenant_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    _ALIASES = {"meeting_id": ("meeting_id", "id")}
    _CONVERT = {"meeting_id": _str, "metadata": _dict}
    _REQUIRED = ("meeting_id",)


@dataclass
class MeetingArtifact(_Model):
    artifact_type: ArtifactType
    artifact_id: str
    display_name: str | None = None
    content_type: str | None = None
    source_url: str | None = None
    download_url: str | None = None
    created_at: datetime | None = None
    available_at: datetime | None = None
    size_bytes: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    _ALIASES = {
        "artifact_id": ("artifact_id", "id"),
        "display_name": ("display_name", "displayName", "name"),
        "source_url": ("source_url", "sourceUrl", "webUrl"),
        "download_url": ("download_url", "downloadUrl", "@microsoft.graph.downloadUrl"),
        "created_at": ("created_at", "createdDateTime"),
        "available_at": ("available_at", "availableDateTime", "lastModifiedDateTime"),
        "size_bytes": ("size_bytes", "size"),
    }
    _CONVERT = {"artifact_id": _str, "metadata": _dict}
    _REQUIRED = ("artifact_id",)
    _DATETIMES = ("created_at", "available_at")

    def __post_init__(self) -> None:
        if self.artifact_type not in {"transcript", "recording", "call_record"}:
            raise ValueError("MeetingArtifact.artifact_type must be transcript, recording, or call_record.")
        super().__post_init__()
        if self.size_bytes is not None:
            self.size_bytes = int(self.size_bytes)


@dataclass
class TeamsMeetingSummaryPayload(_Model):
    meeting_ref: TeamsMeetingRef
    title: str | None = None
    start_time: datetime | None = None
    end_time: datetime | None = None
    participants: list[str] = field(default_factory=list)
    transcript_text: str | None = None
    summary: str | None = None
    key_decisions: list[str] = field(default_factory=list)
    action_items: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    call_metrics: dict[str, Any] = field(default_factory=dict)
    source_artifacts: list[MeetingArtifact] = field(default_factory=list)
    confidence: str | None = None
    confidence_notes: str | None = None
    notion_target: str | None = None
    linear_target: str | None = None
    teams_target: str | None = None

    # Nested payloads are only read from their snake_case keys.
    _ALIASES = {"meeting_ref": ("meeting_ref",), "source_artifacts": ("source_artifacts",)}
    _CONVERT = {
        **dict.fromkeys(("participants", "key_decisions", "action_items", "risks"), _list),
        "call_metrics": _dict,
        "meeting_ref": TeamsMeetingRef.from_dict,
        "source_artifacts": lambda value: [MeetingArtifact.from_dict(item) for item in value or []],
    }
    _DATETIMES = ("start_time", "end_time")

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "TeamsMeetingSummaryPayload":
        # meeting_ref is mandatory: a missing key raises KeyError rather than building a half-empty payload.
        return super().from_dict({**payload, "meeting_ref": payload["meeting_ref"]})


@dataclass
class TeamsMeetingPipelineJob(_Model):
    job_id: str
    event_id: str
    source_event_type: str
    dedupe_key: str
    status: str
    retry_count: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None
    meeting_ref: TeamsMeetingRef | None = None
    selected_artifact_strategy: str | None = None
    summary_payload: TeamsMeetingSummaryPayload | None = None
    error_info: dict[str, Any] = field(default_factory=dict)

    _REQUIRED = ("job_id", "event_id", "source_event_type", "dedupe_key", "status")
    _CONVERT = {
        **dict.fromkeys(_REQUIRED, _str),
        "retry_count": lambda value: value or 0,
        "meeting_ref": _nested(TeamsMeetingRef),
        "summary_payload": _nested(TeamsMeetingSummaryPayload),
        "error_info": _dict,
    }
    _DATETIMES = ("created_at", "updated_at")

    def __post_init__(self) -> None:
        super().__post_init__()
        self.retry_count = int(self.retry_count)


__all__ = [
    "ArtifactType", "GraphSubscription", "MeetingArtifact",
    "TeamsMeetingPipelineJob", "TeamsMeetingRef", "TeamsMeetingSummaryPayload",
]
