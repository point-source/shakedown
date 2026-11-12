# Handoff: <screen or area name>

*Approved: <round n, YYYY-MM-DD> · Kit: §spec:ui-kit · Endpoint(s): <§spec:… / GET /api/…>*

## Purpose

<The operator question this screen answers, in one sentence.>

## Data

| Shown | From (field) | Format | When null or empty |
| --- | --- | --- | --- |
| <label> | `<endpoint>.<field>` | <web/src/lib/format.ts helper> | <what the page shows> |

## Layout

Per target size (390, 768, 1440): order of cards, columns, what collapses
or scrolls. Components used (kit names only).

## States

Every state has a golden at every size in both themes.

| State | Trigger (fixture) | What changes | Golden |
| --- | --- | --- | --- |
| default | `fixtures/<name>.ts` | — | `<test>-<size>-<theme>.png` |
| loading | no answer yet | Skeleton per card | |
| empty | <empty answer> | <empty-state copy> | |
| error / old data | refresh failed or answer older than 2× interval | warning Alert with data age | |
| long content | <long IDs, many rows> | truncation / "and N more" | |

## Copy

Every visible string, with the glossary terms it uses. Wording is
reviewed in place on the renders.

## Behavior

Refresh interval, what updates in place, interactions (tooltips, tabs),
and anything an implementer could otherwise guess.

## Open questions

<None, or Q-ids with status.>
