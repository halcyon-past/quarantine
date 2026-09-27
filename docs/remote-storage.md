# Remote storage

[← docs index](index.md)

`.quarantine/` on a local disk is exactly right for a script on your laptop —
and exactly wrong for Kubernetes, AWS Lambda, Docker, or any fleet of workers
whose disks evaporate with the container. Remote backends give all of them
**one shared quarantine**: workers set failures aside in a bucket, and you
inspect and replay them from your own machine.

## Using the S3 backend

Install the extra (the core package stays zero-dependency):

```bash
pip install "quarantine-py[s3]"
```

Then a URL goes anywhere a folder went — the same `dir=`, the same `--dir`,
the same `$QUARANTINE_DIR`:

```python
@quarantine(dir="s3://my-bucket/etl/quarantine")
def process(item): ...
```

```bash
quarantine list  --dir s3://my-bucket/etl/quarantine
quarantine show 2 --dir s3://my-bucket/etl/quarantine
quarantine retry --dir s3://my-bucket/etl/quarantine --import job.py
quarantine ui    --dir s3://my-bucket/etl/quarantine
export QUARANTINE_DIR=s3://my-bucket/etl/quarantine   # or set it once
```

Every command behaves as it does locally, because records *are* what they are
locally: per-record objects (`0001/meta.json`, `input.pkl`, `traceback.txt`,
…) under the prefix, byte-for-byte the same files. Reads are cached in a
per-URL directory under the system temp folder so `show`, `debug`, `retry`
and the dashboard work unchanged.

## Credentials

The backend uses boto3's standard credential chain — nothing
quarantine-specific to configure. In order, boto3 looks at:

1. `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` (and `AWS_SESSION_TOKEN`)
   environment variables,
2. the shared config/credentials files (`~/.aws/credentials`, with
   `AWS_PROFILE` selecting a profile), including SSO sessions,
3. the compute role: ECS task role, EKS IRSA / Pod Identity, Lambda execution
   role, or the EC2 instance profile.

In a fleet, that usually means: **workers use their task/pod role, your
laptop uses your SSO profile** — same URL, different credentials, no keys in
code. `AWS_ENDPOINT_URL` is honoured too, so S3-compatible stores (MinIO,
LocalStack) work.

## IAM permissions

The backend needs four S3 actions, and they can be scoped to the exact
bucket and prefix. A minimal policy for a **worker or operator** (writes
records, retries, clears):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "QuarantineList",
      "Effect": "Allow",
      "Action": "s3:ListBucket",
      "Resource": "arn:aws:s3:::my-bucket",
      "Condition": {
        "StringLike": { "s3:prefix": "etl/quarantine/*" }
      }
    },
    {
      "Sid": "QuarantineReadWrite",
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
      "Resource": "arn:aws:s3:::my-bucket/etl/quarantine/*"
    }
  ]
}
```

What each action is for:

| Action | Used by |
|---|---|
| `s3:ListBucket` (on the bucket, prefix-scoped) | every read: `list`, `stats`, deduplication, finding the next free id |
| `s3:GetObject` | `show`, `retry`, `debug`, the dashboard — downloading records |
| `s3:PutObject` | quarantining (including the conditional id claim), retry bookkeeping |
| `s3:DeleteObject` | `retry` (recovered records are deleted), `clear`, `reindex` sweeps |

Two useful reductions:

- **Read-only inspection** (a dashboard host, an on-call engineer who may
  look but not touch): drop `s3:PutObject` and `s3:DeleteObject`. `list`,
  `show`, `stats` and the dashboard's viewing work; quarantining, `retry`
  and `clear` will fail with an access error, as intended.
- **Write-only workers** are *not* possible: writers also need `ListBucket`
  and `GetObject`, because allocating an id requires seeing which ids exist
  and deduplication reads existing records.

No bucket-management permissions are needed — the backend never creates
buckets, never touches bucket policy, and works fine on a prefix of a shared
bucket. If the bucket uses SSE-KMS encryption, add `kms:Decrypt` and
`kms:GenerateDataKey` on the bucket's key. And since records contain your
real inputs, treat the prefix like the data it holds: block public access,
encrypt at rest, and remember `redact=` runs **before** upload, so secrets
you name never reach the bucket at all.

## The guarantees, translated to S3

The local folder's promises rest on atomic renames and atomic directory
creation — neither exists on S3, so they are replaced (the reasoning lives in
[ADR 0007](adr/0007-object-store-commit-point.md)):

- **A reader never sees a partial record.** `meta.json` is uploaded **last**
  and is the commit point: a record without it does not exist, whatever else
  has been uploaded. A crash mid-write leaves invisible debris, and
  `quarantine reindex --dir s3://…` sweeps it.
- **Two workers can never claim the same id.** An id is claimed by writing a
  zero-byte `.claim` object with `If-None-Match: *` — S3's conditional
  write, which the loser sees as `PreconditionFailed` and simply takes the
  next number.
- **Deduplication is advisory under concurrency.** Two workers quarantining
  the same input in the same instant may both store it — a duplicate record,
  never a lost one. *Never lose a failure* outranks *never store twice*.
- **There is no `index.json` object.** S3 listings are strongly consistent,
  so the listing *is* the index; nothing can go stale.

## Using the GCS backend

Install the extra (the core package stays zero-dependency):

```bash
pip install "quarantine-py[gcs]"
```

Then a URL goes anywhere a folder went — the same `dir=`, the same `--dir`,
the same `$QUARANTINE_DIR`:

```python
@quarantine(dir="gs://my-bucket/etl/quarantine")
def process(item): ...
```

