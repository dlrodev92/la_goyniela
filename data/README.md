# Team catalogue

`team-catalog.json` is a UTF-8, versioned search catalogue for 2026/27
(snapshot: 2026-09-29). It contains 212 unique club identities and all 211 FIFA
member-association national teams. The club scope covers the Spanish domestic
competitions plus the league phases of the UEFA Champions League, UEFA Europa
League, and UEFA Conference League.

## UEFA identity model

Each club has one identity and one primary `competition_id`. The eight Spanish
clubs that also participate in UEFA remain in their domestic group and receive
only a `uefa_team_id`; they are not duplicated in a UEFA group. Each UEFA
competition's metadata records all 36 league-phase slots as newly created
identities plus `deduplicated_existing` references:

| Primary group | New identities | Reused domestic identities | Slots |
| --- | ---: | ---: | ---: |
| UEFA Champions League | 31 | 5 | 36 |
| UEFA Europa League | 34 | 2 | 36 |
| UEFA Conference League | 35 | 1 | 36 |

This is intentionally not a general competition-membership model. A database
migration can use the recorded UEFA IDs and deduplication metadata later.

The original request mentioned four men's Primera Federación groups. The RFEF
officially operates **two groups of 20** in 2026/27, so the data follows the
official competition rather than the requested number.

## Sources and update workflow

- Competition membership is tied to the official LALIGA, RFEF, Liga F, and FIFA
  URLs stored on each record and in the competition metadata.
- UEFA membership is pinned to the official club pages for the 2026/27 league
  phase only; qualifying-round entries are excluded.
- `team-catalog.sources.json` is the curated source snapshot. Wikipedia season
  pages were used only as a technical extraction aid for club article/media
  resolution; they are not the authority for competition membership.
- National-team flags come from the MIT-licensed `lipis/flag-icons` repository at
  the exact commit recorded in each flag's provenance.
- Run `python3 scripts/build_team_catalog.py --refresh-assets` after curating the
  source snapshot, or use `--refresh-missing-assets` to fetch only absent files
  (requires Pillow, `ffmpeg`, and network access). Normal builds and `--check`
  are offline. Then run
  `python3 scripts/build_team_catalog.py --check` and
  `python3 -m unittest tests.test_team_catalog`.

## Asset rights

All assets are normalized to a flat directory of 512×512 RGBA PNG files. Flags
are generated from the version-pinned MIT flag set. Club badges are trademarks;
their presence here does **not** grant redistribution or commercial-use rights.
Before production distribution, obtain permission from the relevant clubs or
replace the files with licensed assets. The manifest records the technical
source and rights status for every file.
