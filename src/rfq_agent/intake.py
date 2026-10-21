"""The production intake path: a declared fresh RFQ in, immutable rows out.

Every accepted phase before this one consumed an RFQ that already existed. The
demonstration could only run because :mod:`rfq_agent.demo.intake` - a
demo-scoped shim - wrote the two rows a quotation needs, and ``demo/intake.py``
has said since Phase 1M-C that "no accepted phase owns an intake write path
yet". This module is that path: the smallest production writer of ``rfqs`` and
``runs``, with no fixture identifier, no scenario and no import of the demo or
the test factories.

Three properties are deliberate.

**Intake is dumb.** It normalises, hashes, keys and stores - it decides nothing.
Whether the text *is* an RFQ, who the customer is and what the products are is
decided downstream by the deterministic core, exactly as before. Nothing here
reads a catalogue, a price or a customer row (except the one sentinel that
proves the dataset is loaded).

**The stored row is the source of truth.** :func:`provision_intake` writes what
the caller declared; :func:`load_source` reads the row back and rebuilds
:class:`~rfq_agent.domain.intake.InboundRfq` from the *database*, re-checking
every digest. Verification downstream therefore runs against what was stored,
never against the object the caller still holds in memory - a caller cannot
verify one body and store another.

**Identity is stated, and an identity is not a licence to overwrite.** A
declaration names its own ``rfq_id``, ``run_id`` and idempotency key. Re-declaring
the same identity with the same content is a no-op (the rows are reused);
re-declaring it with different content, or reusing a key for a different RFQ,
is a typed refusal that names the fields - intake history is append-only and
nothing here rewrites it. A replay is accepted only while the run is
field-for-field the untouched initial attempt this path wrote: every lifecycle
column is compared, so a run that a later phase has started, retried, finished
or re-parented is refused rather than silently rejoined.

**The get-or-create runs under the write lock.** :func:`provision_intake` takes
SQLite's write lock (``BEGIN IMMEDIATE``) *before* its existence reads, so two
first deliveries of the same message serialise: the second one reads the row the
first committed and reports it as already stored, instead of racing a deferred
read into a constraint failure. A caller that is already inside a transaction
keeps it; the lock is then its own to hold.

The same complete check is exported as :func:`require_intake_preconditions`, so a
caller can re-assert it inside a later write transaction (the quote write path
does exactly that) and close the gap between "intake looked untouched" and "the
rows were written". That check covers the source as a whole, not just the RFQ
columns: a stored RFQ that has since acquired attachment metadata is refused,
because :func:`load_source` cannot reconstruct it and verifying only the body
would silently drop source content.

What this module deliberately does not write: attachments, thread links,
supersession, refusal events or run transitions. ``intake_events`` records
exactly one ``RECEIVED`` row per stored RFQ, in the same transaction as the
RFQ itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Annotated, Self

from pydantic import StringConstraints, ValidationError, model_validator
from sqlalchemy import select, text

from rfq_agent.domain.ids import IdempotencyKey, RfqId, RunId
from rfq_agent.domain.intake import InboundRfq, IntakeEventKind, RfqStatus, SourceChannel
from rfq_agent.domain.trust import UntrustedText
from rfq_agent.domain.values import DomainModel, sha256_text
from rfq_agent.domain.workflow import RunState, RunTrigger
from rfq_agent.persistence.models import (
    CustomerRow,
    IntakeEventRow,
    RfqAttachmentRow,
    RfqRow,
    RunRow,
)
from rfq_agent.seed.loader import SeedSchemaError, require_schema

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

__all__ = [
    "IntakeConflictError",
    "IntakeConflictKind",
    "IntakeDeclaration",
    "IntakeError",
    "IntakeIntegrityError",
    "IntakePrerequisiteError",
    "StoredIntake",
    "load_source",
    "provision_intake",
    "require_intake_preconditions",
    "require_prerequisites",
]

#: The customer whose presence proves the business dataset has been written.
#: The same sentinel the demo command uses, for the same reason: the deterministic
#: core reads this dataset, and an empty catalogue would turn every line
#: ``UNMATCHED`` rather than fail honestly.
_DATASET_SENTINEL = "CUS_0001"

#: Largest body this path accepts, in characters. The trust settings cap untrusted
#: intake at 65 536 bytes; refusing an oversized declaration here is preferable to
#: storing it truncated, because a truncated body changes what verification reads.
_MAX_BODY_CHARS = 65_536

#: Every RFQ field the declaration owns and a stored row must reproduce. The
#: primary key is excluded (it is how the row is found), and so are
#: ``created_at``/``updated_at``, which the database stamps rather than the caller.
_RFQ_CONTENT_FIELDS: tuple[str, ...] = (
    "source_channel",
    "status",
    "received_at",
    "subject",
    "body_text",
    "body_sha256",
    "sender_name",
    "sender_email",
    "sender_email_hash",
    "idempotency_key",
    "thread_key",
    "superseded_by_rfq_id",
)

#: Every run field the declaration owns: the whole initial lifecycle. A stored
#: run is "untouched" only when all of them still agree - ``state`` and
#: ``outcome`` alone would let a started, retried or re-parented attempt pass as
#: one nothing had happened to.
_RUN_CONTENT_FIELDS: tuple[str, ...] = (
    "rfq_id",
    "attempt_no",
    "trigger",
    "state",
    "prior_run_id",
    "outcome",
    "failure_code",
    "retry_count",
    "checkpoint_stage",
    "row_version",
    "started_at",
    "finished_at",
)


class IntakeError(Exception):
    """Base class for intake failures; messages are never rendered by a CLI."""


class IntakePrerequisiteError(IntakeError):
    """The database is not migrated, or the business dataset is not loaded."""


class IntakeIntegrityError(IntakeError):
    """A stored intake row cannot be read back as the request it claims to be."""


class IntakeConflictKind(StrEnum):
    """Why a declaration contradicts what is already stored.

    A closed vocabulary rather than free text, so a caller can classify the
    contradiction without reading - or printing - an exception message.
    """

    #: The same ``rfq_id`` is stored with different content.
    CONTENT_DIFFERS = "CONTENT_DIFFERS"
    #: The idempotency key already belongs to a different RFQ.
    KEY_REUSED = "KEY_REUSED"
    #: The declared run exists but is no longer the untouched ``RECEIVED`` attempt.
    LIFECYCLE_MOVED = "LIFECYCLE_MOVED"
    #: The stored RFQ a caller expected to read back is no longer there.
    INTAKE_MISSING = "INTAKE_MISSING"
    #: The stored RFQ has acquired attachment metadata this path cannot verify.
    ATTACHMENTS_PRESENT = "ATTACHMENTS_PRESENT"


class IntakeConflictError(IntakeError):
    """A declaration collides with stored intake history; nothing was written."""

    def __init__(self, kind: IntakeConflictKind, message: str) -> None:
        """Record the closed-vocabulary kind alongside a safe message."""
        self.kind = kind
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class StoredIntake:
    """What a provisioning call established.

    ``created`` is ``True`` only for the call that wrote the rows; a replay of
    an identical declaration reuses them and writes nothing.
    """

    rfq_id: str
    run_id: str
    created: bool


class IntakeDeclaration(DomainModel):
    """One fresh RFQ as a caller states it: content, identity and the one instant.

    Every field is the caller's statement. There is no clock and no generated
    identifier: ``received_at`` is when the message arrived, and the three
    identifiers are the ones the surrounding system already assigned. The body
    is untrusted content (tier T2) and is carried verbatim; it is hashed here,
    never parsed.

    ``SourceChannel.FIXTURE`` is refused: it names the demonstration's own
    channel, and the production path must not be able to write a row that
    claims to be one.
    """

    rfq_id: RfqId
    run_id: RunId
    #: Stable per-message key; ``rfqs.idempotency_key`` is unique, so a
    #: re-delivered message can never become a second RFQ.
    idempotency_key: IdempotencyKey
    source_channel: SourceChannel
    received_at: datetime
    subject: Annotated[str, StringConstraints(max_length=500)] = ""
    body: Annotated[str, StringConstraints(min_length=1, max_length=_MAX_BODY_CHARS)]
    sender_name: Annotated[str, StringConstraints(min_length=1, max_length=200)] | None = None
    sender_email: Annotated[
        str,
        StringConstraints(min_length=3, max_length=320, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$"),
    ]

    @model_validator(mode="after")
    def _check_the_production_surface(self) -> Self:
        """Refuse the demo channel and a timestamp with no timezone."""
        if self.source_channel is SourceChannel.FIXTURE:
            msg = (
                "source_channel FIXTURE belongs to the demonstration: a declared intake "
                "must name EMAIL, WEB_FORM or API"
            )
            raise ValueError(msg)
        if self.received_at.utcoffset() is None:
            msg = "received_at must be timezone-aware: intake history is ordered in UTC"
            raise ValueError(msg)
        return self

    def to_source(self) -> InboundRfq:
        """Build the immutable intake snapshot this declaration states.

        The digest of the sender address is computed here, once, so the stored
        row and the reconstructed source cannot disagree about it.
        """
        return InboundRfq(
            source_channel=self.source_channel,
            received_at=self.received_at,
            subject=self.subject,
            body=UntrustedText.from_text(self.body),
            sender_name=self.sender_name,
            sender_email=self.sender_email,
            sender_email_hash=sha256_text(self.sender_email),
            idempotency_key=self.idempotency_key,
        )


def require_prerequisites(session: Session) -> None:
    """Refuse unless the database is migrated and seeded, naming the fix command.

    This path neither migrates nor seeds. Both facts are checked here rather
    than left to fail later as an ``UNMATCHED`` line or a missing customer,
    because those failures would look like business outcomes instead of a
    setup mistake.

    Raises:
        IntakePrerequisiteError: When the schema is missing (run
            ``alembic upgrade head``) or the dataset is missing (run ``make seed``).
    """
    try:
        require_schema(session)
    except SeedSchemaError as error:
        raise IntakePrerequisiteError(
            "database schema is missing; run `alembic upgrade head`"
        ) from error
    if session.get(CustomerRow, _DATASET_SENTINEL) is None:
        msg = (
            "the Northwind Components dataset is not loaded: write it first with "
            "`make seed` (or `python -m rfq_agent.seed`)"
        )
        raise IntakePrerequisiteError(msg)


def provision_intake(session: Session, declaration: IntakeDeclaration) -> StoredIntake:
    """Store the declared RFQ, its initial run and its ``RECEIVED`` intake event.

    Get-or-create, with no overwrite path: a stored row is reused only when it
    is field-for-field what the declaration states - the RFQ *and* the run's
    complete lifecycle projection. Anything else is a typed conflict and the
    caller's transaction is left to roll back.

    The write lock is taken before the existence reads, so a concurrent first
    delivery serialises behind this one rather than racing its reads.

    Args:
        session: The unit of work both rows are written in. The caller commits.
            A session that has not begun a transaction gets ``BEGIN IMMEDIATE``;
            one that already has keeps its own transaction and lock discipline.
        declaration: The stated content, identity and arrival instant.

    Returns:
        The identifiers written (or found) and whether this call created them.

    Raises:
        IntakeConflictError: When the identity is stored with different content,
            the idempotency key belongs to another RFQ, or the run has moved on.
    """
    _begin_write(session)
    source = declaration.to_source()
    expected = _rfq_row(declaration, source)
    stored = session.get(RfqRow, declaration.rfq_id)
    if stored is not None:
        require_intake_preconditions(session, declaration, stored=stored, expected=expected)
        return StoredIntake(rfq_id=declaration.rfq_id, run_id=declaration.run_id, created=False)
    owner = session.scalar(
        select(RfqRow.rfq_id).where(RfqRow.idempotency_key == declaration.idempotency_key)
    )
    if owner is not None:
        msg = (
            "the declared idempotency key already belongs to a different RFQ: one message "
            "cannot be two requests, and the key is never reused or rewritten"
        )
        raise IntakeConflictError(IntakeConflictKind.KEY_REUSED, msg)
    if session.get(RunRow, declaration.run_id) is not None:
        # The RFQ is new but the run id is taken. The database would refuse the
        # insert; refusing here names the contradiction instead of surfacing a
        # constraint violation as an unexplained conflict.
        msg = (
            "the declared run identifier already exists for another RFQ: a run belongs to "
            "exactly one request, and this path never re-parents one"
        )
        raise IntakeConflictError(IntakeConflictKind.LIFECYCLE_MOVED, msg)
    session.add(expected)
    # Flush between dependency levels: the models declare no relationships, so
    # SQLAlchemy does not order the inserts by foreign key.
    session.flush()
    session.add(_run_row(declaration))
    session.add(
        IntakeEventRow(
            rfq_id=declaration.rfq_id,
            seq=1,
            kind=IntakeEventKind.RECEIVED,
            occurred_at=declaration.received_at,
            detail_json=None,
            detail_sha256=None,
        )
    )
    session.flush()
    return StoredIntake(rfq_id=declaration.rfq_id, run_id=declaration.run_id, created=True)


def load_source(session: Session, rfq_id: RfqId) -> InboundRfq:
    """Rebuild the intake snapshot from the stored row, digests re-checked.

    This is the only source a quotation may be verified against. The body text
    and sender address are read from ``rfqs``, and both stored digests are
    recomputed from those columns before the snapshot is constructed: a row
    whose text no longer hashes to its recorded digest is refused rather than
    verified against whatever it now says.

    Raises:
        IntakeIntegrityError: When the row is absent, a digest disagrees with
            its text, the row carries attachments this path cannot reconstruct,
            or the stored values do not form a valid inbound request.
    """
    row = session.get(RfqRow, rfq_id)
    if row is None:
        msg = "the declared RFQ is not stored, so there is no source to verify"
        raise IntakeIntegrityError(msg)
    if sha256_text(row.body_text) != row.body_sha256:
        msg = "the stored body digest does not match its text: refusing to verify against it"
        raise IntakeIntegrityError(msg)
    if sha256_text(row.sender_email) != row.sender_email_hash:
        msg = "the stored sender digest does not match its address: refusing to bind a customer"
        raise IntakeIntegrityError(msg)
    attachment = session.scalar(
        select(RfqAttachmentRow.attachment_id).where(RfqAttachmentRow.rfq_id == rfq_id)
    )
    if attachment is not None:
        msg = (
            "the stored RFQ carries attachment metadata this path cannot reconstruct: "
            "verifying only the body would silently drop source content"
        )
        raise IntakeIntegrityError(msg)
    try:
        return InboundRfq(
            source_channel=row.source_channel,
            received_at=row.received_at,
            subject=row.subject,
            body=UntrustedText.from_text(row.body_text),
            sender_name=row.sender_name,
            sender_email=row.sender_email,
            sender_email_hash=row.sender_email_hash,
            idempotency_key=row.idempotency_key,
            thread_key=row.thread_key,
        )
    except ValidationError as error:
        msg = "the stored RFQ cannot be reconstructed as an inbound request"
        raise IntakeIntegrityError(msg) from error


def _begin_write(session: Session) -> None:
    """Take SQLite's write lock before this transaction reads anything.

    A deferred transaction that reads before it writes can have its snapshot
    invalidated by a concurrent commit; the write then fails with
    ``SQLITE_BUSY_SNAPSHOT`` (or a unique-key violation) instead of observing
    the row the other writer stored. ``BEGIN IMMEDIATE`` makes the read the
    last word: the lock is held from the first statement, so a concurrent first
    delivery waits - within the configured ``busy_timeout`` - and then reads a
    committed row.

    A session already inside a transaction is left alone: its lock discipline
    is its own, and nesting ``BEGIN`` is not possible.
    """
    if session.in_transaction():
        return
    session.execute(text("BEGIN IMMEDIATE"))


def require_intake_preconditions(
    session: Session,
    declaration: IntakeDeclaration,
    *,
    stored: RfqRow | None = None,
    expected: RfqRow | None = None,
) -> None:
    """Refuse unless the stored intake is still exactly what was declared.

    This is the complete check a replay and any later write against the same
    intake must pass. It compares every field the declaration owns - the RFQ's
    content, digests and status - then asserts the source is still one this path
    can reconstruct in full (no attachment metadata), and finally compares the
    run's whole lifecycle projection, naming the fields that disagree.

    It is safe to call inside another writer's transaction: the reads then see
    that transaction's snapshot, which is what makes the quote write path's
    guard atomic rather than a check-then-write race. Called there under
    ``BEGIN IMMEDIATE`` it is also the last word on the source: no attachment
    can be committed between this read and the write that follows it.

    Args:
        session: The unit of work to read through. Nothing is written here.
        declaration: The stated content, identity and arrival instant.
        stored: The RFQ row, when the caller already read it in this unit of
            work. Re-read only when omitted.
        expected: The RFQ row the declaration states, when already built.

    Raises:
        IntakeConflictError: When the RFQ is missing or differs from the
            declaration (``CONTENT_DIFFERS``/``INTAKE_MISSING``), when it has
            acquired attachment metadata (``ATTACHMENTS_PRESENT``), or when the
            run is no longer the untouched initial attempt (``LIFECYCLE_MOVED``).
    """
    row = stored if stored is not None else session.get(RfqRow, declaration.rfq_id)
    if row is None:
        msg = (
            "the declared RFQ is no longer stored: intake history cannot be verified, "
            "so nothing may be written against it"
        )
        raise IntakeConflictError(IntakeConflictKind.INTAKE_MISSING, msg)
    _require_identical(
        row, expected if expected is not None else _rfq_row(declaration, declaration.to_source())
    )
    _require_no_attachments(session, declaration.rfq_id)
    _require_untouched_run(session, declaration)


def _require_identical(stored: RfqRow, expected: RfqRow) -> None:
    """Refuse a stored row that is not field-for-field the declared one."""
    mismatches = [
        name for name in _RFQ_CONTENT_FIELDS if getattr(stored, name) != getattr(expected, name)
    ]
    if mismatches:
        msg = (
            "the declared RFQ identity already exists as different content "
            f"({', '.join(mismatches)}): intake history is append-only, so the stored "
            "row is neither overwritten nor rejoined - declare a new identity"
        )
        raise IntakeConflictError(IntakeConflictKind.CONTENT_DIFFERS, msg)


def _require_no_attachments(session: Session, rfq_id: str) -> None:
    """Refuse a stored RFQ that has acquired attachment metadata.

    :func:`load_source` is the only source a quotation may be verified against,
    and it refuses an RFQ with attachments because it can reconstruct the body
    and the sender but not the files. This is the same rule, stated where a
    writer can re-assert it: an attachment added by another connection after the
    source was loaded would otherwise leave a quote verified against a source
    that is no longer the full one.

    The query is a presence test, not a read of the metadata: no filename,
    content type or preview is loaded, and the message names no attachment.
    """
    present = session.scalar(
        select(RfqAttachmentRow.attachment_id).where(RfqAttachmentRow.rfq_id == rfq_id).limit(1)
    )
    if present is not None:
        msg = (
            "the stored RFQ carries attachment metadata this path cannot reconstruct: "
            "verifying only the body would silently drop source content, so nothing is "
            "written against it"
        )
        raise IntakeConflictError(IntakeConflictKind.ATTACHMENTS_PRESENT, msg)


def _require_untouched_run(session: Session, declaration: IntakeDeclaration) -> None:
    """Refuse unless the stored run is field-for-field the untouched attempt.

    ``state`` and ``outcome`` are not enough: a run that a worker started and
    then left in ``RECEIVED`` again, a retried attempt, a re-parented run or one
    whose version moved all describe history this path did not write. They are
    compared like every other lifecycle column, and named when they differ.
    """
    run = session.get(RunRow, declaration.run_id)
    if run is None:
        msg = (
            "the declared run is missing: intake stores the initial attempt, so a "
            "declaration cannot rejoin history that is not there"
        )
        raise IntakeConflictError(IntakeConflictKind.LIFECYCLE_MOVED, msg)
    expected = _run_row(declaration)
    mismatches = [
        name for name in _RUN_CONTENT_FIELDS if getattr(run, name) != getattr(expected, name)
    ]
    if mismatches:
        msg = (
            "the declared run is no longer the untouched initial attempt "
            f"({', '.join(mismatches)}): this path never reopens, retries or re-parents "
            "a run, and refuses to rejoin one whose lifecycle moved"
        )
        raise IntakeConflictError(IntakeConflictKind.LIFECYCLE_MOVED, msg)


def _rfq_row(declaration: IntakeDeclaration, source: InboundRfq) -> RfqRow:
    """The inbound record the declaration states: hashed, never parsed.

    ``thread_key`` and ``superseded_by_rfq_id`` are left ``None``: threading and
    supersession are separate decisions, and inventing either here would assert
    a history the declaration never stated.
    """
    return RfqRow(
        rfq_id=declaration.rfq_id,
        source_channel=source.source_channel,
        status=RfqStatus.OPEN,
        received_at=source.received_at,
        subject=source.subject,
        body_text=source.body.text,
        body_sha256=source.body.sha256,
        sender_name=source.sender_name,
        sender_email=source.sender_email,
        sender_email_hash=source.sender_email_hash,
        idempotency_key=source.idempotency_key,
        thread_key=None,
        superseded_by_rfq_id=None,
    )


def _run_row(declaration: IntakeDeclaration) -> RunRow:
    """The run the quote will belong to: initial, and left in ``RECEIVED``.

    No transition is written. The accepted workflow's edges are the only legal
    ones, and none of them is this path's to take: the run stays where intake
    left it until a phase that owns a transition decides otherwise.
    """
    return RunRow(
        run_id=declaration.run_id,
        rfq_id=declaration.rfq_id,
        attempt_no=1,
        trigger=RunTrigger.INITIAL,
        state=RunState.RECEIVED,
        prior_run_id=None,
        outcome=None,
        failure_code=None,
        retry_count=0,
        checkpoint_stage=None,
        row_version=0,
        started_at=declaration.received_at,
        finished_at=None,
    )
