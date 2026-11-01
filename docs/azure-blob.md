---
title: Azure Blob Storage Backend, Managed Identity & RBAC Guide · quarantine-py
description: Complete guide for Azure Blob Storage dead-letter queues. Entra ID Managed Identity, AKS Workload Identity, connection strings, RBAC roles, and Azurite emulator.
---

# Azure Blob Storage Remote Storage

[← remote storage overview](remote-storage.md) · [docs index](index.md)

The Azure Blob Storage backend gives workloads running across Microsoft Azure (Azure Kubernetes Service, Azure Container Apps, Azure Functions, Azure App Service, or Virtual Machines) **one shared dead-letter queue**. Containers isolate bad inputs and write failure records into an Azure Blob Storage container; operators inspect, diagnose, and replay them from any terminal or visual dashboard.

---

## Installation & Quick Start

The core `quarantine-py` library has zero dependencies. To use Azure Blob Storage, install the optional `azure` extra (which installs `azure-storage-blob`):

```bash
pip install "quarantine-py[azure]"
```

Use an `azure://` (or `az://`) URL anywhere a local folder path is accepted:

### In Python Code

```python
from quarantine import quarantine


@quarantine(
    dir="azure://my-container/etl/jobs",
    halt_after=30,
    report=True,
)
def process_message(message: dict) -> None:
    if not message.get("user_id"):
        raise ValueError("Missing user_id")
    # ... process item ...
```

### In Asynchronous Code

```python
from quarantine import ashield


async def main():
    async for item in ashield(
        fetch_stream(),
        using=handle_stream_item,
        dir="azure://my-container/etl/stream",
    ):
        print("Processed successfully:", item)
```

### From the Command-Line Interface (CLI)

```bash
# List all quarantined records in the container
quarantine list --dir azure://my-container/etl/jobs

# Show details of record #1 (downloads to local cache)
quarantine show 1 --dir azure://my-container/etl/jobs

# Inspect Python traceback
quarantine debug 1 --dir azure://my-container/etl/jobs

# Replay and retry failed inputs after deploying a fix
quarantine retry --dir azure://my-container/etl/jobs --import worker.py

# Launch the visual web dashboard
quarantine ui --dir azure://my-container/etl/jobs --port 8080
```

> **Tip**: Set the `QUARANTINE_DIR` environment variable to avoid passing `--dir` on every CLI invocation:
> ```bash
> export QUARANTINE_DIR="azure://my-container/etl/jobs"
> quarantine list
> ```

---

## URL Syntax & Supported Schemes

The Azure backend supports flexible URL formats to accommodate various environments:

| URL Syntax | Description | Example |
|---|---|---|
| `azure://container/prefix` | Standard format using environment-configured storage account. | `azure://dlq-container/orders` |
| `az://container/prefix` | Short alias for `azure://`. | `az://dlq-container/orders` |
| `azure://account@container/prefix` | Explicit inline storage account name. | `azure://myaccount@dlq-container/orders` |
| `azure://account.blob.core.windows.net/container/prefix` | Fully qualified commercial domain. | `azure://myacc.blob.core.windows.net/dlq/orders` |
| `azure://account.blob.core.usgovcloudapi.net/container/prefix` | Azure US Government sovereign cloud. | `azure://govacc.blob.core.usgovcloudapi.net/dlq` |

---

## Authentication & Credentials

Azure requires knowing which storage account owns the container. Quarantine supports four authentication mechanisms:

### 1. Connection String (`AZURE_STORAGE_CONNECTION_STRING`)

The most common approach for rapid setup, application settings, and local Azurite emulation:

```bash
export AZURE_STORAGE_CONNECTION_STRING="DefaultEndpointsProtocol=https;AccountName=myaccount;AccountKey=...;EndpointSuffix=core.windows.net"
```

### 2. Account Name + Account Key

Direct storage account key access:

```bash
export AZURE_STORAGE_ACCOUNT_NAME="myaccount"
export AZURE_STORAGE_KEY="secret-access-key=="
```

*(Note: `azure://myaccount@container/prefix` can also provide the account name directly).*

### 3. Account Name + Shared Access Signature (SAS) Token

Scoped SAS token authentication:

```bash
export AZURE_STORAGE_ACCOUNT_NAME="myaccount"
export AZURE_STORAGE_SAS_TOKEN="?sv=2021-06-08&ss=b&srt=sco&sp=rwdlacx&se=..."
```

### 4. Microsoft Entra ID / Managed Identity (Recommended for Production)

To eliminate stored secrets, install `azure-identity` (`pip install azure-identity`). Quarantine automatically instantiates `DefaultAzureCredential()`:

