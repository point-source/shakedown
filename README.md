# Shakedown

> *"Nothing shaking on Shakedown Street — used to be the heart of town."*

Self-hosted tool for durably mirroring music collections from open archives — Internet Archive's GratefulDead/etree collections, Phish.in, and similar — onto a local NAS, while keeping the on-disk archive decoupled from whatever library/streaming software you point at it.

See [`REQUIREMENTS.md`](REQUIREMENTS.md) for the problem space and [`SPEC.md`](SPEC.md) for the full design rationale.

## What it does

- Maintains a local mirror of one or more remote source collections, refreshed on a schedule.
- Records the source's manifest at fetch time so "do I already have this?" is a property of the database, not the bytes on disk. Library tools that retag files in place do not trigger re-downloads.
- Materializes a clean, library-ready directory tree as **hardlinks** under `/data/library/`. Beets, Lidarr, Picard, etc. can consume this tree without modification.

## Architecture in one paragraph

Three layers, two on-disk trees, one process. The **archive layer** at `/data/archive/` is Shakedown's domain — immutable from your perspective, source of truth for the state DB. The **library staging tree** at `/data/library/` is hardlink-only and points back at the archive's inodes; library tools read from here. Renaming or deleting files in `/data/library/` does not affect the archive copy. Retagging a file in place changes the archive copy too, because both paths are the same bytes — but sync never reads the bytes, so it never re-downloads because of a retag (`verify --deep` reports it as drift). Sync compares the source's *current* manifest against the manifest Shakedown recorded at fetch time — never on-disk bytes — so library tools are free to mutate files in place.

## Quick start (Docker)

Set `IMAGE` to your fork's image and a released version (see [Container images & releases](#container-images--releases)).

```bash
IMAGE=ghcr.io/OWNER/shakedown:1.1.0   # OWNER = the GitHub user/org that hosts your fork
docker pull "$IMAGE"

cp shakedown.example.yaml ./shakedown.yaml      # edit to taste
mkdir -p ./data/{archive,library}

# Mount ./data once: separate archive and library mounts block hardlinks.
docker run --rm \
  -v $PWD/shakedown.yaml:/config/shakedown.yaml:ro \
  -v $PWD/data:/data \
  "$IMAGE" sync --collection grateful-dead
```

To build from source instead:

```bash
git clone https://github.com/OWNER/shakedown
cd shakedown
docker build -t shakedown:local .
```

## Container images & releases

`.github/workflows/ci.yml` publishes images to **`ghcr.io/<owner>/shakedown`** when you push a release tag (`git tag v1.2.0 && git push origin v1.2.0`), and only after the test job passes. A release `v1.2.0` produces:

| Tag | Meaning |
| --- | --- |
| `1.2.0` | That release. Pin this in `SHAKEDOWN_IMAGE`. |
| `1.2` | Newest `1.2.x` release. Moves. |
| `latest` | Newest release. Moves — don't use it in compose, since ofelia pulls before every tick. |
| `sha-<short>` | The release commit. Immutable. |

Builds are multi-arch (`linux/amd64` + `linux/arm64`), so the same tag works on Intel QNAPs (TS-x73/x83), Atom/Celeron NUCs, and ARM QNAPs (TS-x33/x53D).

Pull requests build the image as a smoke test but do **not** push. Pushes to `main` run the tests only. Running the workflow by hand (*Actions → CI → Run workflow*) publishes only a `sha-<short>` tag.

### One-time setup for a fresh fork

The workflow runs on `GITHUB_TOKEN` and needs no extra secrets, but two manual steps are required after the first release publishes:

1. **Make the package public.** GHCR images default to private. Go to your repo → *Packages* → `shakedown` → *Package settings* → *Change visibility* → *Public*. Without this, `docker pull` requires login.
2. **Confirm Actions has write access.** Same package settings page → *Manage Actions access* → ensure your repo is listed with the *Write* role. The workflow needs this to keep updating the package after the initial publish.

## QNAP / Container Station setup (no SSH required)

