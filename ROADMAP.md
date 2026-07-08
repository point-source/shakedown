# Roadmap

> Work remaining to close the gap between the codebase and
> [SPEC.md](SPEC.md). Sections are in build-dependency order; each ends
> with a surface workstream and a **Verify:** block a reviewer can
> exercise end-to-end. Completed work is deleted from this file — the
> changelog records history.

## Serve auth conformance

### §road:serve-bearer-auth

Switch mutating-endpoint authentication from the custom
`X-Shakedown-Token` header to standard `Authorization: Bearer` tokens
(`src/shakedown/server.py`, `tests/test_server.py`,
`docker-compose.example.yaml`). §spec:serve

**Verify:** Run `shakedown serve` with `SHAKEDOWN_API_TOKEN` set:
`POST /sync` with `Authorization: Bearer <token>` triggers a sync and
without it is rejected. Unset the token and confirm mutating endpoints
report disabled (not open) while `GET /healthz`, `/status`, and
`/metrics` remain reachable.

## Real-source end-to-end check §road:e2e-real-source

### Opt-in network test §road:e2e-real-source-test

Add the opt-in `network`-marked end-to-end test driving the `ia`
plugin against one pinned small public IA item through the full
lifecycle, deselected by the default test run
(`tests/test_e2e_real_source.py`, `pyproject.toml`, README run
instructions and item-replacement procedure). §spec:e2e-real-source

**Verify:** On a clean machine with network access and no IA
credentials, run `pytest -m network`: the test downloads the pinned
item once and passes, exercising sync → hardlink staging → no-op
re-sync → restage after a layout change → disappeared retention →
prune → forget. Run plain `pytest` and confirm zero network tests are
selected and the suite passes offline; confirm `.github/workflows/ci.yml`
is unchanged.
