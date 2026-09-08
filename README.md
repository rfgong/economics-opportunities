# economics-opportunities

Public, lightweight cache for a weekly economics presentation-opportunity and recruiting/outreach scan.

## Design

This repository deliberately separates **monitoring** from **semantic judgment**.

- `venue_calendar.json` stores durable public facts about recurring research venues.
- `recruiting_calendar.json` stores durable public facts about recurring research-oriented PhD outreach sources.
- `refresh.py` checks the official pages weekly and records page health / changes in `state.json`.
- The weekly ChatGPT automation performs live web verification, discovers new calls, and ranks opportunities for the user.

This is intentionally **not** a generalized conference scraper. Conference sites change frequently, so the GitHub layer fails closed and preserves prior good evidence instead of guessing.

## Files

```text
README.md
venue_calendar.json
recruiting_calendar.json
state.json
refresh.py
.github/workflows/refresh.yml
```

## Important rules

1. Historical call/deadline months are monitoring priors only.
2. A current call is not considered open until verified from a current authoritative source.
3. Failed page checks never erase prior successful state.
4. Repeated source failures are marked `manual_review_required`.
5. Amazon is Priority 0 for recruiting/outreach monitoring.
6. Ordinary job postings are not recruiting/outreach opportunities unless they provide unusually strong research exposure or structured economist interaction.
7. The weekly ChatGPT scan should still run a novelty search so the cache does not become a closed universe.

## State semantics

`state.json` records:
- exact check timestamps;
- HTTP health;
- normalized page hashes;
- last detected page change;
- consecutive failures;
- manual-review flags.

A page hash change is only a **signal to inspect the page**, not evidence that a call opened or closed.

## Refresh schedule

The GitHub Action has two Monday schedule attempts. A successful run causes a second run within four hours to no-op. The second schedule therefore serves as a cheap backup if GitHub drops or delays the first scheduled run.

Manual `workflow_dispatch` is also enabled.
