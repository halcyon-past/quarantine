---
title: AWS S3 Storage Backend Setup, IAM & IRSA Guide · quarantine-py
description: Complete guide for AWS S3 dead-letter queue storage. Setup boto3 credentials, IRSA, ECS task roles, least-privilege IAM policies, SSE-KMS, and LocalStack.
---

# AWS S3 Remote Storage

[← remote storage overview](remote-storage.md) · [docs index](index.md)

The AWS S3 backend gives a fleet of ephemeral workers (running in ECS, EKS, AWS Lambda, or EC2) **one shared dead-letter queue**. Workers isolate poisoned items and write failure records to an S3 bucket; operators inspect, diagnose, and replay them from their own laptops or a central dashboard.

---

## Installation & Quick Start

The core `quarantine-py` library maintains zero runtime dependencies. To enable AWS S3 support, install the optional `s3` extra (which pulls in `boto3`):

```bash
pip install "quarantine-py[s3]"
```

Once installed, use an `s3://` URL anywhere a local folder path is accepted:

### In Python Code

```python
from quarantine import quarantine


@quarantine(
    dir="s3://my-quarantine-bucket/etl/jobs",
    halt_after=50,
    report=True,
)
def process_record(record: dict) -> None:
    if not record.get("id"):
        raise ValueError("Missing record ID")
    # ... process item ...
```

### In Asynchronous Code

```python
from quarantine import ashield


async def main():
    async for item in ashield(
        fetch_queue(),
        using=handle_message,
        dir="s3://my-quarantine-bucket/etl/queue",
    ):
        print("Processed successfully:", item)
```

### From the Command-Line Interface (CLI)

```bash
# List all quarantined records in the bucket
quarantine list --dir s3://my-quarantine-bucket/etl/jobs

# Show details of record #1 (downloads to local cache)
quarantine show 1 --dir s3://my-quarantine-bucket/etl/jobs

# Inspect Python traceback
quarantine debug 1 --dir s3://my-quarantine-bucket/etl/jobs

# Replay and retry failed inputs after deploying a fix
quarantine retry --dir s3://my-quarantine-bucket/etl/jobs --import worker.py

# Launch the visual web dashboard
quarantine ui --dir s3://my-quarantine-bucket/etl/jobs --port 8080
```

> **Tip**: Export the `QUARANTINE_DIR` environment variable to avoid passing `--dir` on every CLI command:
> ```bash
> export QUARANTINE_DIR="s3://my-quarantine-bucket/etl/jobs"
> quarantine list
> ```

---

## Authentication & Credentials

The S3 backend utilizes `boto3`'s standard credential resolution chain. There are no quarantine-specific credentials to configure.

### Credential Resolution Priority

1. **Environment Variables**:
   ```bash
   export AWS_ACCESS_KEY_ID="AKIA..."
   export AWS_SECRET_ACCESS_KEY="..."
   export AWS_SESSION_TOKEN="..."  # Optional: for temporary STS credentials
   ```
2. **Shared Config & Credentials Files**:
   Configured via `aws configure` in `~/.aws/credentials` and `~/.aws/config`. Select named profiles with:
   ```bash
   export AWS_PROFILE="production-operator"
   ```
   Supports AWS IAM Identity Center (SSO) profiles seamlessly.
3. **IAM Compute Roles** (Recommended for Production):
   When running inside AWS infrastructure, credentials are automatically retrieved without any credentials in code or environment variables:
   - **Amazon EKS**: IAM Roles for Service Accounts (IRSA) or EKS Pod Identity.
   - **Amazon ECS**: ECS Task IAM Role.
   - **AWS Lambda**: Lambda Execution Role.
   - **Amazon EC2**: Attached EC2 Instance Profile.

### S3-Compatible Storage (MinIO, LocalStack)

Custom S3-compatible endpoints are supported by setting the standard AWS endpoint URL environment variable:

```bash
export AWS_ENDPOINT_URL="http://127.0.0.1:9000"       # Generic AWS endpoint
# or specifically for S3:
export AWS_ENDPOINT_URL_S3="http://127.0.0.1:9000"
```

---

## IAM Permissions & Policy Setup

The backend requires four basic S3 object actions and does **not** require any bucket-management or administrative permissions. It is completely safe to run on a subfolder of an existing, shared bucket.

### Recommended Least-Privilege IAM Policy

