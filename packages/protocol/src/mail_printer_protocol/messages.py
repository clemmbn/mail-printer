"""WebSocket message types exchanged between the server and the Pi agent.

Every frame is a JSON text frame with a ``type`` field. This module is the
single definition of the wire format, used by both sides:

- ``hello`` (Pi -> server): first frame after connecting, carries the
  protocol version so the server can reject an outdated agent.
- ``print`` (server -> Pi): one print job, the ticket PNG as base64 plus a
  plain-text fallback used if image printing fails.
- ``ack`` / ``fail`` (Pi -> server): outcome of one print job.

Constraints:
- STDLIB ONLY (installed on both machines).
- Bump ``PROTOCOL_VERSION`` on any breaking change to these shapes.
- ``decode`` never trusts its input: any malformed frame raises
  ``ProtocolError`` rather than a KeyError/TypeError deep in the caller.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields
from typing import Any, ClassVar

PROTOCOL_VERSION = 1


class ProtocolError(ValueError):
    """Raised when a frame is not valid JSON or not a known, well-formed message."""


@dataclass(frozen=True)
class Hello:
    """Pi -> server handshake.

    Attributes:
        protocol_version: the agent's ``PROTOCOL_VERSION``.
    """

    TYPE: ClassVar[str] = "hello"
    protocol_version: int


@dataclass(frozen=True)
class Print:
    """Server -> Pi print job.

    Attributes:
        job_id: the message id in the server DB; echoed back in ack/fail.
        png_b64: the rendered ticket PNG, base64-encoded.
        fallback_text: timestamp + name + message (never the contact field),
            printed as plain text if image printing fails.
    """

    TYPE: ClassVar[str] = "print"
    job_id: int
    png_b64: str
    fallback_text: str


@dataclass(frozen=True)
class Ack:
    """Pi -> server: job ``job_id`` was printed."""

    TYPE: ClassVar[str] = "ack"
    job_id: int


@dataclass(frozen=True)
class Fail:
    """Pi -> server: job ``job_id`` could not be printed, with a human-readable ``error``."""

    TYPE: ClassVar[str] = "fail"
    job_id: int
    error: str


Message = Hello | Print | Ack | Fail

# Lookup table used by `decode` to go from the wire "type" to a class.
_MESSAGE_TYPES: dict[str, type[Message]] = {cls.TYPE: cls for cls in (Hello, Print, Ack, Fail)}


def encode(message: Message) -> str:
    """Serialise a message to a JSON text frame.

    Args:
        message (Message): any of Hello / Print / Ack / Fail.

    Returns:
        str: compact JSON with a leading ``type`` field.
    """
    return json.dumps({"type": message.TYPE, **asdict(message)}, separators=(",", ":"))


def decode(frame: str | bytes) -> Message:
    """Parse and validate a JSON text frame into a message object.

    Args:
        frame (str | bytes): the raw WebSocket frame payload.

    Returns:
        Message: the matching dataclass instance.

    Raises:
        ProtocolError: invalid JSON, not an object, unknown ``type``, missing
            or unexpected fields, or a field with the wrong type.
    """
    try:
        data = json.loads(frame)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ProtocolError(f"invalid JSON frame: {exc}") from exc
    if not isinstance(data, dict):
        raise ProtocolError("frame must be a JSON object")

    msg_type = data.pop("type", None)
    cls = _MESSAGE_TYPES.get(msg_type) if isinstance(msg_type, str) else None
    if cls is None:
        raise ProtocolError(f"unknown message type: {msg_type!r}")

    return cls(**_validated_fields(cls, data))


def _validated_fields(cls: type[Message], data: dict[str, Any]) -> dict[str, Any]:
    """Check that ``data`` has exactly the fields of ``cls`` with the right types.

    Args:
        cls (type[Message]): target dataclass.
        data (dict): the decoded frame, without its ``type`` key.

    Returns:
        dict: ``data`` unchanged, safe to pass as ``cls(**data)``.

    Raises:
        ProtocolError: on missing / extra fields or a type mismatch.
    """
    expected = {f.name: f.type for f in fields(cls)}
    missing = expected.keys() - data.keys()
    extra = data.keys() - expected.keys()
    if missing or extra:
        raise ProtocolError(
            f"{cls.TYPE}: missing fields {sorted(missing)}, unexpected fields {sorted(extra)}"
        )

    # Field annotations are strings ("int" / "str") because of
    # `from __future__ import annotations`; map them to real types.
    # bool is a subclass of int in Python, so it's rejected explicitly.
    python_types = {"int": int, "str": str}
    for name, annotation in expected.items():
        value = data[name]
        wanted = python_types[annotation]
        if isinstance(value, bool) or not isinstance(value, wanted):
            raise ProtocolError(f"{cls.TYPE}.{name} must be {annotation}, got {value!r}")
    return data
