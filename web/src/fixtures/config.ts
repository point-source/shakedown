// GET /api/config, captured from shakedown.dashboard_config.config_answer on the
// example config (2026-11-24) and given the fixture paths, timezone, and clock,
// less the login fields §spec:dashboard-credentials withdraws and plus
// `reads_links` (§spec:dashboard-settings, round S3).
import type { ConfigAnswer } from "@/api/types"

export const config: ConfigAnswer = {
  "generated_at": "2026-11-12T21:40:00+00:00",
  "state": "valid",
  "path": "/data/config/shakedown.yaml",
  "revision": "sha256:071544bb675d235b097c51184303677947cf92b272dcca747328c9516895bea3",
  "edit": "editable",
  "document": {
    "archive_root": "/data/archive",
    "library_root": "/data/library",
    "state_db": "/data/archive/.shakedown/state.db",
    "max_concurrent_downloads": 4,
    "max_concurrent_collections": 2,
    "keep_run_errors_days": 180,
    "sources": [
      {
        "name": "internetarchive",
        "type": "ia",
        "collections": [
          {
            "name": "grateful-dead",
            "query": "collection:GratefulDead",
            "library_layout": "{year}/{date} - {venue}/{identifier}",
            "prune_disappeared": false,
            "incremental_discovery": false,
            "preserve_source_metadata": false,
            "fill_tags": true,
            "on_complete": {
              "webhook": "http://beets:8337/import?collection={collection}"
            },
            "schedule": "0 3 * * 0"
          },
          {
            "name": "phish",
            "query": "collection:Phish",
            "library_layout": "passthrough",
            "prune_disappeared": false,
            "incremental_discovery": false,
            "preserve_source_metadata": false,
            "fill_tags": false,
            "on_complete": null,
            "schedule": "0 4 * * 0"
          }
        ],
        "max_concurrent_requests": 4
      },
      {
        "name": "etree-lma",
        "type": "etree",
        "collections": [
          {
            "name": "etree-lma",
            "query": "collection:etree AND NOT collection:GratefulDead AND NOT collection:Phish",
            "library_layout": "{year}/{date} - {venue}/{identifier}",
            "prune_disappeared": false,
            "incremental_discovery": false,
            "preserve_source_metadata": false,
            "fill_tags": false,
            "on_complete": null,
            "schedule": "0 5 * * 0"
          }
        ],
        "max_concurrent_requests": null
      }
    ],
    "notifications": {
      "on_failure": {
        "webhook": "https://ntfy.example.net/shakedown-alerts"
      }
    },
    "timezone": "Europe/London"
  },
  "errors": [],
  "next_syncs": [
    {
      "source": "internetarchive",
      "collection": "grateful-dead",
      "next_at": "2026-11-15T03:00:00+00:00"
    },
    {
      "source": "internetarchive",
      "collection": "phish",
      "next_at": "2026-11-15T04:00:00+00:00"
    },
    {
      "source": "etree-lma",
      "collection": "etree-lma",
      "next_at": "2026-11-15T05:00:00+00:00"
    }
  ],
  "source_types": [
    {
      "type": "etree",
      "layout_fields": [
        "identifier",
        "title",
        "creator",
        "date",
        "year",
        "venue",
        "coverage"
      ],
      "layout_filters": [
        "lower",
        "sanitize",
        "short",
        "upper"
      ],
      "reads_links": true
    },
    {
      "type": "ia",
      "layout_fields": [
        "identifier",
        "title",
        "creator",
        "date",
        "year",
        "venue",
        "coverage"
      ],
      "layout_filters": [
        "lower",
        "sanitize",
        "short",
        "upper"
      ],
      "reads_links": true
    }
  ],
  // Removed from the file with its files kept (§spec:collection-removal).
  "unconfigured": [
    { "source": "internetarchive", "collection": "dmb", "shows": 312, "archive_bytes": 912000000000, "library_bytes": 912000000000 }
  ]
}
