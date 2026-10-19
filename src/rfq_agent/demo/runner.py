"""The demonstration pipeline: compose the accepted components, decide nothing.

One scenario run: the provisioning shim gets-or-creates the fixture-stated RFQ
and run rows; 1M-B's ``bind_customer``/``resolve_lines`` rule over the
untrusted intake and the claims; 1M-A's ``to_quote_request`` assembles the
request (its refusals are the "a human is needed" outcomes); 1L's quote run
decides over the business data and 1J''s ``QuoteWriter`` stores the accepted
artefacts. Outcomes are coarseness-classified into the four demo outcomes -
coarse enough to carry no rule: evidence exists or it does not, the gate is
eligible or it is not.

Boundaries this module is held to:

* **no business rules.** Every verdict is an accepted function's own.
* **the human seam is stated, never simulated.** A fixture's ``human_binding``
  becomes a ``ResolutionSource.HUMAN`` binding checked against the search
  evidence; a contradiction is a fixture defect, not a decision.
* **the demo checks itself.** An outcome class disagreeing with the fixture's
  declared ``expected`` is a failure, not a narrative.
* **the read boundary only reads.** The only writes are the shim's two rows
  and the accepted write path's unit of work.

Nothing here approves, sends, transitions a run, or stores a gate decision -
the gate's verdict is reported on stdout and nowhere else.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import ValidationError

from rfq_agent.demo.errors import DemoError
from rfq_agent.demo.fixtures import DemoOutcome, DemoScenario
from rfq_agent.demo.intake import provision_run
from rfq_agent.domain.extraction import ExtractionResult, RequestedDelivery
from rfq_agent.domain.resolution import (
    CustomerMatchStatus,
    ResolutionMatchStatus,
    ResolutionSource,
    ResolvedCustomer,
    ResolvedLine,
)
from rfq_agent.persistence.repositories import BusinessReader
from rfq_agent.persistence.writers import QuoteWriter
from rfq_agent.quote_adapter import ClaimValidationError, to_quote_request
from rfq_agent.quoting import QuoteRunStatus, run_quote
from rfq_agent.resolving import bind_customer, resolve_lines

if TYPE_CHECKING:
    from rfq_agent.persistence.engine import Database
    from rfq_agent.persistence.read_models import CustomerSearch
    from rfq_agent.persistence.writers import QuoteWriteResult
    from rfq_agent.quoting import QuoteRunResult

__all__ = ["ScenarioReport", "run_scenario"]


@dataclass(frozen=True, slots=True)
class ScenarioReport:
    """Everything one scenario run established, ready for the command to print.

    ``result`` and ``write`` are absent exactly when the run stopped before
    quoting: by construction, nothing was prepared or stored.
    """

    scenario: DemoScenario
    outcome: DemoOutcome
    #: One sentence on how the customer was (or was not) bound.
    customer_summary: str
    #: One sentence per claimed line: the resolution core's ruling and reason.
    line_summaries: tuple[str, ...]
    #: Why the run stopped before quoting, when it did.
    stop_reason: str | None = None
    #: The deterministic run's own artefacts, when it completed.
    result: QuoteRunResult | None = None
    #: What the write path stored - or found already stored.
    write: QuoteWriteResult | None = None


def run_scenario(scenario: DemoScenario, database: Database) -> ScenarioReport:
    """Run one fixture scenario through the accepted chain and report it.

    Args:
        scenario: The fixture: intake, claims, identifiers, declared outcome.
        database: The prepared (migrated and seeded) database to provision
            into, read through, and write the accepted artefacts to.

    Raises:
        DemoError: When the fixture's human binding contradicts the search
            evidence, or the run's outcome class is not the declared one.
    """
    with database.session() as session:
        source = provision_run(session, scenario)

    with database.session_factory() as read_session:
        reader = BusinessReader.for_session(read_session)
        customer, search = bind_customer(source.sender_email, reader=reader)
        customer, customer_summary = _customer_for_quote(scenario, customer, search)
        lines = resolve_lines(scenario.lines, line_item_ids=scenario.line_item_ids, reader=reader)
        line_summaries = tuple(_line_summary(line) for line in lines)
        try:
            request = to_quote_request(
                customer,
                lines,
                source=source,
                extraction=ExtractionResult(
                    lines=scenario.lines,
                    requested_delivery=scenario.requested_delivery or RequestedDelivery(),
                ),
                run_id=scenario.run_id,
                quote_id=scenario.quote_id,
                quote_number=scenario.quote_number,
                pricing_as_of=scenario.pricing_as_of,
                stock_as_of=scenario.stock_as_of,
                requested_delivery=scenario.requested_delivery,
                delivery_as_of=scenario.delivery_as_of,
            )
        except ClaimValidationError as error:
            codes = ", ".join(sorted({issue.code.value for issue in error.issues}))
            return _stopped(
                scenario,
                customer_summary,
                line_summaries,
                f"Claim verification refused the quote (codes: {codes}).",
            )
        except ValidationError:
            return _stopped(
                scenario,
                customer_summary,
                line_summaries,
                "A human must review this RFQ before quoting.",
            )
        except ValueError:
            # Adapter ValueErrors can embed untrusted request and database data.
            return _stopped(
                scenario,
                customer_summary,
                line_summaries,
                "Quote request refused; human review is required.",
            )
        result = run_quote(request, reader)
        if result.status is not QuoteRunStatus.COMPLETED:
            return _stopped(
                scenario,
                customer_summary,
                line_summaries,
                "A human must review this RFQ before quoting.",
            )

    fresh = _classify(result)
    if fresh is not scenario.expected:
        msg = (
            f"scenario {scenario.scenario_id}: the fixture declares {scenario.expected} but "
            f"the run produced {fresh}: the demo no longer matches the accepted behaviour"
        )
        raise DemoError(msg)
    write = QuoteWriter(database).persist(result.calculation, result.ledger)  # type: ignore[arg-type]
    return ScenarioReport(
        scenario=scenario,
        outcome=DemoOutcome.DUPLICATE if write.duplicate else fresh,
        customer_summary=customer_summary,
        line_summaries=line_summaries,
        result=result,
        write=write,
    )


def _customer_for_quote(
    scenario: DemoScenario,
    customer: ResolvedCustomer,
    search: CustomerSearch,
) -> tuple[ResolvedCustomer, str]:
    """Apply the contract's binding rule to what the resolution core ruled.

    An ``EXACT`` ruling stands; anything else reaches quoting only via the
    contract's human path - the fixture's stated ``human_binding``, checked
    against the search evidence - or fails at the adapter.
    """
    if customer.match_status is CustomerMatchStatus.EXACT:
        summary = "EXACT identity: the sender's address is a stored alias of one active customer"
        return customer, summary
    binding = scenario.human_binding
    if binding is None:
        if customer.match_status is CustomerMatchStatus.UNBOUND:
            summary = (
                "UNBOUND: no stored alias or name proves who this sender is - "
                "a human must bind the customer"
            )
        else:
            summary = (
                f"{customer.match_status} ({customer.customer_id}): evidence, not proven "
                "identity - a human must confirm before pricing may proceed"
            )
        return customer, summary
    if search.matched_ids and binding not in search.matched_ids:
        found = ", ".join(search.matched_ids)
        msg = (
            f"scenario {scenario.scenario_id}: the fixture's human binding {binding!r} "
            f"contradicts the search evidence ({found}): stating a different customer would "
            "be inventing, not deciding - fix the fixture"
        )
        raise DemoError(msg)
    if not search.matched_ids:
        status = CustomerMatchStatus.NO_MATCH
    elif len(search.matched_ids) == 1:
        status = CustomerMatchStatus.SINGLE_CANDIDATE
    else:
        status = CustomerMatchStatus.AMBIGUOUS
    bound = ResolvedCustomer(
        customer_id=binding,
        match_status=status,
        source=ResolutionSource.HUMAN,
    )
    summary = f"{status}: operator supplied a fixture-stated human binding (source HUMAN)"
    return bound, summary


def _line_summary(line: ResolvedLine) -> str:
    """One safe sentence with the resolution ruling, excluding identifiers."""
    if line.status is ResolutionMatchStatus.AMBIGUOUS:
        return "AMBIGUOUS: multiple catalogue matches; human selection required"
    if line.status is ResolutionMatchStatus.RESOLVED:
        return "RESOLVED: catalogue match accepted"
    return f"{line.status}: human review required"


def _classify(result: QuoteRunResult) -> DemoOutcome:
    """The persisted outcome of a completed run, coarse enough to hold no rule.

    Blocking evidence - or a gate finding the quote ineligible for human
    review - is a blocked draft; anything else awaits the human every V1
    quote awaits.
    """
    ledger = result.ledger
    if (ledger is not None and ledger.reasons) or not result.eligible_for_human_review:
        return DemoOutcome.DRAFT_BLOCKED
    return DemoOutcome.DRAFT_AWAITING_HUMAN_REVIEW


def _stopped(
    scenario: DemoScenario,
    customer_summary: str,
    line_summaries: tuple[str, ...],
    stop_reason: str,
) -> ScenarioReport:
    """Report a run that stopped before quoting - and check the fixture knew."""
    if scenario.expected is not DemoOutcome.HUMAN_REQUIRED_BEFORE_QUOTE:
        msg = (
            f"scenario {scenario.scenario_id}: the fixture declares {scenario.expected} but "
            f"the run stopped before quoting ({stop_reason}): the demo no longer matches "
            "the accepted behaviour"
        )
        raise DemoError(msg)
    return ScenarioReport(
        scenario=scenario,
        outcome=DemoOutcome.HUMAN_REQUIRED_BEFORE_QUOTE,
        customer_summary=customer_summary,
        line_summaries=line_summaries,
        stop_reason=stop_reason,
    )
