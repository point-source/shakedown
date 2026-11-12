# Shakedown

> *"Nothing shaking on Shakedown Street — used to be the heart of town."*

Self-hosted tool for durably mirroring music collections from open archives — Internet Archive's GratefulDead/etree collections, Phish.in, and similar — onto a local NAS, while keeping the on-disk archive decoupled from whatever library/streaming software you point at it.

See [`REQUIREMENTS.md`](REQUIREMENTS.md) for the problem space and [`SPEC.md`](SPEC.md) for the full design rationale.

## What it does

- Maintains a local mirror of one or more remote source collections, refreshed on a schedule.
- Records the source's manifest at fetch time so "do I already have this?" is a property of the database, not the bytes on disk. Library tools that retag files in place do not trigger re-downloads.
- Materializes a clean, library-ready directory tree as **hardlinks** under `/data/library/`. Point a music server such as Navidrome at it to listen; Beets, Lidarr, Picard, etc. can also read it without modification.

## Architecture in one paragraph

Three layers, two on-disk trees, one process. The **archive layer** at `/data/archive/` is Shakedown's domain — immutable from your perspective, source of truth for the state DB. The **library staging tree** at `/data/library/` is hardlink-only and points back at the archive's inodes; library tools read from here. Renaming or deleting files in `/data/library/` does not affect the archive copy. Retagging a file in place changes the archive copy too, because both paths are the same bytes — but sync never reads the bytes, so it never re-downloads because of a retag (`verify --deep` reports it as drift). Sync compares the source's *current* manifest against the manifest Shakedown recorded at fetch time — never on-disk bytes — so library tools are free to mutate files in place.

## Quick start (Docker)

Set `IMAGE` to your fork's image (see [Container images & releases](#container-images--releases) for tags).

