# Products DB — main promotion proposal (HU-PRO-15)

## Purpose

Proposal to promote the Products DB delivery of HU-PRO-15 (synkro-docs#317)
from `qa` to `main`, requested by the owner after both QA PR24 deliveries were
merged. The approved scope is narrow: exactly the two TD018 sub-items
(reservation uniqueness plus the read-only reader). TD017 belongs to the paired
Products API proposal.
This is a fresh local verification of a proposal. It is **not** an actual merge
into `main`, **not** a persistent deployment and **not** a tag. Historical
QA/develop execution evidence and trails stay separate and unchanged; only
their current-status links are updated.

## Source and base

| Item | Commit |
| --- | --- |
| Source (actual integrated QA merge of DB PR24) | `89cd9a670d3e540c3cc524411ff77c058f1b8960` |
| Main base | `043e485d16018fc4fb4cd60694c2a521c143ddbd` |
| Previous QA head | `5f15eb094a7214962a4aae7e7d768e9c250440b9` |
| Candidate (tested) | `5030b3d490d046d9abd4837b0240f2c40e9a8e0d` |
| Branch | `release/products-store-integrity` |

The QA merge was actually performed by the owner on 2026-11-14 21:27:34
America/Bogota. No actual `main` merge SHA exists yet and none is claimed. No
merge between permanent branches was made and nothing was committed directly to
a permanent branch; all four source commits were reapplied on the release
branch with `cherry-pick -x`. Policy:
[branching policy](https://github.com/code-corhuila/synkro-docs/blob/main/00-governance/branching-policy.md).

## Source → main trail

All four sources are actual ancestors of the integrated QA commit. Each target
commit message carries the literal line shown.

1. `5cf5ba91bee64add09a1415e96a7f50b7a66d7fb` →
   `48064c82b97c54e82c6f68631a0a9890e937c262`
   (`test(db): specify approved reservation and reader integrity`)

   `(cherry picked from commit 5cf5ba91bee64add09a1415e96a7f50b7a66d7fb)`

2. `ce8fd682e143049977302b1666e8e69bf3dedafa` →
   `e5135cd36245a40afb6430a67fb1f74311e85a2b`
   (`feat(db): enforce reservation uniqueness and read-only access`)

   `(cherry picked from commit ce8fd682e143049977302b1666e8e69bf3dedafa)`

3. `49708686cb536dfc43448cd3208b5ec3666d257f` →
   `0c68ce6a195eb7350f9899df4da517d984d29847`
   (`docs(qa): record Products database promotion evidence`)

   `(cherry picked from commit 49708686cb536dfc43448cd3208b5ec3666d257f)`

4. `fb1192226831e55ca3ae03166c65126fa34b6efa` →
   `5030b3d490d046d9abd4837b0240f2c40e9a8e0d`
   (`docs(qa): record Products database promotion evidence`)

   `(cherry picked from commit fb1192226831e55ca3ae03166c65126fa34b6efa)`

## Candidate and protected content

The candidate tree is identical to the tree of integrated QA
`89cd9a670d3e540c3cc524411ff77c058f1b8960`, before this documentation was
added. No new production code, configuration, schema, test, credential or
`.github` change is made. Added on top of the tested candidate: this report, the fresh verification
JSON and current-status links in the QA report.

Unchanged: all 34 historical SQL files (V001–V017 and U001–U017) and
CODEOWNERS.

## Fresh validation (local, on the candidate)

Evidence: `docs/evidence/main-promotion-verification.json` (label
`main-db-full-ci`, exit code 0, log SHA-256
`62da691d1618bda751b4a03065bfc734f26f0339d4101db1ed0e0ba4f9a28307`). This run
was executed now on the main candidate; it is separate from the QA and develop
evidence, which is not claimed as newly executed.

10 applicable local CI run blocks passed:

1. Schema-isolated history contract.
2. Migrate from empty — every V migration applied (V001..V020).
3. Migrate again — no migration necessary (no-op).
4. Privilege check: `products_app` can use `products_schema`.
5. Privilege check: `products_app` table privileges.
6. Negative-path constraint checks.
7. Rollbacks applied, highest version to lowest (full old/new U rollback).
8. `products_schema` and `products_writer` no longer exist.
9. Migrate again after disposable reconstruction (rebuild).
10. Approved Products integrity — real upgrade, reader, duplicate and rollback
    cases: 18 real-engine integrity tests on PostgreSQL 16.15 / Flyway 11.20.3.

The 18 tests cover upgrade with existing rows, dirty duplicates aborting
untouched, the exact unique-violation error, current and future effective
reader rights, unsafe role adoption, and partial rollback/reapply. No owned
resources remain after the run. There is no HTTP contract and no Pact for this
repository; it is exempt
([testing strategy, Tier 3b](https://github.com/code-corhuila/synkro-docs/blob/main/11-quality/testing-strategy.md#tier-3b-pact)).

## Billing exception

The owner decided on 2026-11-14 (America/Bogota) that the GitHub Actions
account-billing failure is not an impediment for this same delivery only. Jobs
that never started remain unstarted and are not successful tests; no green
remote CI is claimed. No other review, integrity, test or branch gate is
removed.

## Order, review and remaining evidence

- The owner alone merges. Review/merge order: this DB `main` PR first; the API
  `main` PR follows after its fixture provenance is confirmed/updated against
  the actual DB `main` integration.
- `main` requires current approval by ariel5253 on the delivered head, plus the
  existing conversation/review rules. QA/develop approvals do not transfer and
  CODEOWNERS is not changed. No teacher approval is claimed.
- Not claimed: an actual `main` merge, a persistent deployment, and inspection
  of target-environment owners, default privileges or duplicate data (disposable
  tests do not provide these; target creator/default-privileges/duplicate
  preflight evidence remains to be supplied).
- No tag or version is selected. HU317 stays open; central
  reconciliation/scenario 10 stays pending.
- Excluded: HU-DOCS-98 is untouched; TD012 remains open and excluded (no plan).
  TD018 stays partial: five residual differences remain open with
  SergioAndres17 and no review date.
