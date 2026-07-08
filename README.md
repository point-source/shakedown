# Shakedown

> *"Nothing shaking on Shakedown Street — used to be the heart of town."*

Self-hosted tool for durably mirroring music collections from open archives — Internet Archive's GratefulDead/etree collections, Phish.in, and similar — onto a local NAS, while keeping the on-disk archive decoupled from whatever library/streaming software you point at it.

See [`REQUIREMENTS.md`](REQUIREMENTS.md) for the problem space and [`SPEC.md`](SPEC.md) for the full design rationale.

## What it does

- Maintains a local mirror of one or more remote source collections, refreshed on a schedule.
- Records the source's manifest at fetch time so "do I already have this?" is a property of the database, not the bytes on disk. Library tools that retag files in place do not trigger re-downloads.
- Materializes a clean, library-ready directory tree as **hardlinks** under `/data/library/`. Beets, Lidarr, Picard, etc. can consume this tree without modification.

## Architecture in one paragraph

Three layers, two on-disk trees, one process. The **archive layer** at `/data/archive/` is Shakedown's domain — immutable from your perspective, source of truth for the state DB. The **library staging tree** at `/data/library/` is hardlink-only and points back at the archive's inodes; library tools read from here. Whatever happens in `/data/library/` (renames, retags, deletes) does not affect the archive copy. Sync compares the source's *current* manifest against the manifest Shakedown recorded at fetch time — never on-disk bytes — so library tools are free to mutate files in place.

## Quick start (Docker)

Replace `OWNER` below with the GitHub user/org that hosts your fork.

```bash
docker pull ghcr.io/OWNER/shakedown:latest

cp shakedown.example.yaml ./shakedown.yaml      # edit to taste
mkdir -p ./data/{archive,library}

docker run --rm \
  -v $PWD/shakedown.yaml:/config/shakedown.yaml:ro \
  -v $PWD/data/archive:/data/archive \
  -v $PWD/data/library:/data/library \
  ghcr.io/OWNER/shakedown:latest sync --collection grateful-dead
```

To build from source instead:

```bash
git clone https://github.com/OWNER/shakedown
cd shakedown
docker build -t shakedown:local .
```

## Container images & releases

Images are published to **`ghcr.io/<owner>/shakedown`** by `.github/workflows/ci.yml` on every push to `main`. Each push produces three tags:

| Tag | Meaning |
| --- | --- |
| `latest` | Most recent build of the default branch. Auto-updates. |
| `main` | Same as `latest` today; pin here if you ever add release branches. |
| `sha-<short>` | Immutable per-commit tag. Pin to this for reproducible deployments. |

Builds are multi-arch (`linux/amd64` + `linux/arm64`), so the same tag works on Intel QNAPs (TS-x73/x83), Atom/Celeron NUCs, and ARM QNAPs (TS-x33/x53D).

PRs build the image as a smoke test but do **not** push.

### One-time setup for a fresh fork

The workflow runs on `GITHUB_TOKEN` and needs no extra secrets, but two manual steps are required after the first successful push:

1. **Make the package public.** GHCR images default to private. Go to your repo → *Packages* → `shakedown` → *Package settings* → *Change visibility* → *Public*. Without this, `docker pull` requires login.
2. **Confirm Actions has write access.** Same package settings page → *Manage Actions access* → ensure your repo is listed with the *Write* role. The workflow needs this to keep updating the package after the initial publish.

Tag-driven releases (e.g. `git tag v0.2.0 && git push --tags` → `ghcr.io/.../shakedown:0.2.0`) are not enabled by default. To opt in later, add `tags: ['v*']` to the workflow's `on.push` trigger and add `type=semver,pattern={{version}}` to the `metadata-action` config.

## QNAP / Container Station setup (no SSH required)

1. **File Station:** create `/share/data/{archive,library,music,shakedown-config}` — all under one share so they share a filesystem (hardlinks require this; SPEC.md §spec:system-shape).
2. **File Station:** upload an initial `shakedown.yaml` (start from [`shakedown.example.yaml`](shakedown.example.yaml)) to `/share/data/shakedown-config/`.
3. **Container Station → Applications → Create:** paste [`docker-compose.example.yaml`](docker-compose.example.yaml). For a download-restricted collection, uncomment that job's `environment` label and set `IA_EMAIL` / `IA_PASSWORD`. Click Create.
4. **How syncs run:** ofelia is the only long-running service. On each scheduled tick it launches a *fresh* shakedown container that runs one `sync` and exits — matching shakedown's one-shot design, so there is no idle service to attach to. Nothing syncs on first `up`; each collection syncs only on its own schedule.
5. **Sync now (without waiting for a tick):** either uncomment the `shakedown-serve` control plane and `POST /sync` — `curl -XPOST -H "Authorization: Bearer $SHAKEDOWN_API_TOKEN" "http://host:8080/sync?collection=grateful-dead"` (no SSH) — or start a one-off container from Container Station → Create with command `sync --collection <name>`. Over SSH the equivalent is `docker run --rm` with the same mounts/env; SSH stays supported, it's just never required.
6. **Day-to-day operation:** edit `shakedown.yaml` via File Station for source/collection changes; edit the ofelia labels in the compose YAML (Container Station) for schedule changes; check Container Station for logs — each tick's container logs show that run.

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

A source plugin teaches Shakedown how to talk to one kind of remote archive. It
implements three methods on `shakedown.plugins.base.SourcePlugin` — `discover`,
`fetch`, `verify` — plus a `type_name` and `template_fields` declaration, and
registers itself. The core owns temp-dir lifecycle, atomic archive promotion,
retries/backoff, staging, and the database, so plugins stay small.

The full contract, the data types, the filter and failure-classification rules,
registration, and a complete worked example live in
[`docs/plugins.md`](docs/plugins.md) — the authoritative, self-contained guide.
Reference your plugin from `shakedown.yaml` with `type: <your type_name>`.

## Development

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e '.[dev,serve]'
.venv/bin/pytest -v
```

The default `pytest` run is fully offline (real archive.org HTTP is mocked), so
it is safe to run anywhere and is the run CI executes.

### Real-source end-to-end check

One opt-in test (`tests/test_e2e_real_source.py`, marked `network`) drives the
`ia` plugin against the real Internet Archive through the whole lifecycle —
sync → hardlink staging → no-op re-sync → restage after a layout change →
disappeared retention → prune → forget. It exists to catch what the offline
suite can't: archive.org's real API drifting from the fixtures, and integration
seams that only misbehave with real files. Run it deliberately before tagging a
release; it is **not** part of CI.

```bash
.venv/bin/pytest -m network        # runs only the network check
```

It needs network access but **no** IA credentials. It downloads one small,
pinned, public item (a few hundred KB) once into a throwaway temp tree, so it can
never touch a real deployment's data. A network or upstream failure fails the
check loudly — it never skips itself into a false pass.

**Pinned item and replacement.** The item is pinned by identifier
(`testmp3testfile`, Internet Archive's canonical MP3 test item) rather than
queried live, so runs are comparable over time. If it ever disappears upstream
the check fails (it does not skip). To re-pin, choose another item that is
public and unrestricted (no `access-restricted` flag in its `metadata`), small
(a few hundred KB), and long-lived, then update the `PINNED_*` constants at the
top of `tests/test_e2e_real_source.py` to match the new item's metadata. The
full procedure is documented in that file's module docstring.

## License

MIT.
