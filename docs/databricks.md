---
title: Databricks Unity Catalog Volumes Backend & Governance Guide · quarantine-py
description: Complete guide for Databricks Unity Catalog Volumes dead-letter queues. /Volumes/ paths, databricks-sdk authentication, Unity Catalog privileges, and atomic commit points.
---

# Databricks Unity Catalog Volumes Remote Storage

[← remote storage overview](remote-storage.md) · [docs index](index.md)

The Databricks Unity Catalog Volumes backend gives Databricks jobs, notebooks, Delta Live Tables pipelines, and external workers **one governed, shared dead-letter queue** stored inside a Unity Catalog Volume. Records inherit Unity Catalog access controls, audit logging, and lineage without requiring separate cloud bucket credentials.

---

## Installation & Quick Start

The core `quarantine-py` library has zero runtime dependencies. To use Unity Catalog Volumes, install the optional `databricks` extra (which installs `databricks-sdk`):

```bash
pip install "quarantine-py[databricks]"
```

Pass a `/Volumes/<catalog>/<schema>/<volume>[/<prefix>]` path anywhere a local folder path is accepted:

### In Python Code

```python
from quarantine import quarantine


@quarantine(
    dir="/Volumes/main/analytics/quarantine/etl",
    halt_after=30,
    report=True,
)
def process_row(row: dict) -> None:
    if not row.get("customer_id"):
        raise ValueError("Missing customer_id")
    # ... process row ...
```

### In Asynchronous Code

```python
from quarantine import ashield


async def main():
    async for item in ashield(
        fetch_rows(),
        using=handle_row,
        dir="/Volumes/main/analytics/quarantine/stream",
    ):
        print("Processed successfully:", item)
```

### From the Command-Line Interface (CLI)

```bash
# List all quarantined records in the Volume
quarantine list --dir /Volumes/main/analytics/quarantine/etl

# Show details of record #1 (downloads to local cache)
quarantine show 1 --dir /Volumes/main/analytics/quarantine/etl

# Inspect Python traceback
quarantine debug 1 --dir /Volumes/main/analytics/quarantine/etl

# Replay and retry failed inputs after fixing your code
quarantine retry --dir /Volumes/main/analytics/quarantine/etl --import job.py

# Launch the visual web dashboard
quarantine ui --dir /Volumes/main/analytics/quarantine/etl --port 8080
```

> **Tip**: Set `QUARANTINE_DIR` to avoid passing `--dir` on every CLI invocation:
> ```bash
> export QUARANTINE_DIR="/Volumes/main/analytics/quarantine/etl"
> quarantine list
> ```

---

## Authentication

`DatabricksVolumeStore` uses `databricks-sdk`'s standard `WorkspaceClient()` credential chain automatically:

1. **Inside Databricks Runtime / Jobs / Notebooks:** Uses the cluster or serverless runtime credentials automatically with no extra configuration.
2. **Environment variables:** `DATABRICKS_HOST` + `DATABRICKS_TOKEN` (PAT or OAuth M2M `DATABRICKS_CLIENT_ID` / `DATABRICKS_CLIENT_SECRET`).
3. **Config profile:** `~/.databrickscfg` (e.g., via `databricks auth login`).
4. **Cloud instance identity / OIDC:** AWS IAM, Azure Managed Identity, or GCP service account federation configured on your Databricks workspace.

---

## Required Unity Catalog Privileges

| Role | Required Privileges |
| :--- | :--- |
| **Pipeline Workers (Read + Write)** | `USE CATALOG` on `<catalog>`, `USE SCHEMA` on `<schema>`, `READ VOLUME` and `WRITE VOLUME` on `<volume>` |
| **Operators (Read-Only Inspection)** | `USE CATALOG` on `<catalog>`, `USE SCHEMA` on `<schema>`, `READ VOLUME` on `<volume>` |

Example SQL grant for a pipeline service principal:

```sql
GRANT USE CATALOG ON CATALOG main TO `etl-workers`;
GRANT USE SCHEMA ON SCHEMA main.analytics TO `etl-workers`;
GRANT READ VOLUME, WRITE VOLUME ON VOLUME main.analytics.quarantine TO `etl-workers`;
```

---

## Volume Layout & Atomicity

Records follow the same per-record directory layout and commit-point guarantees as S3, GCS, and Azure ([ADR 0007](adr/0007-object-store-commit-point.md)):

1. **ID claim:** Workers allocate the next record ID by uploading `.claim` with `overwrite=False`.
2. **Payload upload:** `traceback.txt`, `input.txt`, and `input.pkl`/`input.json` are uploaded into `/Volumes/<catalog>/<schema>/<volume>/<prefix>/<id>/`.
3. **Commit point:** `meta.json` is uploaded last. Readers ignore any directory lacking `meta.json`, so half-written records are never visible.

---

## macOS Note

Any path starting with `/Volumes/` is routed to `DatabricksVolumeStore` by `open_store()`. If you want to store a **local** quarantine on a macOS external drive mounted at `/Volumes/<DriveName>`, instantiate `quarantine.Store("/Volumes/...")` directly instead of passing it to `@quarantine(dir=...)`.
