# ADR [Number]: [Short Title of the Decision]

* **Status:** [Draft | Proposed | Accepted | Rejected | Superseded by [ADR-XXXX]]. An Accepted ADR that is not yet fully built may add the suffix "(partly implemented)" with a short note on what is missing; drop the suffix when the work is done.
* **Date:** [YYYY-MM-DD]
* **Deciders:** [Your Name / Human Architect & Claude (AI collaborator)]

## Context
*Describe the context and problem statement that is driving this decision. What are we trying to solve, and what constraints are we operating under (e.g., performance, data integrity, strict Python/Postgres stack)?*

## Decision
*Describe the architectural choice we are making. Be specific about what technology, pattern, or constraint we are adopting.*

## Consequences
*What becomes easier or harder as a result of this decision?*

* **Positive:**
  - *Benefit 1 (e.g., eliminates ORM abstraction overhead)*
  - *Benefit 2*

* **Negative / Trade-offs:**
  - *Drawback 1 (e.g., requires writing explicit raw SQL migrations)*
  - *Drawback 2*

* **Alternatives Considered:**
  - *Alternative 1 (e.g., SQLAlchemy ORM): Why was it rejected?*

*Once this ADR is Accepted, do not rewrite its decision. Record a later change as a dated note under the section it affects, or as a new ADR that supersedes this one (see CLAUDE.md).*