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

## Governance

- `REQUIREMENTS.md` (repo root) holds the problem: what users need and the
  constraints, including the deployment status. `SPEC.md` holds the
  design that meets it. `ROADMAP.md` lists the work still open.
- Every `##` heading ends with its file's slug: `§req:` in
  REQUIREMENTS.md, `§spec:` in SPEC.md, `§road:` in ROADMAP.md (for
  example `## Tag filling §spec:tag-filling`). Deeper headings may carry
  one. Slugs are lowercase words joined by `-`; headings carry no
  ordinal numbers.
- Refer to a section by its slug (`§spec:tag-filling`), never by a
  number. Every reference must match exactly one heading; keep a
  reference on one line.
- Every `##` section in SPEC.md has a status line right after its
  heading, exactly `*Status: not started*`, `*Status: in progress*` or
  `*Status: complete*`. Any detail goes in a plain sentence on the next
  line.
- Spec changes land in the same commit series as the code they
  describe.
- Development principles are maintained in kandev's shared prompts (the
  "principles" prompt) and injected into agent step prompts. They are
  not stored in this repo; do not create a `PRINCIPLES.md`.
- Run `tools/governance-lint.sh` from the repo root before committing a
  change to these files. It runs markdownlint-cli2 too when that is on
  your PATH; CI's governance-lint workflow always does.
