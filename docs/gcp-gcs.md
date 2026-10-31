---
title: Google Cloud Storage (GCS) Backend & Workload Identity Guide · quarantine-py
description: Complete guide for Google Cloud Storage dead-letter queues. Application Default Credentials (ADC), GKE Workload Identity, IAM roles, CMEK, and fake-gcs-server.
---

# Google Cloud Storage (GCS) Remote Storage

[← remote storage overview](remote-storage.md) · [docs index](index.md)

The Google Cloud Storage (GCS) backend provides a single shared quarantine for workloads running across Google Cloud Platform (Google Kubernetes Engine, Cloud Run, Cloud Functions, Compute Engine, or Vertex AI). Ephemeral containers write execution failures into a bucket, while operators manage, inspect, and replay them from any terminal or dashboard.

---

## Installation & Quick Start

The core `quarantine-py` library has zero dependencies. To use Google Cloud Storage, install the optional `gcs` extra (which installs `google-cloud-storage`):

```bash
pip install "quarantine-py[gcs]"
```

Use a `gs://` URL anywhere a directory path is expected:

### In Python Code

```python
from quarantine import quarantine


@quarantine(
    dir="gs://my-quarantine-bucket/etl/pipeline",
    halt_after=25,
    report=True,
)
def process_event(event: dict) -> None:
    if "payload" not in event:
        raise KeyError("Invalid event structure")
    # ... process item ...
```

### In Asynchronous Code

```python
from quarantine import ashield


async def main():
    async for result in ashield(
        event_stream(),
        using=handle_event,
        dir="gs://my-quarantine-bucket/etl/pipeline",
    ):
        print("Processed event:", result)
```

### From the Command-Line Interface (CLI)

```bash
# List all quarantined records in the GCS bucket
quarantine list --dir gs://my-quarantine-bucket/etl/pipeline

# Show record #1 details and download payload
quarantine show 1 --dir gs://my-quarantine-bucket/etl/pipeline

# Inspect traceback of record #1
quarantine debug 1 --dir gs://my-quarantine-bucket/etl/pipeline

# Replay and retry failed inputs after applying a code fix
quarantine retry --dir gs://my-quarantine-bucket/etl/pipeline --import worker.py

# Launch the visual web dashboard
quarantine ui --dir gs://my-quarantine-bucket/etl/pipeline --port 8080
```

> **Tip**: Set the `QUARANTINE_DIR` environment variable to avoid passing `--dir` repeatedly:
> ```bash
> export QUARANTINE_DIR="gs://my-quarantine-bucket/etl/pipeline"
> quarantine list
> ```

---

## Authentication & Credentials

The GCS backend relies on Google's standard **Application Default Credentials (ADC)** chain. No custom SDK initialization or tokens in code are required.

### Credential Resolution Chain

1. **Service Account Key File**:
   Point to a downloaded service account JSON key:
   ```bash
   export GOOGLE_APPLICATION_CREDENTIALS="/etc/secrets/gcp-sa-key.json"
   ```
2. **Local Developer Authentication** (Recommended for Workstations):
   Authenticate via the Google Cloud CLI:
   ```bash
   gcloud auth application-default login
   ```