1. **File Station:** create `/share/data/{archive,library,music,shakedown-config}` — all under one share. The compose file mounts `/share/data` into the container once, as `/data`: hardlinks need archive and library on one filesystem *and* one mount (SPEC.md §spec:system-shape). Keep it that way if you edit the volumes.
2. **Ownership (one time):** every Shakedown container runs as one user, `SHAKEDOWN_USER` (`uid:gid`, default `1000:1000`, the image's built-in user). That user must own the archive and library folders, or runs fail with permission errors. Over SSH: `chown -R 1000:1000 /share/data/archive /share/data/library`. Without SSH: in Container Station, run a one-off `busybox` container as root with `/share/data` mounted at `/data` and command `chown -R 1000:1000 /data/archive /data/library`. Use your own `uid:gid` instead if you set `SHAKEDOWN_USER`.
3. **File Station:** upload an initial `shakedown.yaml` (start from [`shakedown.example.yaml`](shakedown.example.yaml)) to `/share/data/shakedown-config/`.
4. **Container Station → Applications → Create:** paste [`docker-compose.example.yaml`](docker-compose.example.yaml) and set `SHAKEDOWN_IMAGE` to your fork's image at a released version, e.g. `ghcr.io/<owner>/shakedown:1.1.0` — Create fails until it is set. To upgrade, change the version; `latest` would install every new release unreviewed, because ofelia pulls before each tick. For a download-restricted collection, uncomment that job's `environment` label and set `IA_EMAIL` / `IA_PASSWORD`. Click Create.
5. **How syncs run:** ofelia is the only long-running service. On each scheduled tick it launches a *fresh* shakedown container that runs one `sync` and exits — matching shakedown's one-shot design, so there is no idle service to attach to. Nothing syncs on first `up`; each collection syncs only on its own schedule.
6. **Sync now (without waiting for a tick):** either uncomment the `shakedown-serve` control plane and `POST /sync` — `curl -XPOST -H "Authorization: Bearer $SHAKEDOWN_API_TOKEN" "http://host:8080/sync?collection=grateful-dead"` (no SSH) — or start a one-off container from Container Station → Create with command `sync --collection <name>`, the same mounts, and the same user as `SHAKEDOWN_USER`. Over SSH the equivalent is `docker run --rm` with the same mounts/env; SSH stays supported, it's just never required.
7. **Confirm the schedule works:** open Container Station → `shakedown-ofelia` → *Logs*. Expect one `New job registered` line per collection. A missing job or a registration error means that job's labels are wrong: every label of a job must start with the same `ofelia.job-run.<name>.` prefix. To see a tick end to end, set one job's `schedule` a few minutes ahead (e.g. `0 30 14 * * *` runs daily at 14:30), wait for that job's start and stop lines, and check the stop line reports no failure or error. This runs a real sync. Then restore the schedule.
8. **Day-to-day operation:** edit `shakedown.yaml` via File Station for source/collection changes; edit the ofelia labels in the compose YAML (Container Station) for schedule changes. For logs, read the `shakedown-ofelia` container's log: each tick's container is deleted when it finishes, so ofelia's log is the record of every run — start, stop, the run's output, and whether it failed.

## CLI

```text
shakedown sync       [--source S] [--collection C] [--dry-run]
shakedown validate   [--source S] [--collection C] [--live-handoff] [--json]
shakedown status     [--json]
shakedown verify     [--source S] [--collection C] [--deep] [--reconform] [--list] [--yes]
shakedown restage    [--source S] [--collection C]
shakedown reconcile
shakedown item show <identifier>
shakedown item refetch <identifier>
shakedown item forget <identifier>
shakedown serve      [--host HOST] [--port PORT]    # optional HTTP control plane
```

`shakedown validate` is the readiness preflight to run before the first large mirror: it checks config, path writability, that archive files can be hardlinked into the library, source/collection reachability, credentials, layout collision-risk, and handoff configuration, then reports one pass/fail per source and collection. It never downloads a collection and leaves nothing behind; a pass means "ready to attempt a sync," not "already mirrored." Handoff checks are non-mutating by default — pass `--live-handoff` to send a marked test webhook or run the configured command with a marked test payload.

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

### Release validation gate

Before publishing a release, run the opt-in gate:

```bash
.venv/bin/shakedown release-validate
```

The full gate runs deterministic fake-source workflows in throwaway archive,
library, config, and state paths, then runs the pinned real Internet Archive
seam. Its final summary names every user workflow it exercised: setup readiness,
unsafe config rejection, handoff failure, partial-failure recovery,
sync-to-library staging, and the real-source IA seam. A risky scenario that
cannot run is a failure, not a silent skip.

Narrower modes are explicit:

```bash
.venv/bin/shakedown release-validate --deterministic-only
.venv/bin/shakedown release-validate --real-source-only
```

Use `--deterministic-only` when network access is intentionally unavailable.
Use `--real-source-only` when rechecking archive.org behavior after deterministic
coverage already passed. The release gate is not part of the default developer
test run or routine pull-request CI.

### Real-source end-to-end check

One opt-in test (`tests/test_e2e_real_source.py`, marked `network`) drives the
`ia` plugin against the real Internet Archive through the whole lifecycle —
sync → hardlink staging → no-op re-sync → restage after a layout change →
disappeared retention → prune → forget. It exists to catch what the offline
suite can't: archive.org's real API drifting from the fixtures, and integration
seams that only misbehave with real files. The full release gate runs this check;
it can also be reached directly when investigating the real-source seam.

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
