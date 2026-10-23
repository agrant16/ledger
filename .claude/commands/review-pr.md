---
description: Review a ledger pull request against CLAUDE.md, the design doc, and the correctness rules (review only, no edits)
argument-hint: [pr-number]
allowed-tools: Read, Grep, Glob, Bash(gh pr view:*), Bash(gh pr diff:*), Bash(gh pr checks:*), Bash(git log:*), Bash(git diff:*), Bash(git status:*), Bash(gh pr comment:*)
---

Review pull request $ARGUMENTS. If no number was given, review the PR for the current branch.

This is a review only. Do not edit files, commit, or push. Posting is allowed as step 8 describes. Report findings to me here.

## 1. Load context first

Read `CLAUDE.md` and the parts of `docs/design.md` that the PR touches: the milestone checkbox it
addresses, the API and error sections, and the invariants. Read any ADR in `docs/adr/` the change
relates to. Then run `gh pr view`, `gh pr diff` and `gh pr checks` for the PR.

## 2. Scope and process

- Does the PR address one milestone checkbox (or a clearly stated half of one)? Flag bundled,
  unrelated changes.
- Is it on its own branch, with small focused commits, and are both CI checks (`lint-and-test`,
  `compose-smoke`) passing? Intermediate red commits are fine; the final state must be green.
- Does the PR description have all three parts (scope, tests, review notes), and does it match
  what the diff actually does?

## 3. Who wrote what

My "I write myself" list in CLAUDE.md covers transfer logic and `post_transaction`, concurrency,
idempotency, the core data model and migrations, reconciliation, and the API layer. For any code
in those areas, check the commit trailers and the PR's review notes. Flag anything Claude wrote
there that the notes do not call out, and flag red tests that were meant to stay red but were
changed to pass.

## 4. Correctness rules and conventions

Check the diff against each of these, and say which ones the change could affect:

1. Entries sum to zero per currency in every transaction.
2. Entries are append-only, enforced by the database for the app role.
3. The cached balance equals the sum of the account's entries.
4. No protected account (`allow_negative` false) goes negative, including under concurrency.
5. A replayed idempotency key never applies a transfer twice.
6. Every entry's currency matches its account's.

Number rules as in the invariants list in `docs/design.md`.

Conventions: integer minor units, never floats; one database transaction per request on one
connection; balances rows locked with `SELECT ... FOR UPDATE` in ascending account id at READ
COMMITTED, with the overdraft check on the balance that same statement returned; service
functions take the connection and never commit; expected failures are `LedgerError` subclasses
and bugs are not; 400 for validation and 422 only for insufficient funds; merged migrations never
edited; grants written in the migration that creates the table; no `ALTER DEFAULT PRIVILEGES`;
SQL identifiers that are not keywords; ids as `bigint GENERATED ALWAYS AS IDENTITY`.

Layering: SQL only in `repository/`, no psycopg or pool imports in `service/` or `api/`, and the
import-direction test still passes.

## 5. Tests

- Real Postgres, not mocks, in the folder that matches the kind of test.
- Do the tests check the properties above, or just whatever the code currently does?
- Was any existing test changed? If so, was the reason stated, or did a test quietly bend to
  fit the code?
- For concurrency or idempotency changes: is there a test that would actually fail if the lock
  order, the claim, or the key handling were wrong?

## 6. Docs

- Is `docs/design.md` updated in this same PR if a design decision changed, and is the checkbox
  ticked only if the item is fully done?
- If an ADR is involved: Draft status, a row in `docs/adr/README.md`, and it matches the code.
- Do the README and the OpenAPI descriptions still match the behavior?
- Do new or changed docstrings follow the "Docstrings" conventions in `CLAUDE.md`? Do not flag anything those conventions allow (for example a short function with brief text, no `__init__` docstring, or no docstring on a function whose name and signature say everything). Do not ask for implementation reasoning in docstrings.

## 7. Report

Group findings under these headings, each with a file and line reference where possible:

- **Blocking:** breaks a correctness rule, a convention, or the ownership split.
- **Should fix:** a real gap in tests, docs, or edge cases.
- **Nits:** style and naming.
- **Pythonic Score:** A 1 to 10 score of how Pythonic the code I have written is. The baseline is 7, any function or other part that falls below 7 should be pointed out for review. 
- **Questions for me:** anything ambiguous that I need to decide.
- **Could not verify:** what you could not check and why.
- **Issue tasks:** For any attached issue verify the listed items are actually done.
- **Non-Issue changes:** For any changes that aren't in the attached issue but are minor related changes, call them out but they should be allowed with a comment added to the issue to note they were rolled in. e.g. docstring changes in the same module, type hints added in the same module
Finish with up to five questions I should be able to answer about the code in the "I write
myself" areas, as if an interviewer asked. Then draft, but do not post, text for the PR's review
notes section.

## 8. Wrap up

When I say the review is finished, write a short wrap-up for the PR: each finding and how it
was resolved (fixed, deferred, or decided against, with the reason), plus any decisions we made
that aren't in the code. Draft it for me to approve; do not post it. Once I've approved add
the wrap up to the PR as a comment via `gh pr comment`.
