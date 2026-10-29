# Ledger Service

A double-entry ledger service I'm building as a portfolio project, working with Claude Code. The goal is a correct, well-tested system I can explain in depth, not just working code.

## Read first

The project scope, architecture, data model, API design, milestones, and testing plan are in `docs/design.md`. Read it before starting any work, and follow its milestone order. It is the source of truth for the design. I keep some planning notes elsewhere, so don't assume anything outside this repo matches it. If the repo layout or stack differs from the design doc, tell me instead of silently diverging. If we change a design decision, update the design doc in the same change.

## Stack and conventions

The stack below is decided and pre-approved. Don't substitute alternatives.

- Python 3.13, FastAPI with synchronous handlers, PostgreSQL 18, raw SQL via psycopg (no ORM), `psycopg_pool`, `uv` with `pyproject.toml`, `ruff`, SQLFluff, pytest with Hypothesis, Locust, Docker Compose, GitHub Actions.
- Amounts are integers in minor units (cents) plus a currency code. Never floats.
- Every write is one database transaction. A request uses one connection for its whole transaction and never takes a second one while holding the first.
- Lock `balances` rows with `SELECT ... FOR UPDATE` in ascending account id, at READ COMMITTED. Check overdraft against the balance returned by that same locking statement.
- Migrations are numbered plain SQL files applied by a small runner (no Alembic), run as the owner role. The service connects as the app role, which owns no tables and has explicit per-table grants written in the migration that creates each table. Never connect as the superuser or owner, and never use `ALTER DEFAULT PRIVILEGES`. Once a migration is merged, never edit it except for formatting and comments; a schema change is a new migration.
- SQL is linted with SQLFluff (PostgreSQL dialect). Do not use SQL keywords as identifiers: the account name column is `account_name`, not `name`.
- Ids are `bigint GENERATED ALWAYS AS IDENTITY`.
- Validation errors return 400, not FastAPI's default 422. 422 means insufficient funds.
- Tests run against a real Postgres, not mocks.
- Service functions take the request's connection and never commit; the caller owns the database transaction, so a retry wraps the key claim and the posting together.
- Expected failures are `LedgerError` subclasses in `ledger/errors.py`, and the API layer maps each to an HTTP status. Bugs (`LedgerInvariantError`, database safety-net violations) are not `LedgerError`s and return a logged 500.

### Docstrings

Reasons and the full list are in #141. Rule numbers are those in the `docs/design.md` invariants list. Applies to implementation docstrings (`src/`). Testing docstrings are exempt, e.g. lazy import docstrings.

- Google style, descriptive mood ("Returns", not "Return"). Single backticks for code names. `Args` and `Raises` entries are capitalised sentences ending in a period.
- Sections: `Args:`, `Returns:`, `Raises:` (no types, the signature has them), plus `Yields:` for generators and context managers. `Attributes:` for every class with public attributes (dataclasses, exceptions that store fields, plain classes such as middleware): `name: description` when the attribute has a type hint, `name (type): description` only when it has none.
- The class docstring documents construction: `Args:` for the constructor's arguments, `Attributes:` for the fields, with an argument that is also a field in both (for example `ConcurrencyCapMiddleware`: `app` and `cap` in both, `request_count` only in `Attributes:`). `__init__` usually has no docstring; if it has one, it is one line with no `Args:`.
- Public pydantic models (those in OpenAPI) get no `Attributes:` block, because the class docstring becomes the published schema description. Write it as client-facing text.
- Entry points (`main.py`, `seed.py`, `migrate`) have a module docstring with usage and exit codes. Other modules need one only if their purpose is not clear from the name.
- Contract only: what callers can rely on, what they raise, which rules and constraints apply. Cite rules and ADRs in parentheses after the summary line, on the same line: `(rule 5, ADR 0012)`. No implementation reasoning, worked examples, maintainer notes or filler, and no inline comments for removed reasoning.
- A function called from outside its own module has `Args`, `Returns` and `Raises`, as far as it has them; short functions get shorter text but still list them. A private function needs no full sections, but a docstring it has follows the same rules. A function whose name and signature say everything may have none.
- `Raises:` lists every error the function raises or lets through, with full causes only for the errors it raises itself; a propagated error is listed without its full causes, as they are listed in the function that raises it. Database errors never propagate as such: the repository translates them (ADR 0013), and a repository-level safety-net violation reaches callers as `LedgerInvariantError`. A function that raises or lets through `LedgerInvariantError` lists it once, with its own causes and a general cause for anything underneath; an orchestrator that only calls other functions describes the cause generally. There is no standalone "Propagates…" line. Examples are in #141.
- `conn`: "The request's connection, inside the caller's transaction." in services; "An active, open PostgreSQL connection or transaction context." in repositories.

## How we split the work

This project has to demonstrate my own skills, so the division matters.

