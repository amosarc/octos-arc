# Baseline issues (sheet — no exported grader report; verify first)

> Provenance: the sheet run ended by the kernel loop-guard (last
> doom-events entry: "kept calling the same tool … Stopping the turn"),
> so the final state was never verified green and no grader report was
> exported. Evidence available: eval-traffic telemetry during the eval
> window (~04:41–06:57Z) showing `PATCH /api/workbooks/wb_q3sales/
> sheets/sh1 → 400` repeated 23×. This dossier is deliberately short:
> Phase 0 must first MEASURE (run the full own suite cold), then fix
> what is actually red.

## Known signals

- `PATCH /api/workbooks/wb_q3sales/sheets/sh1` returned 400 twenty-three
  times during the eval window — a rename/validation path on a
  baseline-seed workbook rejecting valid payloads (check the rename
  rules for over-rejection, e.g. leaked create-side validation applied
  to PATCH).
- The previous run never reached a verified-green finish (loop-guard
  kill mid-turn): treat every family's "green" as unproven until the
  cold-start own suite says otherwise.

## Phase 0 duties for this project (in order)

1. Run the full self-test suite cold (`sh scripts/run-e2e.sh`) and list
   every red — that list, not assumptions, drives the repair.
2. Extend backend/data/db.json IN PLACE with the EVO-* workbooks the
   evolution GIVEN lines pre-provision (names, worksheets, cell values
   and formulas verbatim); never remove or rename a baseline workbook.
3. Fix the reds with surgical edits; re-run the suite until green.
   Navigation chrome renders synchronously; every flow fits a 10-second
   budget; no two controls share an accessible name.
