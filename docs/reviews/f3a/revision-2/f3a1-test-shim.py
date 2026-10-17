"""Test-only shim (outside the repo): install the F3-A1 prototype primitives."""

import importlib
import os

_proto = importlib.import_module(os.environ.get("F3A_PROTO_MODULE", "f3a1_prototype"))
import rfq_agent.observability.redaction as _r

_r.redact_text = _proto.redact_text
_r.redact_mapping = _proto.redact_mapping
_r.summarize_payload = _proto.summarize_payload
_r.redact_payload = _proto.redact_payload
_r.REDACTED_KEY = _proto.REDACTED_KEY