```bash
IMAGE=ghcr.io/OWNER/shakedown:latest   # OWNER = the GitHub user/org that hosts your fork
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
| `1.2.0` | That release. Use it in `SHAKEDOWN_IMAGE` to hold a version. |
| `1.2` | Newest `1.2.x` release. Moves. |
| `latest` | Newest release. Use it in `SHAKEDOWN_IMAGE` for automatic updates: ofelia pulls before every tick, so each release reaches the NAS at its next run. |
| `sha-<short>` | The release commit. Immutable. |

The version comes from the git tag, so `shakedown --version` in the `1.2.0` image prints `1.2.0`. Check it to see which release your NAS runs.

Builds are multi-arch (`linux/amd64` + `linux/arm64`), so the same tag works on Intel QNAPs (TS-x73/x83), Atom/Celeron NUCs, and ARM QNAPs (TS-x33/x53D).

Pull requests build the image as a smoke test but do **not** push. Pushes to `main` run the tests only. Running the workflow by hand (*Actions → CI → Run workflow*) publishes only a `sha-<short>` tag.

### One-time setup for a fresh fork

The workflow runs on `GITHUB_TOKEN` and needs no extra secrets, but two manual steps are required after the first release publishes:

1. **Make the package public.** GHCR images default to private. Go to your repo → *Packages* → `shakedown` → *Package settings* → *Change visibility* → *Public*. Without this, `docker pull` requires login.
2. **Confirm Actions has write access.** Same package settings page → *Manage Actions access* → ensure your repo is listed with the *Write* role. The workflow needs this to keep updating the package after the initial publish.

## QNAP / Container Station setup (no SSH required)

1. **File Station:** create `/share/data/{archive,library,navidrome,shakedown-config}` — all under one share. The compose file mounts `/share/data` into the container once, as `/data`: hardlinks need archive and library on one filesystem *and* one mount (SPEC.md §spec:system-shape). Keep it that way if you edit the volumes.
2. **Ownership (one time):** every Shakedown container runs as one user, `SHAKEDOWN_USER` (`uid:gid`, default `1000:1000`, the image's built-in user). That user must own the archive and library folders, or runs fail with permission errors. Over SSH: `chown -R 1000:1000 /share/data/archive /share/data/library`. Without SSH: in Container Station, run a one-off `busybox` container as root with `/share/data` mounted at `/data` and command `chown -R 1000:1000 /data/archive /data/library`. Use your own `uid:gid` instead if you set `SHAKEDOWN_USER`.
3. **File Station:** upload an initial `shakedown.yaml` (start from [`shakedown.example.yaml`](shakedown.example.yaml)) to `/share/data/shakedown-config/`.
4. **Container Station → Applications → Create:** paste [`docker-compose.example.yaml`](docker-compose.example.yaml) and set `SHAKEDOWN_IMAGE` to your fork's image, e.g. `ghcr.io/<owner>/shakedown:latest` — Create fails until it is set. With `latest`, Shakedown updates itself: ofelia pulls before each tick, and CI publishes a release only after its tests pass. To hold a version instead, use a release tag such as `1.1.0`. For a download-restricted collection, uncomment that job's `environment` label and set `IA_EMAIL` / `IA_PASSWORD`. Click Create.
5. **How syncs run:** ofelia is the only long-running Shakedown service. On each scheduled tick it launches a *fresh* shakedown container that runs one `sync` and exits — matching shakedown's one-shot design, so there is no idle service to attach to. Nothing syncs on first `up`; each collection syncs only on its own schedule. If another run is still writing when a tick fires, that collection is rescheduled and syncs when that run finishes.
6. **Listen:** the stack also runs Navidrome on the library tree. Open `http://host:4533` and create the admin user on first visit. Navidrome's folder watcher picks up each newly synced show by itself, so no handoff is configured.
7. **Sync now (without waiting for a tick):** either uncomment the `shakedown-serve` control plane and `POST /sync` — `curl -XPOST -H "Authorization: Bearer $SHAKEDOWN_API_TOKEN" "http://host:8080/sync?collection=grateful-dead"` (no SSH); it answers `202` at once and the sync runs in the background (see "The serve control plane" below) — or start a one-off container from Container Station → Create with command `sync --collection <name>`, the same mounts, and the same user as `SHAKEDOWN_USER`. Over SSH the equivalent is `docker run --rm` with the same mounts/env; SSH stays supported, it's just never required.
8. **Confirm the schedule works:** open Container Station → `shakedown-ofelia` → *Logs*. Expect one `New job registered` line per collection. A missing job or a registration error means that job's labels are wrong: every label of a job must start with the same `ofelia.job-run.<name>.` prefix. To see a tick end to end, set one job's `schedule` a few minutes ahead (e.g. `0 30 14 * * *` runs daily at 14:30), wait for that job's `Started` line, its `StdOut:` output, and a `Finished in … failed: false` line. This runs a real sync. Then restore the schedule.
9. **Day-to-day operation:** edit `shakedown.yaml` via File Station for source/collection changes; edit the ofelia labels in the compose YAML (Container Station) for schedule changes. For logs, read the `shakedown-ofelia` container's log: each tick's container is deleted when it finishes, so ofelia's log is the record of every run — when it started, its output, and whether it failed.

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
shakedown serve      [--host HOST] [--port PORT]    # optional HTTP control plane
```

`shakedown validate` is the readiness preflight to run before the first large mirror: it checks config, path writability, that archive files can be hardlinked into the library, source/collection reachability, credentials, layout collision-risk, and handoff configuration, then reports one pass/fail per source and collection. It never downloads a collection and leaves nothing behind; a pass means "ready to attempt a sync," not "already mirrored." Handoff checks are non-mutating by default — pass `--live-handoff` to send a marked test webhook or run the configured command with a marked test payload.

Writing commands take turns: one that finds another run still writing exits 1 at once. A `sync --collection` refused this way is also rescheduled: it logs that, and `shakedown status` shows `rescheduled: first skipped <time>; runs when the current sync finishes` for that collection until a sync finishes it. `status --json` and `GET /status` carry the same as `"rescheduled": {"since": "<time>"}`, or `null`. A sync without `--collection`, `sync --refresh-metadata`, `POST /sync`, and the other writing commands are not rescheduled.

When a sync finishes its own work, it catches up: it reads `shakedown.yaml` again and syncs every rescheduled collection, oldest skip first, before it exits. Each catch-up is an ordinary sync with its own run, handoff, and failure alert; its log lines say `catch-up of the sync skipped at <time>`. A collection no longer in the config is dropped from the list, with a warning. If the config is invalid at that point, nothing is caught up, the list is kept, and the sync exits 1. The sync's exit code includes its catch-ups: it exits 1 when any of them failed, and the log names the collection. `sync --refresh-metadata`, `sync --dry-run`, and the other writing commands never catch up.

`shakedown verify --deep` is the **only** command that hashes on-disk bytes. It is operator-invoked, never scheduled. Drift (e.g. from Beets retagging in place) is informational, not an error. `--reconform` re-fetches drifted files from the source if you want byte-level fidelity to upstream.

### The serve control plane

`shakedown serve` is an optional HTTP API for triggers and monitoring without SSH. `GET /healthz`, `GET /status` (the same JSON as `status --json`), `GET /metrics` (Prometheus), and a default `POST /validate` are open. `POST /sync`, `POST /verify`, and `POST /validate?live_handoff=true` need `Authorization: Bearer $SHAKEDOWN_API_TOKEN`; with no token set they are disabled (`503`).

- `POST /sync?source=&collection=` and `POST /verify?source=&collection=&deep=&reconform=` start a separate one-shot run, the same run the scheduler starts. They answer `202` once it has started: `{"started": "sync", "source": null, "collection": "grateful-dead", "run_ids": [42]}`. `run_ids` holds one id per collection the sync covers; a verify has none (`[]`).
- The result is not in the response. Poll `GET /status`: each collection's `last_run.run_id` names its latest run, and `finished_at` is set when it is done. A run that crashes or is killed still gets its `finished_at`, with the reason in `errors`.
- If another run holds the writer lock (a scheduler tick, a manual run, an earlier trigger), the answer is `409` "a run is in progress; try again when it finishes". Nothing is queued or rescheduled: retry once it finishes.
- A triggered sync catches up rescheduled collections when its own work is done, like any sync. Those runs are not in `run_ids`; they show in `GET /status` as each collection's `last_run`, and a failed catch-up makes the sync count as `fail`. If the run is killed during a catch-up, that catch-up's `finished_at` stays empty until the collection syncs again.
- serve reads `shakedown.yaml` at every request, so an edit applies to the next request without a restart. While the file is invalid, every endpoint that needs it answers `503` with the config error, naming the key; fix the file and retry.
- `shakedown_sync_runs_total{result="ok"|"fail"}` counts triggered syncs when they exit.

The serve container starts runs itself, so it needs the same image, mounts, user, and credentials env as the scheduled job.

### Library handoff (optional)

A collection's `on_complete` can call a webhook or run a command after a sync that stages new shows, for a tool of your own that should hear about them. Listening needs none: Navidrome's folder watcher finds new shows by itself. A handoff that is not delivered never fails the sync; its shows show in `status` and go out again with the next sync. See [docs/handoff.md](docs/handoff.md) for the delivery rules and the payload.

### Upgrading

- **Library folder names (this release):** every `library_layout` field now has path-unsafe characters replaced, even without `|sanitize`, so a folder built from a raw field that held a `/`, `:` or a list may get a new name. Run `shakedown restage --collection <name>` once per collection after upgrading to rebuild a clean library tree. The archive is untouched.

## Writing a source plugin

A source plugin teaches Shakedown how to talk to one kind of remote archive. It
implements four methods on `shakedown.plugins.base.SourcePlugin` — `list_items`,
`lookup_item`, `fetch`, `verify` — plus a `type_name` and `template_fields`
declaration, and registers itself. Each method answers a single request. The core
owns parallel lookups under the politeness budget, retries/backoff, isolating one
bad item from the rest, choosing which files to mirror, temp-dir lifecycle,
atomic archive promotion, staging, and the database, so plugins stay small.

The full contract, the data types, the failure-classification rules,
registration, and a complete worked example live in
[`docs/plugins.md`](docs/plugins.md) — the authoritative, self-contained guide.
Reference your plugin from `shakedown.yaml` with `type: <your type_name>`.

## Development

```bash
uv sync --python 3.12 --extra dev --extra serve
.venv/bin/pytest -v
```

Dependencies are pinned in `uv.lock`; CI and the Docker image install from it with `uv sync --locked`, which fails if the lockfile is out of date. After changing dependencies in `pyproject.toml`, run `uv lock` and commit the updated lockfile. `uv lock --upgrade` moves every pin to the newest release.

The default `pytest` run is fully offline (real archive.org HTTP is mocked), so
it is safe to run anywhere and is the run CI executes.

### Release workflow tests

`tests/test_release_workflows.py` holds the workflows a release must not break:
first-setup readiness, unsafe config rejection, partial-failure recovery, and
the ordinary sync-to-library path. They run on synthetic sources in throwaway
paths, as part of the default `pytest` run. CI runs them on every pull request
and release tag, and publishes the image only after they pass. Each test is
named for its workflow, so a failure names what broke (for example
`test_partial_failure_recovery_restage`).

Before tagging a release, also run the real-source check below. CI does not run
it.

### Real-source end-to-end check

One opt-in test (`tests/test_e2e_real_source.py`, marked `network`) drives the
`ia` plugin against the real Internet Archive through the whole lifecycle —
sync → hardlink staging → no-op re-sync → restage after a layout change →
disappeared retention → prune. It exists to catch what the offline
suite can't: archive.org's real API drifting from the fixtures, and integration
seams that only misbehave with real files. Run it before tagging a release; CI
never runs it.

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
