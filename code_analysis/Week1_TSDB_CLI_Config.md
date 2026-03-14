---
title: "TSDB CLI and Config Analysis"
date: 2026-03-14
last_modified_at: 2026-03-14
author: Byeonggyu Park
---
# Week1. TSDB CLI and Config Analysis

## 1. TSDB CLI Flag 분석
Prometheus에서 CLI (Command Line Interface)란, 서버 시작 시에 옵션으로 설정할 수 있는 값입니다. TSDB 관련 설정 외에도 여러 값들이 있지만 TSDB 관련 값들은 `--storage.tsdb.*`으로, 아래와 같습니다.
* 코드: [`cmd/prometheus/main.go`](https://github.com/prometheus/prometheus/blob/release-3.10/cmd/prometheus/main.go)

### 1.1. Open Flags

| Flag | Description | Default |
|------|-------------|---------|
| `--storage.tsdb.path` | Base path for metrics storage. | `data/` |
| `--storage.tsdb.retention.time` | [DEPRECATED] How long to retain samples in storage. When both time and size are unset, defaults to `15d`. Use config file instead. | Unset (runtime default `15d`) |
| `--storage.tsdb.retention.size` | [DEPRECATED] Maximum number of bytes that can be stored for blocks. Use config file instead. | Unset (unlimited) |
| `--storage.tsdb.no-lockfile` | Do not create lockfile in data directory. | `false` |
| `--storage.tsdb.head-chunks-write-queue-size` | Size of the queue through which head chunks are written to disk to be m-mapped. 0 disables the queue. Experimental. | `0` |
| `--storage.tsdb.delay-compact-file.path` | Path to a JSON file tracking uploaded TSDB blocks (e.g. Thanos shipper meta). Delays compaction for blocks not yet uploaded. | `""` |

### 1.2. Hidden Flags

| Flag | Description | Default |
|------|-------------|---------|
| `--storage.tsdb.wal-segment-size` | Size at which to split the tsdb WAL segment files. (10MB~256MB) | Unset (TSDB internal default) |
| `--storage.tsdb.wal-compression` | Compress the tsdb WAL. If false, compression-type is ignored. | `true` |
| `--storage.tsdb.wal-compression-type` | Compression algorithm for the tsdb WAL. (`snappy`, `zstd`) | `snappy` |
| `--storage.tsdb.min-block-duration` | Minimum duration of a data block before being persisted. For use in testing. | `2h` |
| `--storage.tsdb.max-block-duration` | Maximum duration compacted blocks may span. For use in testing. | Unset (runtime: `min(retention/10, 31d)`) |
| `--storage.tsdb.max-block-chunk-segment-size` | The maximum size for a single chunk segment in a block. | Unset (TSDB internal default) |
| `--storage.tsdb.samples-per-chunk` | Target number of samples per chunk. | `120` |
| `--storage.tsdb.allow-overlapping-compaction` | Allow compaction of overlapping blocks. If false, vertical compaction is disabled. | `true` |
| `--storage.tsdb.delayed-compaction.max-percent` | Upper limit for random compaction delay as % of head chunk range. Requires delayed-compaction feature flag. | `10` |
| `--storage.tsdb.block-reload-interval` | Interval to check for new or removed blocks. Minimum 1s. | `1m` |

* 참고: [Official Docs](https://prometheus.io/docs/prometheus/latest/command-line/prometheus/#flags)에는 Hidden이 아닌 CLI들만 정리되어 있습니다.

### 1.3. Feature Flags (`--enable-feature`)

| Feature Flag | Description |
|-------------|-------------|
| `exemplar-storage` | Enable experimental in-memory exemplar storage. |
| `memory-snapshot-on-shutdown` | Save memory snapshot on shutdown for faster restarts. |
| `metadata-wal-records` | Write metadata records to WAL. |
| `created-timestamp-zero-ingestion` | Enable start timestamp zero ingestion. Changes default scrape_protocols to ProtoFirst. |
| `st-storage` | Enable start timestamp storage. Changes default scrape_protocols to ProtoFirst. |
| `delayed-compaction` | Delay compaction (used with delayed-compaction.max-percent). |
| `use-uncached-io` | Enable experimental uncached IO. |

* 참고: [Official Docs](https://prometheus.io/docs/prometheus/latest/feature_flags/)

## 2. TSDB Config 분석 (prometheus.yml)

CLI Flag는 서버 시작 시 고정되지만, Config는 `prometheus.yml` 파일에서 설정하며 **런타임에 reload 가능**합니다. TSDB 관련 설정은 `storage` 블록 아래에 위치합니다.
* 코드: [`config/config.go`](https://github.com/prometheus/prometheus/blob/release-3.10/config/config.go)

```yaml
storage:
  tsdb:
    out_of_order_time_window: 5m
    stale_series_compaction_threshold: 0.3
    retention:
      time: 30d
      size: 10GB
  exemplars:
    max_exemplars: 100000
```

### 2.1. `storage.tsdb` (`TSDBConfig`)

| YAML Key | Type | Description | Default |
|----------|------|-------------|---------|
| `out_of_order_time_window` | `duration` | How far back in time an out-of-order sample can be inserted. Internally converted to milliseconds `int64`. | `0` (disabled) |
| `stale_series_compaction_threshold` | `float64` | Ratio (0.0~1.0) of stale series in the Head block. If exceeded, stale series compaction runs immediately. | `0` (disabled) |

### 2.2. `storage.tsdb.retention` (`TSDBRetentionConfig`)

| YAML Key | Type | Description | Default |
|----------|------|-------------|---------|
| `time` | `duration` | Sample retention period. Config file replacement for CLI `--storage.tsdb.retention.time`. | Unset (CLI or runtime default `15d`) |
| `size` | `bytes` | Maximum storage size for blocks. Config file replacement for CLI `--storage.tsdb.retention.size`. Unit required (B, KB, MB, GB, TB). | Unset (unlimited) |

> CLI의 retention 플래그는 DEPRECATED이며, 이 config 값이 CLI보다 우선 적용됩니다. ([`cmd/prometheus/main.go:709-716`](https://github.com/prometheus/prometheus/blob/release-3.10/cmd/prometheus/main.go#L709-L716))

### 2.3. `storage.exemplars` (`ExemplarsConfig`)

| YAML Key | Type | Description | Default |
|----------|------|-------------|---------|
| `max_exemplars` | `int64` | Maximum number of exemplars stored in memory (circular buffer). 0 or less disables storage. Requires `--enable-feature=exemplar-storage`. | `100000` |

* 참고: [Official Docs](https://prometheus.io/docs/prometheus/latest/configuration/configuration/#tsdb)

---
author: [Byeonggyu Park](https://github.com/ggyuchive)
