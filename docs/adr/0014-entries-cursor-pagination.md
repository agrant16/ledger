# ADR 0014: Entry history pages by an opaque cursor on the entry id

* **Status:** Accepted
* **Date:** 2026-10-19 (decided during milestone 3 planning, issue #88)
* **Deciders:** Alan (human architect) & Claude (AI collaborator)

## Context
`GET /accounts/{id}/entries` returns an account's history, which grows without bound, so it must be paged. A client that walks the pages while transfers are being committed must see every entry exactly once: a page boundary must neither repeat an entry nor skip one. The usual shortcuts do not give that. Offset paging shifts when a new entry arrives, and ordering by `created_at` is not safe either, because `created_at` is the transaction's start time and a transfer that started earlier can wait for a lock and commit after a later one. The service also wants a stable, explainable reason why the walk is correct, since the project's point is to show correctness under concurrency. Entries are never updated or deleted (invariant 2), which helps.

## Decision
- **Keyset paging on `entries.id`, newest first.** A page is the entries of one account with `id < cursor`, ordered by `id DESC`, limited to the page size. The first page has no cursor. The index on `(account_id, id)` (milestone 1) serves the query. The order is by entry id, never by `created_at`, which is informational and may not be in id order.
- **Why the walk is correct.** Every writer locks the account's `balances` row with `SELECT ... FOR UPDATE` before inserting entries for it, and holds the lock until commit (ADR 0006). So for one account, one writer's inserts finish and commit before the next writer's begin, and the ids of that account's entries increase in commit order. Identity values are handed out at insert time, so ids across different accounts can interleave with commit order, but within one account they cannot. A page boundary therefore never skips an entry that commits late, and a new entry always has a higher id than anything a walk has already read. The cursor is only as safe as this lock: an insert into `entries` that skips the `balances` lock would break it. Milestone 4 adds a paging-while-writing test and a control that shows the skip when the lock is left out.
- **The cursor is the base64url of the last returned entry id** written in decimal, without `=` padding. It is opaque to clients and unsigned. Decoding is strict: it accepts only text that the encoder could have produced (the decoded id re-encodes to the same string), so each id has exactly one valid cursor and padding, whitespace, signs, leading zeros and other spellings are rejected. The id must be within the id bounds (1 to the `bigint` maximum). Undecodable text and an out-of-range id are both a 400. The codec is `encode_cursor` and `decode_cursor` in `ledger/api/rest/cursor.py`.
- **Unsigned is acceptable here.** A cursor is just a position in the caller's own listing. A forged one can only start a page at a different entry id, and entries are not secret in this service (ADR 0009: no authentication, balances and ids are exposed).
- **`limit + 1` rows are fetched** to learn whether an older entry exists, and only `limit` are returned. `next_cursor` is the cursor of the last returned entry when one exists and null otherwise, so there is never an empty trailing page and no `count(*)`. There is no total count.
- **`limit` is 1 to 200, default 50, and an out-of-range value is a 400, never clamped.** Like path ids, it must be plain decimal digits (`+5`, `05` and `5.0` are rejected). The maximum is one constant, `MAX_PAGE_SIZE` in `ledger/models/entry_history_model.py`, used by the route's validation and by the range check in `get_entry_history`. The service layer treats an out-of-range limit as a bug (`LedgerInvariantError`), because the API guarantees the range.
- **Validation is a Pydantic query type.** `LimitInQuery` and `CursorInQuery` are in `ledger/api/rest/schemas/types.py`. A bad value becomes a `RequestValidationError`, so the 400 has the same `errors` list with `parameter` set to `limit` or `cursor` as every other query problem (ADR 0009). The type decodes the cursor, so the route hands the service a plain entry id (`before_id`) and the service knows nothing about cursors. Stateless validation runs before the account lookup, so a bad cursor on an unknown account is a 400, not a 404.

## Consequences
* **Positive:**
  - A walk returns each entry exactly once, whatever is committed during it, and the reason is one sentence about the balances lock.
  - Page cost does not grow with depth: each page is an index range scan from the cursor.
  - No server-side state, no expiry, and a cursor stays valid indefinitely because entries are never deleted.
  - The service layer has no HTTP or encoding concerns, and the error shape matches the rest of the API.

* **Negative / Trade-offs:**
  - A client cannot jump to page N, only walk forward from the newest entry.
  - Correctness depends on every writer taking the `balances` lock first. A future bulk loader or a repair script that inserts entries without it can make a walk skip an entry.
  - The cursor can be forged, which is harmless here but would matter for an endpoint with access control.
  - A walk is not a snapshot: entries committed after it started are not shown on later pages, which is intended, but a client wanting a consistent view across a long walk must start again to see new entries.

* **Alternatives Considered:**
  - **Offset paging (`LIMIT/OFFSET`):** rejected. Rows shift when entries are added, so a walk repeats or skips entries, and the database scans and discards all the earlier rows.
  - **Keyset on `created_at`, or `(created_at, id)`:** rejected. `created_at` is the transaction's start time, not its commit time, so it can be out of order with commit order and a boundary could skip a late commit.
  - **A signed or encrypted cursor:** rejected as extra machinery with no benefit while there is no authentication.
  - **A total count:** rejected. It costs a scan, and with concurrent writers the number is stale as soon as it is read.
  - **Clamping an oversized `limit` to 200:** rejected. A silent change to the request hides a client bug; a 400 shows it.
  - **A REPEATABLE READ snapshot held for the whole walk:** rejected. It needs server-side state across requests, and the balances-lock argument already gives exactly-once without it.
