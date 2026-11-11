# Library handoff

You do not need a handoff to listen. The example compose stack runs Navidrome
on Shakedown's library tree, and Navidrome's folder watcher picks up each new
show by itself.

A handoff is for a tool of your own that should hear about new shows: a script,
an importer, or a chat notifier. Each collection can have one `on_complete`
handoff, either a webhook or a command. All targets below are placeholders: point
them at a receiver you run.

```yaml
collections:
  - name: grateful-dead
    # ...
    on_complete:
      webhook: http://your-receiver.example/shakedown?collection={collection}
      # or, instead of webhook:
      # exec: /path/to/your-command {staging_root}
```

`{source}`, `{collection}` and `{staging_root}` are filled in from the payload.
`staging_root` is the collection's folder in the library tree.

## When it fires

After each sync of a collection that has shows to hand off: shows staged by this
run, plus any shows from earlier runs whose notice was not delivered. A sync
with nothing new and nothing pending sends nothing. A `--dry-run` sends nothing.

## Delivered or not

- **Webhook:** an HTTP `POST` of the JSON payload. A `2xx` answer delivers it.
  Any other answer, including a redirect, does not. Neither does a failed
  connection.
- **Exec:** the command runs inside the Shakedown container with the JSON payload
  on stdin. Exit status `0` delivers it. A non-zero exit, or a command that
  cannot start, does not. The command must exist in the Shakedown image or be
  reachable from it.

Shakedown waits up to five minutes for the answer or the exit. Past that the
notice is undelivered and the command is stopped. A handoff should start the
work and return. It should not do a whole import before it answers.

A notice that is not delivered never fails the sync. Its shows stay pending:
`shakedown status` shows `handoff not delivered: N shows since <time>` with the
last reason. They go out again with the next sync of that collection, merged
into that run's single notice, until one is delivered. Receivers must accept a
show they have already seen. Pending shows that are pruned meanwhile leave the
list. Removing `on_complete` drops the list. Changing the target sends the
pending shows to the new one.

`shakedown validate --live-handoff` sends a marked test payload
(`"event": "validate.handoff_test"`, `"test": true`) so you can check a receiver
before the first sync.

## Payload (`payload_version: 2`)

```json
{
  "payload_version": 2,
  "event": "sync.complete",
  "source": "internetarchive",
  "collection": "grateful-dead",
  "staging_root": "/data/library/internetarchive/grateful-dead",
  "run": {
    "started_at": "2026-11-10T03:00:00",
    "finished_at": "2026-11-10T03:12:41",
    "items_new": 1,
    "items_updated": 0,
    "items_failed": 0,
    "bytes_downloaded": 412311552
  },
  "staged": [
    {
      "identifier": "gd1977-05-08.sbd.example",
      "archive_path": "/data/archive/internetarchive/grateful-dead/gd1977-05-08.sbd.example",
      "staging_path": "/data/library/internetarchive/grateful-dead/1977/1977-05-08 - Barton Hall/gd1977-05-08.sbd.example"
    }
  ]
}
```

`staged` lists the shows staged and not yet delivered, so it can hold shows from
earlier runs. `run` describes the run that sent the notice. Each entry's
`staging_path` is the show's current folder in the library tree. Paths are as
seen inside the Shakedown container. The payload carries paths and counts,
never credentials. `payload_version` changes only when the shape breaks.

## Failure alert

A global webhook can fire when a run fails (a source that could not be listed,
or shows that failed):

```yaml
notifications:
  on_failure:
    webhook: http://your-ntfy.example/shakedown
```

It receives the same envelope with `"event": "sync.failed"` and an `errors`
array instead of `staged`. A failure alert that is not delivered shows in
`status` under that run as `failure notice not delivered: <reason>`. It is not
sent again: it describes that run, and `status` already shows that run's state.