```bash
quarantine list  --dir gs://my-bucket/etl/quarantine
quarantine show 2 --dir gs://my-bucket/etl/quarantine
quarantine retry --dir gs://my-bucket/etl/quarantine --import job.py
quarantine ui    --dir gs://my-bucket/etl/quarantine
export QUARANTINE_DIR=gs://my-bucket/etl/quarantine   # or set it once
```

Every command behaves as it does locally, because records *are* what they are
locally: per-record objects (`0001/meta.json`, `input.pkl`, `traceback.txt`,
…) under the prefix, byte-for-byte the same files. Reads are cached in a
per-URL directory under the system temp folder so `show`, `debug`, `retry`
and the dashboard work unchanged.

## GCS credentials

The backend uses `google-cloud-storage`'s standard Application Default
Credentials (ADC) chain — nothing quarantine-specific to configure. In order,
the client looks at:

1. the `GOOGLE_APPLICATION_CREDENTIALS` environment variable, pointing at a
   service-account key file,
2. user credentials from `gcloud auth application-default login`,
3. the attached runtime identity: a GKE Workload Identity binding, a Cloud
   Run/Cloud Functions service account, or a Compute Engine instance's
   attached service account.

In a fleet, that usually means: **workers use their attached service
account, your laptop uses `gcloud auth application-default login`** — same
URL, different credentials, no keys in code. `STORAGE_EMULATOR_HOST` is
honoured too, so a local [fake-gcs-server](https://github.com/fsouza/fake-gcs-server)
works for development and testing without touching a real bucket.

## GCS IAM permissions

The backend needs the storage object actions, scoped to the bucket. The
[`roles/storage.objectUser`](https://cloud.google.com/storage/docs/access-control/iam-roles)
predefined role covers exactly this — list, read, create and delete objects,
with no bucket-management permissions — for a **worker or operator** (writes
records, retries, clears):

```bash
gcloud storage buckets add-iam-policy-binding gs://my-bucket \
  --member="serviceAccount:worker@my-project.iam.gserviceaccount.com" \
  --role="roles/storage.objectUser"
```

If you'd rather write a custom role, the underlying permissions are
`storage.objects.list`, `storage.objects.get`, `storage.objects.create` and
`storage.objects.delete`.

What each permission is for:

| Permission | Used by |
|---|---|
| `storage.objects.list` | every read: `list`, `stats`, deduplication, finding the next free id |
| `storage.objects.get` | `show`, `retry`, `debug`, the dashboard — downloading records |
| `storage.objects.create` | quarantining (including the conditional id claim), retry bookkeeping |
| `storage.objects.delete` | `retry` (recovered records are deleted), `clear`, `reindex` sweeps |

Two useful reductions:

- **Read-only inspection** (a dashboard host, an on-call engineer who may
  look but not touch): use [`roles/storage.objectViewer`](https://cloud.google.com/storage/docs/access-control/iam-roles)
  instead. `list`, `show`, `stats` and the dashboard's viewing work;
  quarantining, `retry` and `clear` will fail with an access error, as
  intended.
- **Write-only workers** are *not* possible: writers also need `list` and
  `get`, because allocating an id requires seeing which ids exist and
  deduplication reads existing records.

No bucket-management permissions are needed — the backend never creates
buckets and works fine on a prefix of a shared bucket. If the bucket uses a
customer-managed encryption key (CMEK), add `cloudkms.cryptoKeyEncrypterDecrypter`
on that key. And since records contain your real inputs, treat the prefix
like the data it holds: keep the bucket private, encrypt at rest, and
remember `redact=` runs **before** upload, so secrets you name never reach
the bucket at all.

## The guarantees, translated to GCS

The local folder's promises rest on atomic renames and atomic directory
creation — neither exists on GCS, so they are replaced with GCS's own
primitives (the reasoning lives in
[ADR 0007](adr/0007-object-store-commit-point.md)):

- **A reader never sees a partial record.** `meta.json` is uploaded **last**
  and is the commit point: a record without it does not exist, whatever else
  has been uploaded. A crash mid-write leaves invisible debris, and
  `quarantine reindex --dir gs://…` sweeps it.
- **Two workers can never claim the same id.** An id is claimed by uploading
  a zero-byte `.claim` object with `if_generation_match=0` — GCS's
  generation-precondition analog to a conditional write, which the loser sees
  as a `PreconditionFailed` (412) and simply takes the next number.
- **Deduplication is advisory under concurrency.** Two workers quarantining
  the same input in the same instant may both store it — a duplicate record,
  never a lost one. *Never lose a failure* outranks *never store twice*.
- **There is no `index.json` object.** GCS listings are strongly consistent,
  so the listing *is* the index; nothing can go stale.

## Writing your own backend

The interface is public and small. Subclass
`quarantine.StorageBackend`, implement its methods (the docstrings state the
contract, and `Store` — the local folder — is the reference implementation),
and claim a URL scheme:

```python
from quarantine import StorageBackend, register_backend


class MyStore(StorageBackend):
    def __init__(self, url: str) -> None: ...

    # exists, ensure, records, get, count, add, update, write_traceback,
    # delete, clear, purge_temp, rebuild_index, fingerprints, disk_bytes


register_backend("mystore", MyStore)
```

From then on `dir="mystore://…"` works everywhere a path does. Whatever the
transport, keep the two promises: a reader never sees a partial record, and a
failed write never silently loses one.

Azure Blob, Redis and Databricks Unity Catalog volumes are planned as
further built-ins — see [ENHANCEMENTS.md](https://github.com/halcyon-past/quarantine/blob/main/ENHANCEMENTS.md);
the interface above is how they will be built, and how you can build one
sooner.
