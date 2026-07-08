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
