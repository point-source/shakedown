# Roadmap

> Work remaining to close the gap between the codebase and
> [SPEC.md](SPEC.md). Sections are in build-dependency order; each ends
> with a surface workstream and a **Verify:** block a reviewer can
> exercise end-to-end. Completed work is deleted from this file — the
> changelog records history.

## Pasted links read by the source plugin §road:source-links

Closes the pasted-links part of §spec:dashboard-settings: the
collection editor already asks `GET /api/source-link`; the server and
the plugins answer it.

### Link reading in the plugin interface and the API §road:source-link-api

Add the optional `query_from_link(url) -> str | None` to the source
plugin interface (`src/shakedown/plugins/base.py`), implement it in the
`ia` and `etree` plugins for archive.org `/details/`, `/search`, and
`/advancedsearch.php` links, report `reads_links` per type in
`GET /api/config`'s `source_types` (`src/shakedown/dashboard_config.py`),
and serve `GET /api/source-link?type=&url=` (`src/shakedown/server.py`).
Add the hook to §spec:source-plugins and the endpoint to §spec:serve.
§spec:dashboard-settings

**Verify:** `GET /api/source-link?type=ia&url=https://archive.org/details/GratefulDead`
answers `{query: "collection:GratefulDead"}`; a link from another site
answers `{query: null}`; an unknown type answers `404`. In the
dashboard, paste an archive.org search link into a new collection's
Search: it becomes the search, and the collection saves and syncs it.

## Remove the source login §road:remove-source-login

Closes §spec:dashboard-credentials: the archive.org login is withdrawn
(operator decision, 2026-11-25) because it unlocks nothing Shakedown
downloads. The dashboard already offers none; this takes the built
backend out.

### Take the login out of the API and the runs §road:login-backend

Delete `PUT`/`DELETE /api/credentials` (`src/shakedown/dashboard_credentials.py`,
`src/shakedown/server.py`), the credential status and `takes_credentials`
in `GET /api/config` (`src/shakedown/dashboard_config.py`), the
`credentials` field of `POST /api/setup`, credential storage
(`src/shakedown/credentials.py`, `src/shakedown/db.py`) and its moves on
save (`src/shakedown/config_save.py`, `src/shakedown/edit_lock.py`), the
`ia` plugin's login and `takes_credentials` (`src/shakedown/plugins/ia/plugin.py`,
`src/shakedown/plugins/base.py`), and Validate's login check. A file that
still names a source's `auth` loads with the key ignored, and a
dashboard save leaves it out. `serve` drops an existing
`source_credentials` table on open. Update §spec:source-plugins,
§spec:configuration, §spec:state, §spec:serve,
§spec:dashboard-run-controls, §spec:setup-readiness-validation, and
§spec:release-validation-gate to match, and delete the credential tests.
§spec:dashboard-credentials

### Drop the IA_* settings from the docs and examples §road:login-docs

Remove `auth` from `shakedown.example.yaml`, the `IA_EMAIL` /
`IA_PASSWORD` lines from `docker-compose.example.yaml` and
§spec:deployment, and the restricted-collection login step from
`README.md`. Depends on §road:login-backend. §spec:dashboard-credentials

**Verify:** `PUT /api/credentials` answers `404`; `GET /api/config` has
no `credentials` and no `takes_credentials`. A database that held a
stored login has no `source_credentials` table after `serve` starts. A
file whose source names `auth` still syncs, and a dashboard save writes
it without the key. With `IA_EMAIL` and `IA_PASSWORD` set, a sync makes
no login request. A sync of a collection with a stream-only show
mirrors its public files and lists the private ones as refused.
