"""Tests for the WebSocket message (de)serialisation in mail_printer_protocol.messages."""

import json

import pytest

from mail_printer_protocol.messages import (
    PROTOCOL_VERSION,
    Ack,
    Fail,
    Hello,
    Print,
    ProtocolError,
    decode,
    encode,
)


@pytest.mark.parametrize(
    "message",
    [
        Hello(protocol_version=PROTOCOL_VERSION),
        Print(job_id=42, png_b64="iVBORw0KGgo=", fallback_text="01/01/2026 12:00\nAnonymous\nhi"),
        Ack(job_id=42),
        Fail(job_id=42, error="paper out"),
    ],
)
def test_round_trip(message):
    assert decode(encode(message)) == message


def test_encode_wire_format():
    assert json.loads(encode(Ack(job_id=7))) == {"type": "ack", "job_id": 7}


def test_decode_accepts_bytes():
    assert decode(b'{"type":"ack","job_id":1}') == Ack(job_id=1)


@pytest.mark.parametrize(
    "frame",
    [
        "not json",
        "[1, 2]",
        '{"job_id": 1}',  # no type
        '{"type": "nope"}',  # unknown type
        '{"type": 3}',  # non-string type
        '{"type": "ack"}',  # missing field
        '{"type": "ack", "job_id": 1, "extra": 1}',  # unexpected field
        '{"type": "ack", "job_id": "1"}',  # wrong type
        '{"type": "ack", "job_id": true}',  # bool is not an int here
        '{"type": "fail", "job_id": 1, "error": null}',
    ],
)
def test_decode_rejects_malformed(frame):
    with pytest.raises(ProtocolError):
        decode(frame)