Attach this policy to worker compute roles (ECS tasks, EKS service accounts, Lambda functions) or developer operator roles:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "QuarantineListBucket",
      "Effect": "Allow",
      "Action": "s3:ListBucket",
      "Resource": "arn:aws:s3:::my-quarantine-bucket",
      "Condition": {
        "StringLike": {
          "s3:prefix": "etl/jobs/*"
        }
      }
    },
    {
      "Sid": "QuarantineObjectOperations",
      "Effect": "Allow",
      "Action": [
        "s3:GetObject",
        "s3:PutObject",
        "s3:DeleteObject"
      ],
      "Resource": "arn:aws:s3:::my-quarantine-bucket/etl/jobs/*"
    }
  ]
}
```

### Granular Permission Breakdown

| Action | Required By | Reason |
|---|---|---|
| `s3:ListBucket` | All commands (`list`, `show`, `retry`, `stats`, `add`) | Scanned to find the next available ID, check deduplication fingerprints, and list existing records. |
| `s3:GetObject` | `show`, `debug`, `retry`, dashboard | Downloads record payload files (`input.pickle`, `meta.json`, `traceback.txt`) into local cache. |
| `s3:PutObject` | Worker quarantining (`add`), `retry` bookkeeping | Uploads serialized inputs, tracebacks, and claims candidate IDs. |
| `s3:DeleteObject` | `retry`, `clear`, `reindex` | Deletes recovered items after a successful retry or cleans up crashed write debris. |

### Read-Only Inspection Roles

For dedicated dashboard hosts, monitoring agents, or on-call engineers who should inspect failures without mutating state:
- Omit `s3:PutObject` and `s3:DeleteObject`.
- Grant only `s3:ListBucket` and `s3:GetObject`.
- `quarantine list`, `show`, `debug`, and `ui` work seamlessly; mutative commands (`clear`, `retry`) will fail with access-denied errors.

### Server-Side Encryption (KMS)

If the S3 bucket uses AWS Key Management Service (SSE-KMS) with a Customer Managed Key (CMK), grant the worker role permissions to use that key:

```json
{
  "Effect": "Allow",
  "Action": [
    "kms:Decrypt",
    "kms:GenerateDataKey"
  ],
  "Resource": "arn:aws:kms:us-east-1:123456789012:key/your-key-uuid"
}
```

---

## Architectural Guarantees & Consistency (ADR 0007)

Remote object stores do not provide POSIX directory renames. The S3 backend implements atomic consistency using native S3 guarantees:

1. **Commit Point Guarantee**:
   Record assets (`input.pickle`, `traceback.txt`) are uploaded first. The metadata document (`meta.json`) is uploaded **strictly last**. A reader ignores any prefix that lacks `meta.json`. A worker crashing mid-write leaves invisible debris that never corrupts reader state.
2. **Atomic ID Allocation**:
   Workers claim numeric record IDs by writing a zero-byte `.claim` object using `If-None-Match: *`. S3 evaluates this conditionally: if another worker claimed the ID simultaneously, S3 returns HTTP 412 `PreconditionFailed`, causing the runner to increment and claim the next available ID.
3. **Zero Stale Listing Index**:
   AWS S3 offers strong read-after-write consistency for object listings. Quarantine relies directly on live bucket listings—there is no `index.json` file that can drift out of sync.
4. **Local Read Caching**:
   Records downloaded for inspection (`show`, `debug`, `retry`) are securely cached in a machine-local directory under `$TMPDIR/quarantine-s3-<hash>`. Path traversal prevention and symlink rejection protect the host environment.

---

## Frequently Asked Questions (FAQ)

### Why do workers require `s3:ListBucket` if they only write failures?
Workers must list existing record prefixes to find the next unused candidate ID and check deduplication fingerprints before writing. Without list permissions, ID allocation cannot determine the starting counter.

### What happens if a worker terminates abruptly while uploading a record?
Because `meta.json` is uploaded last, an incomplete record is never visible to `quarantine list` or readers. You can sweep orphaned debris at any time using:
```bash
quarantine reindex --dir s3://my-quarantine-bucket/etl/jobs
```

### Does quarantine store sensitive data in plaintext in S3?
Inputs are pickled and serialized as binary payloads. To ensure sensitive fields (tokens, passwords, credit card numbers) are never written to S3, use the `redact=` parameter on the decorator:
```python
@quarantine(
    dir="s3://my-quarantine-bucket/etl/jobs",
    redact=["password", "token", "ssn"],
)
def handle_user(user_data): ...
```
Redaction runs **in-memory before upload**, ensuring secrets never reach the network or S3.

### Can I share one S3 bucket across multiple microservices?
Yes. Quarantine treats prefixes as isolated namespaces. For example:
- `s3://company-dlq/services/billing-service`
- `s3://company-dlq/services/auth-service`
- `s3://company-dlq/services/notification-service`

Each service operates independently with separate ID sequences and deduplication scopes.
