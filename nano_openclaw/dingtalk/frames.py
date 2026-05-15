"""Pydantic models for DingTalk Stream protocol frames.

The Stream protocol uses a single envelope shape for all inbound traffic:

- ``type``: ``"EVENT"`` | ``"CALLBACK"`` | ``"SYSTEM"``
- ``specVersion``: protocol version (always ``"1"`` today)
- ``headers``: routing metadata (topic, messageId, …)
- ``data``: payload as a JSON-encoded **string** — callers ``json.loads`` it
  themselves because some topics carry arbitrary blobs we don't want to parse
  twice.

Each frame requires a same-shape ACK back over the WebSocket; without it the
server retransmits.
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class FrameHeaders(BaseModel):
    """Headers shared by all inbound frame types.

    The set of fields that actually arrive depends on ``type``: ``CALLBACK``
    frames carry only the common fields, while ``EVENT`` frames also carry
    ``eventId`` / ``eventType`` / ``eventBornTime`` / ``eventCorpId`` /
    ``eventUnifiedAppId``. We accept extras because the server may add new
    fields without bumping the spec version.
    """

    model_config = ConfigDict(extra="allow")

    messageId: str
    topic: str
    appId: Optional[str] = None
    connectionId: Optional[str] = None
    contentType: Optional[str] = "application/json"
    time: Optional[str] = None
    eventId: Optional[str] = None
    eventType: Optional[str] = None
    eventBornTime: Optional[str] = None
    eventCorpId: Optional[str] = None
    eventUnifiedAppId: Optional[str] = None


class _BaseFrame(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: str
    specVersion: str = "1"
    headers: FrameHeaders
    data: str = ""


class EventFrame(_BaseFrame):
    type: Literal["EVENT"] = "EVENT"


class CallbackFrame(_BaseFrame):
    type: Literal["CALLBACK"] = "CALLBACK"


class SystemFrame(_BaseFrame):
    type: Literal["SYSTEM"] = "SYSTEM"


class AckFrame(BaseModel):
    """ACK envelope sent back for every inbound frame.

    ``code`` follows HTTP semantics — 200 success, 400 malformed, 404 not
    implemented, 500 internal exception. We always include the original
    ``messageId`` in ``headers`` so the server can correlate.
    """

    code: int = 200
    headers: dict[str, Any] = Field(default_factory=dict)
    message: str = "ok"
    data: str = "{}"


def parse_inbound(raw: dict[str, Any]) -> _BaseFrame:
    """Dispatch an inbound frame dict to the right subclass.

    Falls back to ``SystemFrame`` for unknown ``type`` values so unknown
    server-side message kinds still get ACKed and logged rather than crashing
    the client.
    """
    t = raw.get("type")
    if t == "EVENT":
        return EventFrame.model_validate(raw)
    if t == "CALLBACK":
        return CallbackFrame.model_validate(raw)
    return SystemFrame.model_validate(raw)


def make_ack(frame: _BaseFrame, *, code: int = 200, message: str = "ok") -> AckFrame:
    """Build the ACK that must be sent for ``frame``."""
    return AckFrame(
        code=code,
        headers={"messageId": frame.headers.messageId, "contentType": "application/json"},
        message=message,
        data="{}",
    )
