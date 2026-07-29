import json


def encode_webrtc_payload(kind, payload):
    if isinstance(payload, (bytes, bytearray, memoryview)):
        body = bytes(payload)
    else:
        body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return kind.encode("ascii") + b"|" + body


def decode_webrtc_payload(message):
    if isinstance(message, str):
        message = message.encode("utf-8")
    if isinstance(message, memoryview):
        message = message.tobytes()
    if not isinstance(message, (bytes, bytearray)):
        raise TypeError("message must be bytes-like")
    if b"|" not in message:
        raise ValueError("invalid WebRTC payload")
    kind, body = message.split(b"|", 1)
    kind_text = kind.decode("ascii")
    try:
        return kind_text, json.loads(body.decode("utf-8"))
    except Exception:
        return kind_text, body
