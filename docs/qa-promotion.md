# Products DB — QA promotion proposal (HU-PRO-15)

Current status: both QA PR24 integrations are complete. DB: `89cd9a670d3e540c3cc524411ff77c058f1b8960`; API: `4f0f15be88156ed86ec6755d679721ea026d71f0`.
The current [main proposal](main-promotion.md) remains pending review/integration.
The candidate executions and trails below remain historical QA evidence.

## Purpose

Proposal to promote the Products DB delivery of HU-PRO-15 (synkro-docs#317)
from `develop` to `qa`. This repository delivers exactly two TD018 sub-items;
TD017 is delivered by the paired Products API proposal.
This is a fresh local verification of a proposal. It is **not** a persistent
QA deployment and **not** an executed promotion to `main`. Historical develop
evidence in `docs/evidence/` is kept separate and unchanged.

## Source and base

| Item | Commit |
| --- | --- |
| Source (actual integrated develop merge of DB PR23) | `0867b1e98e65ed3c24a170248961ef04d1a44a89` |
| QA base | `5f15eb094a7214962a4aae7e7d768e9c250440b9` |
| Candidate (tested) | `ce8fd682e143049977302b1666e8e69bf3dedafa` |
| Branch | `qa-products-store-integrity` |

The QA child uses the repository's existing `qa-...` naming: GitHub rejected
`qa/...` with a directory/file conflict against the permanent `qa` ref.
No merge between permanent branches was made. Both feature commits were
applied with `cherry-pick -x`.

## Source → QA trail

Each QA commit message carries the literal line shown.

1. `c634b1896800bd70e9d07229e2eecf324a30b29b` →
   `5cf5ba91bee64add09a1415e96a7f50b7a66d7fb`
   (`test(db): specify approved reservation and reader integrity`)

   `(cherry picked from commit c634b1896800bd70e9d07229e2eecf324a30b29b)`

2. `2a58ab08493080cc9af22cab72d7f2f981d0c143` →
   `ce8fd682e143049977302b1666e8e69bf3dedafa`
   (`feat(db): enforce reservation uniqueness and read-only access`)

   `(cherry picked from commit 2a58ab08493080cc9af22cab72d7f2f981d0c143)`

## Candidate and scope

The candidate tree is identical to the tree of source
`0867b1e98e65ed3c24a170248961ef04d1a44a89`. Migrations, rollbacks, tests and
configuration match their develop source exactly; the only change added on top
of the tested candidate is documentation (this report and the fresh verification JSON). All 34 historical
SQL files, V001–V017 and U001–U017, are unchanged, and
CODEOWNERS is unchanged. TD012 is excluded. HU-DOCS-98 is untouched.

## Fresh validation (local, on the candidate)

Evidence: `docs/evidence/qa-promotion-verification.json` (label
`qa-db-full-ci`, exit code 0, log SHA-256
`9b441cef25b96e0931847e4c603f6cb3c53bee0bb6e54d1a5b533b87f1505b97`).
This run is separate from the develop-era evidence.

10 applicable local CI run blocks passed:

1. Schema-isolated history contract.
2. Migrate from empty — every V migration applied (through V020).
3. Migrate again — no migration necessary.
4. Privilege check: `products_app` can use `products_schema`.
5. Privilege check: `products_app` table privileges.
6. Negative-path constraint checks.
7. Rollbacks applied, highest version to lowest.
8. `products_schema` and `products_writer` no longer exist.
9. Migrate again after disposable reconstruction.
10. Approved Products integrity — real upgrade, reader, duplicate and rollback
    cases: 18 real-engine cases on PostgreSQL 16.15 / Flyway 11.20.3.

No owned resources remain after the run. There is no DB Pact: this repository
exposes no HTTP contract and is exempt.

## Billing exception

The owner decided on 2026-11-14 (America/Bogota) that the GitHub Actions
account-billing failure is not an impediment for this delivery only. Jobs that
never started are not successful tests, and no green remote CI is claimed.
Review, integrity checks, tests and branch rules still apply.

## Order, merge and limits

- Both actual QA integrations are complete. Main proposals are tracked in
  [main-promotion.md](main-promotion.md), using cherry-pick -x from actual QA.
- The owner alone merges. Main order: DB first, then API after confirmation of
  its fixture provenance against the actual DB main integration.
- Main requires current ariel5253 approval on the delivered head and the
  existing review/conversation rules. Earlier approvals do not transfer.
- Not claimed: a persistent QA deployment, and inspection of target-environment
  owners, default privileges or duplicate data (disposable tests do not show
  these).
- No tag, rollout, or closure of HU-PRO-15 / synkro-docs#317. TD018 stays
  partial: five residual items remain open, coordinated by SergioAndres17,
  with no review date.
