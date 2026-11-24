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

mkdir -p ./data/{archive,library,shakedown-config}
cp shakedown.example.yaml ./data/shakedown-config/shakedown.yaml   # edit to taste

# Mount ./data once: separate archive and library mounts block hardlinks.
# Shakedown reads /data/shakedown-config/shakedown.yaml unless --config says otherwise.
docker run --rm \
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
| `latest` | Newest release. Use it in `SHAKEDOWN_IMAGE` for automatic updates: the example stack's Watchtower checks once a day (12:00) and recreates both Shakedown containers from a newer image. |
| `sha-<short>` | The release commit. Immutable. |

The version comes from the git tag, so `shakedown --version` in the `1.2.0` image prints `1.2.0`. Check it to see which release your NAS runs.

Builds are multi-arch (`linux/amd64` + `linux/arm64`), so the same tag works on Intel QNAPs (TS-x73/x83), Atom/Celeron NUCs, and ARM QNAPs (TS-x33/x53D).

Pull requests build the image as a smoke test but do **not** push. Pushes to `main` run the tests only. Running the workflow by hand (*Actions → CI → Run workflow*) publishes only a `sha-<short>` tag.

### One-time setup for a fresh fork

The workflow runs on `GITHUB_TOKEN` and needs no extra secrets, but two manual steps are required after the first release publishes:

1. **Make the package public.** GHCR images default to private. Go to your repo → *Packages* → `shakedown` → *Package settings* → *Change visibility* → *Public*. Without this, `docker pull` requires login.
2. **Confirm Actions has write access.** Same package settings page → *Manage Actions access* → ensure your repo is listed with the *Write* role. The workflow needs this to keep updating the package after the initial publish.

## QNAP / Container Station setup (no SSH required)