**I write myself:**
- Transfer logic and the core ledger invariants (`post_transaction` and the code around it)
- Concurrency handling: lock ordering, retries, timeouts
- Idempotency: the key claim, request hashing, and replay behavior
- The data model and migrations for the core tables, including their grants and constraints
- The reconciliation job
- The API layer: routes, request validation and error mapping, and pagination

**Claude can do:**
- Project scaffolding, config, and boilerplate (Docker Compose, CI, the app-role init script, the migration runner)
- OpenAPI docs and descriptions for the endpoints I write (including the balance endpoint's cached-read note)
- Test generation and test infrastructure, including Hypothesis and concurrency harnesses
- The Locust load test and benchmark scripts
- Code review of what I write: bugs, edge cases, missed invariants
- Docs, README drafts, and tooling scripts

If I ask Claude to write something from my "I write myself" list, ask me to confirm first. If I say yes, walk me through the code afterward so I can explain it. If a task falls between the two lists, ask me which side it belongs on rather than guessing.

## Correctness rules

These properties must always hold. Tests should check them directly, preferably as property-based tests and concurrency tests, not only example-based ones.

1. Every transaction's entries sum to zero per currency (debits equal credits).
2. Ledger entries are never changed after they are written, and the database itself refuses UPDATE and DELETE on them for the service's role (the owner role, used only for migrations and test fixtures, is not restricted). Corrections are made with new entries, never by editing or deleting old ones.
3. The cached balance always equals the sum of that account's entries.
4. An account with `allow_negative` false never goes below zero, including under concurrent transfers. The funding accounts (`allow_negative` true) are the exception.
5. The same idempotency key never moves money twice: replaying the same transfer request with the same key never applies it twice.
6. Every entry's currency matches its account's currency.


These mirror the invariants list in `docs/design.md`, in the same order. Cite a rule by its number in that list (in tests, docstrings, PR descriptions and issues). If the two ever disagree, tell me and we'll fix both.

Write tests against these properties, not against whatever the current code happens to do. If a test fails, don't change the test to match the code without telling me why.

## Working style

- Work one checkbox at a time, in milestone order, and keep each change small enough for me to review. A checkbox normally gets one pull request; if one needs two, split it into two PRs and say so before starting. If a pull request is growing beyond what I can review in one sitting, stop and propose a split before going on, as with the layering refactor, whose step 2 was split into two PRs, 2a (accounts) and 2b (posting) (ADR 0013); it is a judgement call, not a line count, and the decision is mine. Tests, docs and scaffolding that a checkbox needs belong in its PR; unrelated clean-ups do not, and go in their own. A milestone's "Done when" line is its acceptance check.
- Make small, focused commits with clear messages. Don't bundle unrelated changes.
- Stage files by explicit path, and only the ones the change is about. Never run `git add -A`, `git add .`, `git add -u` or `git commit -a`: they sweep in work in progress that I haven't asked to commit (this happened on PR 83, where my uncommitted implementation files went into a commit that Claude co-authored). Check `git status` before committing, and stage again after a hook or formatter rewrites a file.
- Before writing code for a non-trivial feature, briefly state the approach and any tradeoffs so I can weigh in (step 1 of the workflow below).
- Explain any non-obvious design choice in plain terms, in chat or an ADR (not in docstrings), so I can explain and defend my decisions.
- Record the larger design decisions as architecture decision records in `docs/adr/`, using `docs/adr/template.md`. An ADR goes in the same pull request as the code it describes, like the design doc update, starts as `Draft`, is marked `Accepted` in the PR that merges it, and gets a row in the index at `docs/adr/README.md`. One ADR can cover a group of related decisions, so group them by theme and date, not one per decision. Claude drafts them from our discussions and the code, and I review them.
- An `Accepted` ADR's decision is not rewritten. Formatting, typos, comments and links may be fixed, and the Status line and index row may change (for example `Accepted (partly implemented)` to `Accepted`, or `Superseded by ADR NNNN`). Any other change is recorded one of two ways: a dated note under the section it affects (`*Note (YYYY-MM-DD): ...*`, naming the issue or PR that prompted it), for a correction or update that does not reverse the decision; or a new ADR that supersedes the old one, for a reversed or replaced decision, with the old ADR's Status and the index row updated. A `Draft` ADR is edited freely within its PR. The design doc is still updated in place, because it is the source of truth and not a decision log.
- Dependencies beyond the stack above, schema changes, and changes to the public API need my approval first. Adding the pre-approved stack's own packages during scaffolding is fine.
- Every change goes on its own branch and a pull request, never straight to `main`. Both CI checks (`lint-and-test` and `compose-smoke`) must pass, and PRs are squash-merged, so intermediate commits on a branch may be red. A pull request whose tests are committed ahead of the implementation is opened as a draft and stays one until its checks pass.
- For the items on my "I write myself" list, Claude writes the tests first from `docs/design.md` and I write the implementation; the tests stay red until it exists.
- Tests written ahead of their implementation import the code under test lazily, inside a fixture (docstring: "Imported lazily so these tests fail one by one while the module does not exist yet."), so each test fails on its own and not the whole file at collection. Claude replaces that fixture with a normal top-level import in the finish step below. The older fixtures with that docstring were replaced with top-level imports in the clean-up of #55, so none remain; the other function-level imports in tests were hoisted in the clean-up of #118.
- Ruff rule `PLC0415` rejects an import outside the top of a file. The lazy fixture carries `# noqa: PLC0415` until the finish step replaces it, and any other late import needs the same marker and a comment saying why. The acceptable reasons are listed in the comment above `select` in `pyproject.toml`.
- Every pull request description has three parts, which Claude writes when the PR is opened and keeps current as it changes: the scope (the milestone item or component the PR addresses); the tests (what was written or run, and which correctness rules or `docs/design.md` sections they enforce); and the review notes (what Claude scaffolded, what remains for me to implement, and any edge cases we discussed). The description also names the issue the PR implements with `Closes #N`, and links related issues.

### Workflow for a checkbox that involves tests and code

Follow these steps in order. Each approval is mine to give, so don't skip ahead. The rules above all still apply.

1. **Plan.** We plan the current task: state the approach and tradeoffs, and settle the details. Claude then drafts the issue for it (see Issues and the project board) and creates it once I approve.
2. **Tests.** Once I approve the plan, Claude writes the tests, if there are any.
3. **Test rundown.** Before anything is committed, Claude gives me a rundown of the tests: the files they add, how many tests each file adds, a summary of each test (what it does and which design constraint or correctness rule it checks), and how we know they work (for example that they fail for the expected reason against the missing code, or that a control shows they can fail).
4. **Draft PR.** Once I approve the tests, Claude commits them, pushes the branch and opens the pull request as a draft, because the tests fail CI until the implementation exists. My approval of the tests is the go-ahead for that commit, push and pull request.
5. **What is left.** Once the tests are committed and the pull request has its initial description, Claude lists the work left for me to implement.
6. **Finish.** When I have an implementation and everything is green, we finalize the pull request: Claude replaces the lazy-import fixtures in the PR's tests with top-level imports (and reruns the tests), brings its description up to date, moves it out of draft, and reminds me to run `/clear` and then `/review-pr <PR number>`.

For work on Claude's own list, such as scaffolding or test infrastructure, Claude does the implementation after step 4 and the same finish applies once it is green.

## Issues and the project board

Work is tracked as GitHub issues in this repository, on the Ledger Project board (`https://github.com/users/agrant16/projects/1`), so there is a ticket for each piece of implementation work. Claude creates and updates them with the `gh` CLI (it needs the `project` token scope).

- **One issue per checkbox,** or per pull request when the checkboxes are bundled into one PR (like the three safety-net items of PR 5c). Issues are created when we plan the checkbox (step 1 of the workflow below), not for the whole backlog up front.
- **Claude drafts, I approve.** Claude shows me the title, body, labels, milestone and board status, and creates the issue only after I say yes. The same goes for any later edit that changes its meaning.
- **Milestone:** `M1` to `M7`, matching the headings in `docs/design.md`. A `type:tech-debt` issue has none, because it is not a design checkbox.
- **Owner label:** `owner:alan` (an item on my "I write myself" list), `owner:claude` (an item on Claude's list), or `owner:shared` (Claude writes the tests, I write the code). If an item falls between the two lists, ask me, as with any other task.
- **Type label:** `type:task` for implementation work on a design checkbox, `type:decision` for a deferred design decision to revisit, `type:tech-debt` for a clean-up of existing code, tests or docs with no behavior change (for example hoisting a test's function-level import to the top of the file).
- **Board status:** `Todo` when created, `In Progress` when work starts, `Done` when the PR merges (`Closes #N` closes the issue).
- **Body:** the scope (the checkboxes as a task list, quoted from `docs/design.md`), the correctness rules it defends, and any notes that are not in the design doc. The design doc stays the source of truth; an issue points to it and does not replace it.
- **Deferred decisions** go in a `type:decision` issue with the trigger for revisiting them, so they are not left only as a sentence in a doc.

## Commands

These were verified in milestone 1.

- Start the database the tests need: `docker compose up -d --wait db`
- Run tests: `uv run pytest` (CI runs `uv run pytest --cov`, which also enforces the coverage floor)
- Run only the tests that need no database: `uv run pytest tests/unit`. Tests live in `tests/unit`, `tests/integration` (`db`, `service`, `api`), `tests/property`, `tests/concurrency` (the thread harness and the tests built on it) and `tests/e2e`; put a new test where its kind belongs
- Run linter: `uv run ruff check .`
- Run formatter: `uv run ruff format .`
- Lint the SQL migrations: `uv run sqlfluff lint migrations/` (CI runs it; `uv run sqlfluff fix migrations/` applies the safe fixes)
- Run the service locally: `docker compose up --build` (a one-shot `migrate` service applies migrations first)
- Apply migrations by hand: `PYTHONPATH=src uv run python -m ledger.migrate`
