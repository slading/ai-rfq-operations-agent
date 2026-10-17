"""Test-only shim (outside the repo): install the F3-A1 prototype primitives.

The shim also extends ``__all__`` so acceptance tests can assert the public
surface the implementation PR will ship. It never writes to the repository.
"""

import importlib
import os

_proto = importlib.import_module(os.environ.get("F3A_PROTO_MODULE", "f3a1_prototype"))
import rfq_agent.observability.redaction as _r

_r.redact_text = _proto.redact_text
_r.redact_mapping = _proto.redact_mapping
_r.summarize_payload = _proto.summarize_payload
_r.redact_payload = _proto.redact_payload
_r.REDACTED_KEY = _proto.REDACTED_KEY

for _name in ("redact_payload", "REDACTED_KEY"):
    if _name not in _r.__all__:
        _r.__all__ = [*_r.__all__, _name]
