"""Observability primitives.

Phase 0 provides correlation identifiers, the structured log payload shape, the
redaction choke point and the trace-span schemas. The writer that persists trace
rows arrives with the workflow engine in Phase 2; the operator-facing trace view
arrives in Phase 6.
"""
