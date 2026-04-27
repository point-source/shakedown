# Shakedown

> *"Nothing shaking on Shakedown Street — used to be the heart of town."*

Self-hosted tool for durably mirroring music collections from open archives — Internet Archive's GratefulDead/etree collections, Phish.in, and similar — onto a local NAS, while keeping the on-disk archive decoupled from whatever library/streaming software you point at it.

See [`prd.md`](prd.md) for the full design rationale.

## What it does

- Maintains a local mirror of one or more remote source collections, refreshed on a schedule.
- Records the source's manifest at fetch time so "do I already have this?" is a property of the database, not the bytes on disk. Library tools that retag files in place do not trigger re-downloads.
- Materializes a clean, library-ready directory tree as **hardlinks** under `/data/library/`. Beets, Lidarr, Picard, etc. can consume this tree without modification.

## Architecture in one paragraph

Three layers, two on-disk trees, one process. The **archive layer** at `/data/archive/` is Shakedown's domain — immutable from your perspective, source of truth for the state DB. The **library staging tree** at `/data/library/` is hardlink-only and points back at the archive's inodes; library tools read from here. Whatever happens in `/data/library/` (renames, retags, deletes) does not affect the archive copy. Sync compares the source's *current* manifest against the manifest Shakedown recorded at fetch time — never on-disk bytes — so library tools are free to mutate files in place.

## Quick start (Docker)

```bash
git clone https://github.com/yourorg/shakedown
cd shakedown
docker build -t shakedown:latest .
cp shakedown.example.yaml ./shakedown.yaml      # edit to taste
mkdir -p ./data/{archive,library}

docker run --rm \
  -v $PWD/shakedown.yaml:/config/shakedown.yaml:ro \
  -v $PWD/data/archive:/data/archive \
  -v $PWD/data/library:/data/library \
  shakedown:latest sync --collection grateful-dead
```

## QNAP / Container Station setup (no SSH required)

1. **File Station:** create `/share/data/{archive,library,music,shakedown-config}` — all under one share so they share a filesystem (hardlinks require this; PRD §4).
2. **File Station:** upload an initial `shakedown.yaml` (start from [`shakedown.example.yaml`](shakedown.example.yaml)) to `/share/data/shakedown-config/`.
3. **Container Station → Applications → Create:** paste [`docker-compose.example.yaml`](docker-compose.example.yaml). Set `IA_EMAIL` / `IA_PASSWORD` env vars if you have a download-restricted collection. Click Create.
4. **Day-to-day operation:** edit `shakedown.yaml` via File Station for source/collection changes; edit the compose YAML in Container Station for schedule changes; check Container Station for logs.

## CLI

```text
shakedown sync       [--source S] [--collection C] [--dry-run]
shakedown status     [--json]
shakedown verify     [--source S] [--collection C] [--deep] [--reconform] [--list] [--yes]
shakedown restage    [--source S] [--collection C]
shakedown reconcile
shakedown item show <identifier>
shakedown item refetch <identifier>
shakedown item forget <identifier>
shakedown serve      [--host HOST] [--port PORT]    # optional HTTP control plane
```

`shakedown verify --deep` is the **only** command that hashes on-disk bytes. It is operator-invoked, never scheduled. Drift (e.g. from Beets retagging in place) is informational, not an error. `--reconform` re-fetches drifted files from the source if you want byte-level fidelity to upstream.

## Writing a source plugin

A source plugin implements `shakedown.plugins.base.SourcePlugin`:

```python
from shakedown.plugins.base import SourcePlugin, ItemDescriptor, FetchResult, VerifyResult
from shakedown.plugins.registry import register

@register
class MyPlugin(SourcePlugin):
    type_name = "mything"
    template_fields = ("identifier", "date", "venue")

    def discover(self, collection):
        for thing in remote_api.list(collection.query):
            yield ItemDescriptor(
                identifier=thing.id,
                manifest=Manifest(files=tuple(...)),
                metadata={...},
            )

    def fetch(self, item, dest_dir, format_filters, exclude_filters):
        # Download into dest_dir; return FetchResult(success=True, bytes_downloaded=N).
        ...

    def verify(self, item, archive_path):
        # Existence-only re-check. Do NOT hash bytes here — that's `verify --deep`'s job.
        ...
```

Reference your plugin via `type: mything` in `shakedown.yaml`. See [`src/shakedown/plugins/ia/plugin.py`](src/shakedown/plugins/ia/plugin.py) for the IA implementation.

## Development

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e '.[dev,serve]'
.venv/bin/pytest -v
```

## License

MIT.
