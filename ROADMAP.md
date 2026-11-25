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
