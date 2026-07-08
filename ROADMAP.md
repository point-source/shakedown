# Roadmap

> Work remaining to close the gap between the codebase and
> [SPEC.md](SPEC.md). Sections are in build-dependency order; each ends
> with a surface workstream and a **Verify:** block a reviewer can
> exercise end-to-end. Completed work is deleted from this file — the
> changelog records history.

## Plugin seam proof

Success criterion 8 requires a documented interface and a second plugin
written against it; neither exists yet.

### §road:plugin-interface-doc

Write the source-plugin interface documentation (`docs/plugins.md`,
linked from `README.md`) sufficient to author a plugin without reading
the IA plugin's internals. §spec:source-plugins. Depends on
§road:core-atomic-fetch and §road:core-retry-backoff (the documented
contract must reflect core-owned durability and backoff).

### §road:etree-plugin

Implement and register the etree source plugin against the documented
interface only (`src/shakedown/plugins/etree/`, `tests/test_etree_plugin.py`).
§spec:source-plugins. Depends on §road:plugin-interface-doc.

**Verify:** Add a `type: etree` source with one collection to
`shakedown.yaml` and run `shakedown sync`; confirm items mirror into the
archive and library trees and appear in `shakedown status`. Confirm via
review that the plugin was written from `docs/plugins.md` alone, without
reference to the IA plugin's internals.

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
