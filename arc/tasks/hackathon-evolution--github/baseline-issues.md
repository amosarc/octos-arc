# Baseline issues (platform grader round — github, 42/100)

> Provenance: extracted from the platform grader's playwright-report.json
> (run start 2026-10-03T21:13:02Z, 269s, 100 scenarios: 42 passed /
> 58 failed, 10s per-scenario timeout, 1 worker). The report file has
> since been removed from the project dir; counts below are the preserved
> forensic extraction. Mount telemetry from the same eval window shows
> several of these names DID appear in the DOM ("Pull requests" 72×,
> "Issues" 72×, "Settings" 75×, "Acme Demo" 34×) — the failures are
> visibility-at-the-moment / accessible-name / reachability mismatches
> and over-long flows, NOT missing features.

## Navigation targets the grader could not find (33 failures)

`Could not find a visible navigation target named "X"` — the grader walks
by clicking point-visible targets; a name that exists in the DOM but is
hidden at click time, buried behind a menu/dropdown, or duplicated/renamed
fails the whole scenario. Recurring targets:

- "Acme Demo" — 7 scenario(s) (seed organization; must be visible and
  clickable in the org surface without opening a collapsed dropdown first)
- "Pull requests" — 5 scenario(s) (PR tab/nav must be a visible tab, not
  behind a menu)
- "Compare" — 3 scenario(s) (the Compare tab never mounted during the
  whole eval window — likely unreachable in the grader's session state;
  REQ-6-2-3)
- "Overview onboarding PR" — 3 scenario(s) (seed PR row link on the PR
  list must render the quoted display name as its accessible name)
- "Settings" — 2 scenario(s)
- "Issues" — 2 scenario(s)
- "Reviewable onboarding PR" — 2 scenario(s)
- 9 single occurrences: seed titles such as "Draft onboarding update",
  "Document search flow", "Commentable onboarding issue",
  "Editable onboarding issue", "Closable onboarding issue" — each entity
  link's accessible name must be the requirement's quoted display name.

Family concentration of all 58 failures: REQ-6 (PRs) 20, REQ-5 (issues)
14, REQ-2 (org/teams) 10, REQ-4 (code/search) 7, REQ-1 3, REQ-3 2.
Worst spec files: REQ-4-2-3-search-code (4), REQ-1-3-change-password,
REQ-6-2-1-filter-pull-requests, REQ-6-3-1-view-pull-request (3 each).

## Hard timeouts (13 failures, 10s per-scenario cap)

- REQ-1-3 change password — Scenarios 1/2/3 (sign-in → account menu →
  Settings → "Password and authentication" → 3 fields → update →
  sign-out dialog → re-login: too many hops for 10s; shorten the flow's
  clicks/waits, render navigation synchronously)
- REQ-4-2-3 repository code search — Scenarios 1-4
- REQ-5-1-1 — Scenarios 1/2
- REQ-3-2-2 — Scenarios 1/2
- REQ-2-2-4 — Scenario 2
- REQ-4-2-1 — Scenario 1

## Other failures

- REQ-6-1 Scenario 1 — strict-mode `toBeVisible()` failure (duplicate
  accessible names: no two controls may share an accessible name on a
  page).

## Repair directions (bind to the RULES in AGENTS.md)

1. Navigation chrome renders synchronously and stays visible: tabs,
   named links, menu items — never gated behind a data fetch, never
   rebuilt on open, never hidden behind a collapsed dropdown the
   scenario does not open.
2. Every seed entity is reachable from the home page by clicking
   visible links in the session state each scenario starts from;
   signing in never hides an entry signed-out users had.
3. Entity links use the requirement's quoted display name as the
   accessible name (never slug/id); an entity's name renders at most
   once per page as visible text.
4. Long flows must fit a 10-second budget: fewer hops, no artificial
   confirmation detours, instant navigation.