- **Azure Kubernetes Service (AKS)**: [Workload Identity](https://learn.microsoft.com/en-us/azure/aks/workload-identity-overview) binds Kubernetes pods to Entra ID identities.
- **Azure Container Apps & VMs**: System-Assigned or User-Assigned Managed Identity.
- **Local Developer Workstations**: Azure CLI credentials (`az login`).

```bash
export AZURE_STORAGE_ACCOUNT_NAME="myaccount"
# No keys needed: DefaultAzureCredential automatically authenticates
```

### Local Emulator Testing (Azurite)

For development and automated CI testing without cloud accounts, quarantine seamlessly connects to Microsoft's official [Azurite](https://github.com/Azure/Azurite) emulator:

```bash
# Start Azurite emulator via Docker
docker run -d -p 10000:10000 mcr.microsoft.com/azure-storage/azurite

# Connect quarantine to the emulator
export AZURE_STORAGE_ACCOUNT_NAME="devstoreaccount1"
export AZURE_STORAGE_EMULATOR_HOST="http://127.0.0.1:10000"
export AZURE_STORAGE_KEY="Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw=="

quarantine list --dir azure://test-container/prefix
```

---

## Azure RBAC Permissions & Role Setup

Quarantine requires only blob data actions scoped to the container. It never requires storage account management or container-creation permissions.

### Recommended Role: `Storage Blob Data Contributor`

Assign the built-in [`Storage Blob Data Contributor`](https://learn.microsoft.com/en-us/azure/role-based-access-control/built-in-roles#storage-blob-data-contributor) role to your Managed Identity, Service Principal, or user account:

```bash
az role assignment create \
  --assignee "<managed-identity-principal-id>" \
  --role "Storage Blob Data Contributor" \
  --scope "/subscriptions/<sub-id>/resourceGroups/<rg>/providers/Microsoft.Storage/storageAccounts/<account>/blobServices/default/containers/my-container"
```

### Granular Permission Breakdown

| Permission Action | Required By | Reason |
|---|---|---|
| `Microsoft.Storage/.../blobs/read` | All commands (`list`, `show`, `retry`, `stats`, `add`) | Lists existing record prefixes to find the next free ID and check deduplication fingerprints. |
| `Microsoft.Storage/.../blobs/write` | Worker quarantining (`add`), `retry` bookkeeping | Uploads serialized inputs, tracebacks, and claims candidate IDs. |
| `Microsoft.Storage/.../blobs/delete` | `retry`, `clear`, `reindex` | Deletes recovered items after successful retry and cleans up crashed write debris. |

### Read-Only Inspection Role

For monitoring agents, audit pipelines, or on-call engineers:
- Use [`Storage Blob Data Reader`](https://learn.microsoft.com/en-us/azure/role-based-access-control/built-in-roles#storage-blob-data-reader) instead.
- `quarantine list`, `show`, `debug`, and `ui` work normally. Mutative operations (`clear`, `retry`) will fail with access-denied errors.

---

## Architectural Guarantees & Consistency (ADR 0007)

Azure Blob Storage does not provide POSIX directory renames. The Azure backend translates local directory guarantees into Azure primitives:

1. **Commit Point Protocol**:
   Record assets (`input.pickle`, `traceback.txt`) are uploaded first. The metadata document (`meta.json`) is uploaded **strictly last**. A reader ignores any prefix that lacks `meta.json`. A worker terminating mid-write leaves invisible debris that never corrupts reader state.
2. **Atomic ID Allocation via Conditional Write**:
   Workers claim numeric record IDs by uploading a zero-byte `.claim` blob with `overwrite=False` (which sends `If-None-Match: *`). If another worker claimed the ID concurrently, Azure returns HTTP 409 `ResourceExistsError` (or HTTP 412), causing the runner to increment and claim the next candidate ID.
3. **Zero Stale Listing Index**:
   Azure Blob Storage provides strong read-after-write consistency for blob listings. Quarantine reads directly from live container listings—there is no `index.json` file that can get corrupted or drift out of sync.
4. **Secure Local Read Caching**:
   Records downloaded for inspection (`show`, `debug`, `retry`) are cached in `$TMPDIR/quarantine-azure-<hash>`. Path traversal prevention and symlink rejection protect the host environment.

---

## Frequently Asked Questions (FAQ)

### Why do I need to specify the storage account for Azure, but not for AWS S3?
In AWS S3, bucket names are globally unique across all AWS accounts, so `s3://bucket-name` uniquely identifies the resource. In Azure, container names are scoped to an individual storage account. You can specify the account via:
- Environment variable: `export AZURE_STORAGE_ACCOUNT_NAME="myaccount"`
- Connection string: `export AZURE_STORAGE_CONNECTION_STRING="..."`
- Inline URL: `azure://myaccount@my-container/prefix`

### How do I configure Workload Identity on Azure Kubernetes Service (AKS)?
1. Ensure Workload Identity is enabled on your AKS cluster.
2. Create a User-Assigned Managed Identity and assign `Storage Blob Data Contributor` on the storage container.
3. Establish a federated identity credential between the managed identity and your Kubernetes ServiceAccount:
   ```bash
   az identity federated-credential create \
     --name "aks-worker-federation" \
     --identity-name "worker-identity" \
     --resource-group "my-rg" \
     --issuer "<aks-oidc-issuer-url>" \
     --subject "system:serviceaccount:default:worker-sa"
   ```
4. Label your pod with `azure.workload.identity/use: "true"`. Quarantine will automatically authenticate via `DefaultAzureCredential`.

### What happens if a worker container terminates while uploading a record?
Because `meta.json` is uploaded last as the commit point, incomplete uploads remain completely invisible to readers. To clean up uncommitted upload debris, run:
```bash
quarantine reindex --dir azure://my-container/etl/jobs
```

### Can I use Azure Sovereign Clouds (US Government, China)?
Yes. Pass the fully qualified domain name in the URL:
```bash
quarantine list --dir azure://govaccount.blob.core.usgovcloudapi.net/my-container/prefix
```
Quarantine recognizes the sovereign cloud domain and directs all authentication and blob calls to the correct regional endpoints.