3. **Attached Runtime Identities** (Recommended for Production):
   When running inside Google Cloud infrastructure, ADC automatically resolves the attached identity without managing private keys:
   - **Google Kubernetes Engine (GKE)**: [Workload Identity](https://cloud.google.com/kubernetes-engine/docs/how-to/workload-identity) binds a Kubernetes ServiceAccount directly to an IAM ServiceAccount.
   - **Cloud Run & Cloud Functions**: Configured via the service's runtime service account.
   - **Compute Engine (GCE)**: Uses the VM's attached service account.

### Local Emulator Testing (fake-gcs-server)

For local development and unit/integration testing without GCP network access or cloud costs, point to a local [fake-gcs-server](https://github.com/fsouza/fake-gcs-server):

```bash
# Run local emulator via Docker
docker run -d -p 4443:4443 fsouza/fake-gcs-server -scheme http

# Direct quarantine to the emulator
export STORAGE_EMULATOR_HOST="http://localhost:4443"
quarantine list --dir gs://test-bucket/local-test
```

---

## IAM Permissions & Policy Setup

The backend needs object-level permissions within the bucket. It never attempts to create or manage buckets.

### Recommended IAM Role: `roles/storage.objectUser`

Grant the predefined [`roles/storage.objectUser`](https://cloud.google.com/storage/docs/access-control/iam-roles) role to your worker or operator service account:

```bash
gcloud storage buckets add-iam-policy-binding gs://my-quarantine-bucket \
  --member="serviceAccount:worker-sa@my-project.iam.gserviceaccount.com" \
  --role="roles/storage.objectUser"
```

### Underlying Permission Breakdown

| Permission | Required By | Reason |
|---|---|---|
| `storage.objects.list` | All commands (`list`, `show`, `retry`, `stats`, `add`) | Lists existing record prefixes to find the next free ID and check deduplication fingerprints. |
| `storage.objects.get` | `show`, `debug`, `retry`, dashboard | Reads and downloads serialized inputs, tracebacks, and metadata. |
| `storage.objects.create` | Worker quarantining (`add`), `retry` bookkeeping | Uploads serialized inputs, tracebacks, and claims candidate IDs. |
| `storage.objects.delete` | `retry`, `clear`, `reindex` | Deletes recovered items after retry and sweeps aborted write debris. |

### Read-Only Inspection Role

For monitoring instances, audit pipelines, or on-call engineers:
- Use [`roles/storage.objectViewer`](https://cloud.google.com/storage/docs/access-control/iam-roles) instead of `objectUser`.
- `quarantine list`, `show`, `debug`, and `ui` work normally. Mutative operations (`clear`, `retry`) will fail with access-denied errors.

### Customer-Managed Encryption Keys (CMEK)

If your GCS bucket is encrypted with Cloud KMS, grant the Cloud Storage service agent permission to use your KMS key:

```bash
gcloud kms keys add-iam-policy-binding my-key \
  --keyring=my-keyring \
  --location=global \
  --member="serviceAccount:service-PROJECT_NUMBER@gs-project-accounts.iam.gserviceaccount.com" \
  --role="roles/cloudkms.cryptoKeyEncrypterDecrypter"
```

---

## Architectural Guarantees & Consistency (ADR 0007)

Google Cloud Storage does not provide atomic directory renames. The GCS backend implements strong consistency guarantees using native GCS primitives:

1. **Commit Point Protocol**:
   Input files (`input.pickle`, `traceback.txt`) are uploaded first. The metadata document (`meta.json`) is uploaded **last**. Any record prefix lacking `meta.json` is treated as non-existent by all readers, preventing readers from seeing half-written records.
2. **Race-Free ID Allocation with Generation Match**:
   When multiple workers quarantine items simultaneously, they claim candidate numeric IDs by creating a zero-byte `.claim` object with precondition `if_generation_match=0`. If another worker created the claim first, GCS returns HTTP 412 `PreconditionFailed`, causing the runner to immediately increment and claim the next candidate ID.
3. **Strongly Consistent Listings**:
   GCS provides strong global read-after-write consistency for bucket object listings. There is no `index.json` file that can get corrupted or drift out of sync.
4. **Secure Local Read Caching**:
   Records downloaded for inspection (`show`, `debug`, `retry`) are cached in `$TMPDIR/quarantine-gcs-<hash>`. Symlink traversal checks and safe file writing (`O_NOFOLLOW` and `dir_fd`) prevent local filesystem hijacking.

---

## Frequently Asked Questions (FAQ)

### How do multiple workers in GKE avoid overwriting each other's failures?
GCS uses `if_generation_match=0` on the `.claim` file upload. If two pods attempt to claim ID `0001` at the exact same instant, GCS accepts one and rejects the second with HTTP 412. The second pod catches the rejection and claims `0002`.

### What happens if a Cloud Run container terminates before completing an upload?
Because `meta.json` is uploaded last as the commit point, incomplete uploads remain completely invisible to readers. To clean up uncommitted upload debris, run:
```bash
quarantine reindex --dir gs://my-quarantine-bucket/etl/pipeline
```

### Can I run quarantine across multiple environments in the same bucket?
Yes. Quarantine treats GCS prefixes as independent namespaces:
- `gs://company-dlq/prod/order-processing`
- `gs://company-dlq/staging/order-processing`
- `gs://company-dlq/prod/user-sync`

Each prefix maintains its own isolated counter and deduplication cache.

### How do I configure Workload Identity on GKE?
1. Annotate your Kubernetes ServiceAccount with the GCP service account:
   ```bash
   kubectl annotate serviceaccount k8s-worker-sa \
     iam.gke.io/gcp-service-account=worker-sa@my-project.iam.gserviceaccount.com
   ```
2. Allow the Kubernetes ServiceAccount to impersonate the GCP service account:
   ```bash
   gcloud iam service-accounts add-iam-policy-binding \
     worker-sa@my-project.iam.gserviceaccount.com \
     --role roles/iam.workloadIdentityUser \
     --member "serviceAccount:my-project.svc.id.goog[default/k8s-worker-sa]"
   ```
No keys or credentials need to be mounted into your pods.