1. **File Station:** create `/share/data/{archive,library,navidrome,shakedown-config}` — all under one share. The compose file mounts `/share/data` into each Shakedown container once, as `/data`: hardlinks need archive and library on one filesystem *and* one mount (SPEC.md §spec:system-shape). Keep it that way if you edit the volumes.
2. **Ownership (one time):** every Shakedown container runs as one user, `SHAKEDOWN_USER` (`uid:gid`, default `1000:1000`, the image's built-in user). That user must own the archive, library, and `shakedown-config` folders, or runs fail with permission errors and serve cannot save settings. Over SSH: `chown -R 1000:1000 /share/data/archive /share/data/library /share/data/shakedown-config`. Without SSH: in Container Station, run a one-off `busybox` container as root with `/share/data` mounted at `/data` and command `chown -R 1000:1000 /data/archive /data/library /data/shakedown-config`. Use your own `uid:gid` instead if you set `SHAKEDOWN_USER`.
3. **File Station:** upload an initial `shakedown.yaml` (start from [`shakedown.example.yaml`](shakedown.example.yaml)) to `/share/data/shakedown-config/`. Inside the containers it is `/data/shakedown-config/shakedown.yaml`, the path Shakedown reads by default. Give each collection that should sync on its own a `schedule` (see [The scheduler](#the-scheduler)).
4. **Container Station → Applications → Create:** paste [`docker-compose.example.yaml`](docker-compose.example.yaml) and set `SHAKEDOWN_IMAGE` to your fork's image, e.g. `ghcr.io/<owner>/shakedown:latest` — Create fails until it is set. Set `TZ` (e.g. `Europe/Berlin`) for schedules and log times; it defaults to UTC. If any collection is download-restricted, uncomment the `IA_*` lines on both `shakedown-serve` and `shakedown-scheduler` and set `IA_EMAIL` / `IA_PASSWORD`: both start runs. Click Create.
5. **What runs:**
   - `shakedown-serve`: the dashboard and control plane (see "The serve control plane" below). Its port is published on the NAS's loopback only; put it behind your reverse proxy.
   - `shakedown-scheduler`: starts each collection's scheduled syncs. Every run is a fresh one-shot `shakedown` process that exits when done. Nothing syncs on first `up`; each collection syncs only on its own schedule. If another run is still writing when a tick fires, that collection is rescheduled and syncs when that run finishes.
   - `watchtower` and `socket-proxy` (optional): with `latest`, Watchtower checks once a day at 12:00 (in `TZ`) and recreates both Shakedown containers from a newer image; CI publishes a release only after its tests pass. It updates nothing else, and reaches Docker only through the proxy, the one container that mounts the Docker socket. If you pin a release tag such as `1.1.0`, or update by hand, delete both services. `GET /api/health` shows which release serve runs.
6. **Listen:** the stack also runs Navidrome on the library tree. Open `http://host:4533` and create the admin user on first visit. Navidrome's folder watcher picks up each newly synced show by itself, so no handoff is configured.
7. **Sync now (without waiting for a tick):** either set `SHAKEDOWN_API_TOKEN` on `shakedown-serve` and `POST /sync` — `curl -XPOST -H "Authorization: Bearer $SHAKEDOWN_API_TOKEN" "https://<serve behind your proxy>/sync?collection=grateful-dead"` (no SSH; the proxy is the way in, or use `http://127.0.0.1:8080` on the NAS itself); it answers `202` at once and the sync runs in the background (see "The serve control plane" below) — or start a one-off container from Container Station → Create with command `sync --collection <name>`, the same image, the `/share/data:/data` mount, and the same user as `SHAKEDOWN_USER`. Over SSH the equivalent is `docker run --rm` with the same mount and env; SSH stays supported, it's just never required.
8. **Confirm the schedule works:** in Container Station, `shakedown-scheduler` shows *healthy* about a minute after start: its health check passes while the scheduler records a heartbeat. It shows *unhealthy* while `shakedown.yaml` is missing, and when the scheduler stops reporting. Its log names how many collections it planned and the next tick (`planned 2 scheduled collection(s) … next tick …`). `GET /api/health` and `GET /status` carry the same as `scheduler: {state, last_seen, release}`. To see a tick end to end, set one collection's `schedule` a few minutes ahead (e.g. `30 14 * * *` runs daily at 14:30), wait for its sync in the scheduler's log, then restore the schedule. This runs a real sync.
9. **Day-to-day operation:** edit `shakedown.yaml` via File Station for source, collection, and schedule changes; the scheduler and serve pick up a saved file within a minute, with no restart or redeploy. For logs, read the `shakedown-scheduler` container's log for scheduled runs and `shakedown-serve`'s for runs it started; every run's record is also in `shakedown status` and the dashboard.

## CLI

```text
shakedown sync       [--source S] [--collection C] [--dry-run]
shakedown validate   [--source S] [--collection C] [--live-handoff] [--json]
shakedown status     [--json]
shakedown verify     [--source S] [--collection C] [--deep] [--reconform] [--list] [--yes]
shakedown restage    [--source S] [--collection C]
shakedown reconcile
shakedown trim-history                               # drop old run error lines now, compact the database
shakedown item show <identifier>
shakedown item refetch <identifier>
shakedown serve      [--host HOST] [--port PORT]    # dashboard and HTTP control plane
```

`shakedown validate` is the readiness preflight to run before the first large mirror: it checks config, path writability, that archive files can be hardlinked into the library, source/collection reachability, credentials, layout collision-risk, and handoff configuration, then reports one pass/fail per source and collection. It never downloads a collection and leaves nothing behind; a pass means "ready to attempt a sync," not "already mirrored." Handoff checks are non-mutating by default — pass `--live-handoff` to send a marked test webhook or run the configured command with a marked test payload.

Writing commands take turns: one that finds another run still writing exits 1 at once. A `sync --collection` refused this way is also rescheduled: it logs that, and `shakedown status` shows `rescheduled: first skipped <time>; runs as a catch-up sync` for that collection until a sync finishes it; the catch-up runs when the current sync finishes its own work. `status --json` and `GET /status` carry the same as `"rescheduled": {"since": "<time>"}`, or `null`. A sync without `--collection`, `sync --refresh-metadata`, `POST /sync`, and the other writing commands are not rescheduled.

When a sync finishes its own work, it catches up: it reads `shakedown.yaml` again and syncs every rescheduled collection, oldest skip first, before it exits. Each catch-up is an ordinary sync with its own run (kind `catch_up` in `status`), handoff, and failure alert; its log lines say `catch-up of the sync skipped at <time>`. A collection no longer in the config is dropped from the list, with a warning. If the config is invalid at that point, nothing is caught up, the list is kept, and the sync exits 1. The sync's exit code includes its catch-ups: it exits 1 when any of them failed, and the log names the collection. `sync --refresh-metadata`, `sync --dry-run`, and the other writing commands never catch up. Like any sync of several collections, a catch-up runs `max_concurrent_collections` of them at a time. A collection still waiting for its turn has no run yet: `status` shows `waiting: since <time>; starts when a slot frees in the current sync` for it (`"waiting": {"kind": "catch_up", "since": "<time>"}` in `status --json`, `null` otherwise) and no recovery warning.

**Stopping a run.** `docker stop` (or Ctrl-C, a NAS shutdown, Container Station's Stop) is the right way to stop a sync, restage, reconcile, refetch, or reconform. The run starts nothing new, drops each download in progress at its next chunk (the files a show already finished stay in its temp folder, so the next run fetches only the rest), records the run as `stopped`, and exits 143 (SIGTERM) or 130 (SIGINT). A stopped run is not a failure: `status` shows `run stopped on request before it finished; the next run continues` (`last_run.result: "stopped"` in `status --json`), with no recovery warning and no failure alert. It removes nothing, keeps its rescheduled entry, and runs no catch-up; shows it fetched are handed off by the collection's next run. A second signal exits at once, and the next run cleans up as after a kill. A healthy download stops within a second, a stalled one within its 30 s read timeout: give containers a 120 s stop timeout (`stop_grace_period: 2m` in compose, `docker stop -t 120`). Docker's default 10 s is usually enough, and a run killed after it is still safe. Stopping `serve` stops the runs it started first (it waits up to 90 s). No init process (`--init`, tini) is needed: shakedown handles the signals itself, which also makes them reach it as the container's PID 1.

Every run records its progress in the state database every few seconds, whether or not `serve` is running: one entry per collection with its phase, counts, the shows being worked on, bytes, speed, and time left (`GET /api/activity`). A sync's run record moves at the same pace, so `status` mid-run shows the same counts as the dashboard. A run that is killed (SIGKILL, out of memory, host down) stops reporting: within about 20 seconds `status` shows its last run as `run ended unexpectedly (lost contact)` (`last_run.end_reason: "stopped_reporting"` in `status --json`), and the next writing command closes that run record at its last report, with the same error. Recording never slows or fails a run; a plain `verify` right after an upgrade records nothing until a writing command has upgraded the database. Every writing command also records, as it ends, each collection's complete shows and archive bytes; the dashboard draws 30 days of growth from them (`GET /api/library`).

Runs also record each source's network activity, kept 30 days: per minute, the bytes fetched, the most requests in flight, rate-limit hits, server errors, timeouts, connection errors, retries, and back-off seconds; and each of those events with when it happened (`GET /api/network`). Shakedown measures this itself, the same way for every source plugin. A run's `bytes_downloaded` (in `status` and on the dashboard) is the bytes it fetched from the source: a file fetched again after a failed checksum counts again, and a file kept from an interrupted attempt does not count. The per-minute bytes add up to the same total.

`shakedown verify --deep` is the **only** command that hashes on-disk bytes. It is operator-invoked, never scheduled. Drift (e.g. from Beets retagging in place) is informational, not an error. `--reconform` re-fetches drifted files from the source if you want byte-level fidelity to upstream.

### The scheduler

A collection's `schedule` in `shakedown.yaml` is a standard five-field cron expression (`30 3 * * *` is daily at 03:30), in the clock the top-level `timezone` names (an IANA name such as `Europe/Berlin`; without it, the container's local time). A collection with no `schedule` syncs only when someone starts it. `shakedown schedule` is the long-running command that starts each scheduled sync, as the same one-shot `sync --collection` run a tick always launched. It listens on no port and never writes the config. It notices a changed file within a minute, with no restart. While the file is invalid, it keeps the schedules it last read. Collections due in the same minute start in file order: the later ones find the first one writing and are rescheduled. About once a minute it records a heartbeat in the state database. `shakedown schedule --check` exits 1 when that heartbeat is missing or more than three minutes old, for a container health check. `serve` reads the heartbeat independently: `GET /status` and `GET /api/health` carry `scheduler: {state, last_seen, release}`. Needs attention lists the scheduler when it stops, and lists each scheduled sync that did not start within ten minutes. The failure webhook fires once per stop (`scheduler.stale`, see [docs/handoff.md](docs/handoff.md)).

### The serve control plane

`shakedown serve` is the long-running HTTP service behind the read-only web dashboard, and an API for triggers and monitoring without SSH. The example stack runs it enabled. `GET /healthz`, `GET /status` (`{"scheduler": {…}, "collections": [<the status --json list>]}`), `GET /metrics` (Prometheus), and the dashboard's `GET /api/*` endpoints are open. `POST /sync`, `POST /verify`, `POST /validate`, and `POST /api/history/trim` accept a request that either sends `Authorization: Bearer $SHAKEDOWN_API_TOKEN` or comes from the dashboard's own page (the browser marks it `Sec-Fetch-Site: same-origin`, and it is a JSON request). Anything else answers `403 {"code": "cross_site"}`. The token is optional: without it, only the dashboard can start runs. Browsers mark requests this way only over HTTPS or on localhost, so open the dashboard through your HTTPS proxy (or at `http://127.0.0.1:8080` on the NAS) to start runs from it. `POST /validate?live_handoff=true` can run your configured command, so it takes the token only.

The dashboard is serve's root page: open `/` through your proxy. It works under a path prefix such as `/shakedown/` with no setting. A serve started from a source checkout has no page (`GET /` answers `404`) until you build one: `cd web && npm ci && npm run build`, then copy `web/dist/*` into `src/shakedown/dashboard_web/` and restart serve. The API works without it.

- `POST /sync?source=&collection=` and `POST /verify?source=&collection=&deep=&reconform=` start a separate one-shot run, the same run the scheduler starts. They answer `202` once it has started: `{"started": "sync", "source": null, "collection": "grateful-dead"}`. Each collection's run starts when it gets a slot (`max_concurrent_collections`); `GET /status` shows its id as that collection's `last_run.run_id`, and a collection still waiting shows `waiting` instead.
- `POST /api/history/trim` starts `shakedown trim-history`: it drops the error lines of runs older than `keep_run_errors_days` (180 days unless set; runs keep their result, counts, and dates), then compacts the state database so the space returns to the disk. Compacting needs about the database's size free; without it the lines are still dropped and the run reports the failure. It answers `202 {"started": "trim"}`, or `409` while another run holds the writer lock. Runs that hold the lock drop old lines this way as they end, without compacting.
- The result is not in the response. Poll `GET /status`: each collection's `last_run.run_id` names its latest run, and `finished_at` is set when it is done. A run that crashes or is killed still gets its `finished_at`, with the reason in `errors`.
- If another run holds the writer lock (a scheduler tick, a manual run, an earlier trigger), the answer is `409` "a run is in progress; try again when it finishes". Nothing is queued or rescheduled: retry once it finishes.
- A triggered sync catches up rescheduled collections when its own work is done, like any sync. They show in `GET /status` as each collection's `last_run`, and a failed catch-up makes the sync count as `fail`. If the run is killed during a catch-up, `serve` closes that catch-up's run record as it does the run's own.
- serve reads `shakedown.yaml` at every request, so an edit applies to the next request without a restart. While the file is invalid, `/status`, `/metrics`, and the `POST` endpoints answer `503` with the config error, naming the key; fix the file and retry. The dashboard's `/api/*` endpoints keep answering from the last config this serve loaded and report the error in `GET /api/health`. A serve that has never loaded a valid config answers `503` there too. A config error names the key and the problem, never a value from the file.
- `shakedown_sync_runs_total{result="ok"|"stopped"|"fail"}` counts triggered syncs when they exit; `stopped` is one that exited 143 or 130 after a stop.

The serve container starts runs itself, so it needs the same image, data mount, user, and credentials env as the scheduler container.

#### Dashboard read endpoints

The dashboard's data comes from `GET /api/*` endpoints:

- They are read-only. They answer `GET` only (any other method is `405`), never start a run, never contact a source, never write, and never wait for a running sync. They read the state database, the config file, and the data volume's free space.
- They have no login, like `GET /status`. Your reverse proxy (for example Authelia) decides who opens them; do not publish serve's port past it. They return operational data only — counts, identifiers, paths, timings, error text — never credentials or tokens. serve sends no CORS headers.
- Every answer carries `generated_at`, the time serve computed it (ISO 8601 with UTC offset, like every time these endpoints return). serve computes each answer at most once per refresh interval (about 30 s; 5 s for the live view) and gives the same answer to every viewer, so many open dashboards cost what one does. A change can take up to that interval to show.
- `GET /api/health` answers `{generated_at, release, config: {valid, error, last_loaded_at}, validation: [{source, collection, status, at}]}`: the release of this serve, whether `shakedown.yaml` loads now (with the error naming the key when it does not, and when the config these endpoints answer from last loaded), and per collection the latest recorded `validate` outcome (`completed` or `failed_before_completion`) and its time, `null` when none is recorded. It reports validations already run; it never runs one.
- `GET /api/activity` answers `{generated_at, runs: [...], network: [...]}`, refreshed every 5 s. `runs` holds the running entries first, then entries that ended in the last ten minutes, newest first. Each entry: `activity_id`, `run_id` (the id `status` and `POST /sync` report; `null` for restage, verify, and reconcile), `source`, `collection`, `kind` (`sync`, `catch_up`, `refetch`, `reconform`, `restage`, `verify`, `reconcile`, `trim`), `started_by_serve`, `started_at`, `finished_at`, `last_heartbeat_at`, `last_progress_at`, `state` (`running` or `ended`), `result` (`null` while running, else `completed`, `completed_with_item_issues`, `failed_before_completion`, `stale_enumeration`, or `stopped`), `end_reason` (`null` or `stopped_reporting`), `phase` (`starting`, `listing`, `discovering`, `downloading`, `tagging`, `staging`, `notifying`, `verifying`, `finishing`), `items_listed`, `items_to_look_up`, `items_looked_up`, `items_to_fetch`, `items_done`, `items_failed`, `current_items`, `bytes_downloaded`, `bytes_to_fetch`, `speed_bytes_per_second` (last ~10 s), `eta_seconds`, and `runs_trimmed` and `bytes_freed` (a trim's figures, `null` for other kinds). A count a kind does not have is `null` (a restage lists nothing). A trim has no phase and `null` `source` and `collection`. `items_to_fetch`, `bytes_to_fetch`, and `eta_seconds` stay `null` until the listing's lookups are done; `eta_seconds` is also `null` while nothing downloads. An entry that has not reported for about 20 seconds shows as ended with `failed_before_completion` and `stopped_reporting`. `network` lists each source with a run in progress: `{source, speed_bytes_per_second, requests_in_flight, last_event}`, where `last_event` is that run's newest network event (shaped as in `GET /api/network`) or `null`.
- `GET /api/network?days=N` answers `{generated_at, bucket_seconds, sources: [{source, series, events, events_total}]}` for the last `N` days (1–30, default 30; anything else is `422`). `sources` lists every configured source block in config order. `series` holds `{t, bytes, peak_requests_in_flight, rate_limited, server_errors, timeouts, connection_errors, retries, backoff_seconds}` per bucket with recorded activity, oldest first; it is empty for a source with none. `bucket_seconds` is 60 for one day and widens up to 3600 for 30 days, so a longer window holds at most 720 points. `events` holds the newest 500 events, newest first: `{at, kind, http_status, collection, identifier, wait_seconds, detail}`, where `kind` is `rate_limited`, `server_error`, `timeout`, `connection_error`, `refused`, `retry`, or `backoff`, and an absent value is `null`. A `rate_limited` event's `wait_seconds` is the `Retry-After` the source asked for; a `backoff` event's is the wait taken. `events_total` counts every event in the window.
- `GET /api/runs?days=N` answers `{generated_at, runs: [...]}`: every run started in the last `N` days (1–30, default 30; anything else is `422`), newest first, refreshed about every 30 s. Each run: `run_id`, `activity_id`, `kind` (as in `/api/activity`), `source`, `collection`, `started_at`, `finished_at`, `duration_seconds`, `result`, `end_reason`, `items_discovered`, `items_new`, `items_updated`, `items_failed`, `bytes_downloaded`, `errors`, `failure_notice_error`, `detail_trimmed` (true once the run's per-show error lines were dropped; `status --json` carries it on `last_run` too), `runs_trimmed`, and `bytes_freed` (as in `/api/activity`). Syncs, catch-ups, refetches, and reconforms take every figure from their run record, so they match `status`; a run from before this release has `activity_id: null`. Restages, verifies, and reconciles have no run record: `run_id`, `items_new`, `items_updated`, and `failure_notice_error` are `null`, `items_discovered` is the shows a reconcile listed, and `errors` are the errors recorded in the restage's or reconcile's outcome (`null` for a verify). `finished_at`, `duration_seconds`, and `result` are `null` while a run runs; a run that stopped reporting shows `failed_before_completion` and `stopped_reporting`, ended at its last report.
- `GET /api/library` answers `{generated_at, disk: {total_bytes, free_bytes}, totals: {counts, restricted, archive_bytes, library_bytes}, collections: [{source, collection, counts, restricted, archive_bytes, library_bytes, last_successful_sync_at}], growth: [{date, source, collection, complete, archive_bytes}], recently_mirrored: [{source, collection, identifier, title, downloaded_at}]}`, refreshed every 30 s. `counts`, `restricted` (a count), and `archive_bytes` are the figures `status --json` reports as `counts`, `restricted`, and `bytes_on_disk`. `library_bytes` is the size of the files the library stages; they are hardlinks, so they use no disk of their own. `disk` is the archive's filesystem, as `df` reports it. `last_successful_sync_at` is when the collection's latest sync or catch-up that ended `completed` or `completed_with_item_issues` finished, `null` if none did. `growth` has one row per collection per day for the last 30 days, starting at the upgrade; a day with no run repeats the day before. `recently_mirrored` lists the 20 shows downloaded most recently (new or re-fetched), with the source's title or `null`.
- `GET /api/attention` answers `{generated_at, entries: [...], health: [...]}`, refreshed about every 30 s. Every figure comes from the same computation as `status --json`, so the two agree.
  - `entries` lists what needs you: one entry per collection per kind, so 300 failed shows are one entry. It is empty when nothing needs attention. Each entry is `{kind, source, collection, since, identifiers, count, detail}`. `since` is when the problem began, or `null` where nothing records it. `identifiers` names the affected shows and is empty for collection-wide kinds. `count` is how many shows the entry affects (files, for `unplaced_files` and `unidentified_files`). `detail` uses `status --json`'s field names.
  - The kinds, in this order:
    - `failed_shows`: shows in status `failed`.
    - `recovery`: `status`'s `recovery` object as `detail`; `identifiers` is its `affected_item`.
    - `ended_unexpectedly`: runs that stopped reporting in the last 30 days, as `detail.runs: [{run_id, activity_id, kind, finished_at}]`. It clears when a later run of the same kind ends `completed` or `completed_with_item_issues`; a catch-up counts as a sync.
    - `stale`.
    - `rescheduled`: `since` is the oldest skip.
    - `handoff_pending`: `detail: {shows, last_reason}`.
    - `failure_notice_undelivered`: `detail: {failure_notice_error, run_id}` from the latest run.
    - `separations`: `detail.separations: [{identifier, folder}]`.
    - `unplaced_files`: `detail.unplaced_files: [path]`.
    - `unidentified_files`: audio files whose content matches no format shakedown recognises, kept and staged under the source's name and never tagged, as `detail.unidentified_files: [{identifier, file}]`. It clears when the source replaces the file with audio shakedown recognises, or the show is pruned.
  - `health` has one row per collection: `{source, collection, restricted: [{identifier, reason}], restricted_count, removed_upstream: [{identifier, removed_on}], removed_upstream_count, drift_files, not_playable: [{identifier, reason}], not_playable_count, playable_copies: [identifier], playable_copies_count}`. `drift_files` comes from the last `verify --deep`. `playable_copies` lists complete shows kept twice: an unplayable original (SHN or an unrecognised format), kept in the archive only, plus the playable copy the library stages.
  - Each list (`identifiers`, the lists in `detail`, and the health lists) carries its first 10 shows or files; `count` and each health list's `*_count` are complete. `status --json` keeps its lists complete.

### Library handoff (optional)

A collection's `on_complete` can call a webhook or run a command after a sync that stages new shows, for a tool of your own that should hear about them. Listening needs none: Navidrome's folder watcher finds new shows by itself. A handoff that is not delivered never fails the sync; its shows show in `status` and go out again with the next sync. See [docs/handoff.md](docs/handoff.md) for the delivery rules and the payload.

### Upgrading

- **Moving an existing install over (this release).** ofelia leaves the stack: the `shakedown-scheduler` container starts scheduled syncs from each collection's `schedule` in `shakedown.yaml`. Nothing downloads again, because the archive, library, and state database stay where they are.
  1. Replace the compose file with the new [`docker-compose.example.yaml`](docker-compose.example.yaml) (Container Station → edit the application): drop the `ofelia` service; add `shakedown-scheduler`, `watchtower`, and `socket-proxy` (the last two are optional); give `shakedown-serve` the single `/share/data:/data` mount and no `/config` mount. Afterwards, check that Container Station no longer lists `shakedown-ofelia`, and delete it if it does: a leftover ofelia keeps starting its old jobs. Keep your `SHAKEDOWN_IMAGE`, `SHAKEDOWN_USER`, `SHAKEDOWN_API_TOKEN`, and `IA_*` values, and set `IA_*` on the scheduler too if a collection needs them.
  2. Your `shakedown.yaml` already sits at `/share/data/shakedown-config/`, which is `/data/shakedown-config/` inside the containers, the path Shakedown now reads by default. Make sure `SHAKEDOWN_USER` owns that folder (QNAP setup, step 2): serve is the container that writes the file when settings are saved (`PUT /api/config`). Shakedown did not write this file, so those saves stay locked until the edit override is on (`POST /api/config/override`, with `SHAKEDOWN_API_TOKEN` until the dashboard's Settings screens ship); File Station edits always work.
  3. Copy each collection's schedule from the old `ofelia.job-run.<name>.schedule` label into that collection's `schedule` in `shakedown.yaml` (File Station). Drop the label's first field, which is seconds: `0 0 3 * * 0` becomes `0 3 * * 0` (Sunday 03:00). Set `timezone` in the file, or `TZ` on the containers, to the clock the labels used. Until then each collection is manual only and does not sync on its own; there is no import.
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
uv sync --python 3.15 --extra dev --extra serve
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
