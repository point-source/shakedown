# Agent guidance

## UI implementation rules

These apply to any change to the web dashboard in `web/`. The design
authority is §spec:ui-kit in SPEC.md, with the approved goldens in
`web/tests/__screenshots__/` and the handoff specs in `docs/ui/handoff/`.

- The approved golden plus its handoff spec is the spec. Build what they
  show; do not restyle or "improve" it.
- Use kit tokens (`web/src/index.css`) and kit components
  (`web/src/components/ui/`) only: no literal colors, sizes, spacing,
  radii, shadows, or fonts. A value the kit lacks is a kit change made
  in §spec:ui-kit first.
- Words on screen come from the Terms glossary in §spec:ui-kit; the page
  words every endpoint code in `web/src/lib/words.ts`.
- A screen is done when its goldens pass (`npm run test:screens` in
  `web/`) and every state its handoff spec lists exists. CI does not run
  the screenshot tests; run them before a UI change merges.
- A missing state or an ambiguous spec is never invented. Settle it from
  a named source (a kit pattern, a spec line, an approved screen) and
  record the call, or raise it as a question through the task's
  workflow.
- A failing golden is evidence: fix the code, or bring the difference to
  the operator. Never regenerate a baseline just to get green.
- Load no aesthetic or design skills while implementing.
