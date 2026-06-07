# Deploying ARG Telemetry to Azure Event Hubs

This guide covers deploying the ARG telemetry pipeline to Azure Event Hubs using the
Kafka protocol endpoint.  The existing `kafka_pipeline.py` in
`examples/financial-circuit-breaker/src/` connects without code changes — Event Hubs
exposes a Kafka-compatible surface on port 9093.

---

## Prerequisites

- Azure CLI: `az --version` (≥ 2.50)
- An Azure subscription with Contributor access on the target resource group
- Python 3.11+ with `confluent-kafka` installed

---

## 1. Resource Group and Namespace

```bash
# Set variables
RG="arg-governance-rg"
LOCATION="eastus"
NAMESPACE="arg-eventhubs"          # must be globally unique
SKU="Standard"                     # Standard tier required for Kafka protocol

az group create --name $RG --location $LOCATION

az eventhubs namespace create \
  --name $NAMESPACE \
  --resource-group $RG \
  --location $LOCATION \
  --sku $SKU \
  --enable-kafka true \
  --minimum-tls-version 1.2
```

**Note:** The `--enable-kafka` flag enables the Kafka protocol endpoint. It is only
available on Standard tier and above. The Kafka endpoint is:
```
${NAMESPACE}.servicebus.windows.net:9093
```

---

## 2. Create the Three Event Hubs

ARG uses three hubs matching `config/sidecar-proxy/eventhubs-pipeline.yaml`:

```bash
# arg-telemetry-events — main audit stream, 12 partitions
az eventhubs eventhub create \
  --name arg-telemetry-events \
  --namespace-name $NAMESPACE \
  --resource-group $RG \
  --partition-count 12 \
  --message-retention 7 \
  --enable-capture true \
  --capture-interval 300 \
  --capture-size-limit 314572800 \
  --destination-name EventHubArchive.AzureBlockBlob \
  --storage-account "/subscriptions/<sub-id>/resourceGroups/$RG/providers/Microsoft.Storage/storageAccounts/argauditstore" \
  --blob-container arg-audit-archive \
  --archive-name-format "{Namespace}/{EventHub}/{PartitionId}/{Year}/{Month}/{Day}/{Hour}/{Minute}/{Second}"

# arg-circuit-breaker-fired — circuit breaker events, 2 partitions
az eventhubs eventhub create \
  --name arg-circuit-breaker-fired \
  --namespace-name $NAMESPACE \
  --resource-group $RG \
  --partition-count 2 \
  --message-retention 7

# arg-wallet-lifecycle — wallet issuance/revocation events, 4 partitions
az eventhubs eventhub create \
  --name arg-wallet-lifecycle \
  --namespace-name $NAMESPACE \
  --resource-group $RG \
  --partition-count 4 \
  --message-retention 7
```

Create the audit storage account if it doesn't exist:

```bash
az storage account create \
  --name argauditstore \
  --resource-group $RG \
  --location $LOCATION \
  --sku Standard_LRS \
  --kind StorageV2

az storage container create \
  --name arg-audit-archive \
  --account-name argauditstore
```

---

## 3. Managed Identity (No Connection Strings in Code)

Create a user-assigned managed identity for the ARG enforcement service:

```bash
az identity create \
  --name arg-enforcement-identity \
  --resource-group $RG

# Assign Azure Event Hubs Data Sender role on the namespace
IDENTITY_PRINCIPAL=$(az identity show \
  --name arg-enforcement-identity \
  --resource-group $RG \
  --query principalId -o tsv)

NAMESPACE_ID=$(az eventhubs namespace show \
  --name $NAMESPACE \
  --resource-group $RG \
  --query id -o tsv)

az role assignment create \
  --assignee $IDENTITY_PRINCIPAL \
  --role "Azure Event Hubs Data Sender" \
  --scope $NAMESPACE_ID

# Audit consumers get Data Receiver
az role assignment create \
  --assignee $IDENTITY_PRINCIPAL \
  --role "Azure Event Hubs Data Receiver" \
  --scope $NAMESPACE_ID
```

Attach the identity to your compute resource:

```bash
# For Azure Container Apps
az containerapp identity assign \
  --name arg-enforcement-sidecar \
  --resource-group $RG \
  --user-assigned arg-enforcement-identity

# For AKS (attach to node pool or use pod identity / workload identity)
# See: https://learn.microsoft.com/azure/aks/workload-identity-overview
```

---

## 4. Connect kafka_pipeline.py to Event Hubs

Event Hubs exposes a Kafka-compatible endpoint. The existing `kafka_pipeline.py`
connects with these settings:

```python
# In kafka_pipeline.py or via environment variables
KAFKA_CONFIG = {
    "bootstrap.servers": f"{NAMESPACE}.servicebus.windows.net:9093",
    "security.protocol": "SASL_SSL",
    "sasl.mechanism":    "PLAIN",
    # For managed identity, use the OAuth bearer token flow:
    "sasl.username":     "$ConnectionString",
    "sasl.password":     os.environ["ARG_EVENTHUBS_CONNECTION_STRING"],
}
```

**For production (managed identity instead of connection string):**

Event Hubs supports OAuth 2.0 bearer tokens via managed identity.  Generate a
token using `azure-identity` and pass it as the SASL password:

```python
from azure.identity import DefaultAzureCredential
from azure.core.credentials import AccessToken

credential = DefaultAzureCredential()
token: AccessToken = credential.get_token(
    "https://eventhubs.azure.net/.default"
)

KAFKA_CONFIG = {
    "bootstrap.servers":  f"{NAMESPACE}.servicebus.windows.net:9093",
    "security.protocol":  "SASL_SSL",
    "sasl.mechanism":     "OAUTHBEARER",
    "sasl.oauthbearer.config": f"token={token.token}",
}
```

**Topic mapping** — Event Hub names match the Kafka topic names in
`config/sidecar-proxy/kafka-pipeline.yaml` exactly:

| Kafka topic | Event Hub |
|---|---|
| `arg.telemetry.events` | `arg-telemetry-events` |
| `arg.circuit_breaker.fired` | `arg-circuit-breaker-fired` |
| `arg.wallet.lifecycle` | `arg-wallet-lifecycle` |

---

## 5. Audit Storage (Capture)

Capture is enabled on `arg-telemetry-events` in step 2.  Captured files land in:
```
arg-audit-archive/{Namespace}/{EventHub}/{PartitionId}/{Year}/{Month}/{Day}/{Hour}/{Minute}/{Second}
```

Files are Avro format by default.  To retain as JSON for direct querying in Synapse
or Data Explorer, configure the capture format at the namespace level:

```bash
az eventhubs eventhub update \
  --name arg-telemetry-events \
  --namespace-name $NAMESPACE \
  --resource-group $RG \
  --capture-encoding Json
```

---

## 6. Consumer Group for Audit Replay

Create the audit consumer group required by `config/sidecar-proxy/kafka-pipeline.yaml`:

```bash
az eventhubs eventhub consumer-group create \
  --name arg-audit-consumer \
  --eventhub-name arg-telemetry-events \
  --namespace-name $NAMESPACE \
  --resource-group $RG
```

---

## 7. Verify

Test the connection from a local machine using the Kafka CLI or `confluent-kafka`:

```python
from confluent_kafka.admin import AdminClient

client = AdminClient({
    "bootstrap.servers": f"{NAMESPACE}.servicebus.windows.net:9093",
    "security.protocol": "SASL_SSL",
    "sasl.mechanism":    "PLAIN",
    "sasl.username":     "$ConnectionString",
    "sasl.password":     os.environ["ARG_EVENTHUBS_CONNECTION_STRING"],
})

# List topics — should include all three ARG hubs
metadata = client.list_topics(timeout=10)
print([t for t in metadata.topics.keys() if t.startswith("arg")])
```

---

## 8. Key Vault Integration (Signing Key)

The ARG signing key must be stored in Key Vault, not in the enforcement service.
Create the key and grant the enforcement identity access to sign with it:

```bash
KV_NAME="arg-keyvault"

az keyvault create \
  --name $KV_NAME \
  --resource-group $RG \
  --location $LOCATION \
  --sku premium \
  --enable-rbac-authorization true

# Create the signing key (EC P-256 = ES256)
az keyvault key create \
  --vault-name $KV_NAME \
  --name arg-signing-key \
  --kty EC \
  --curve P-256 \
  --ops sign verify

# Grant the enforcement identity Sign permission
az role assignment create \
  --assignee $IDENTITY_PRINCIPAL \
  --role "Key Vault Crypto User" \
  --scope $(az keyvault show --name $KV_NAME --resource-group $RG --query id -o tsv)
```

Then wire in the signer at startup:

```python
from ops.kms_signing.src.azure_signer import AzureKeyVaultSigner
from examples.sycophancy_loop_detection.src.telemetry_emitter import TelemetryEmitter

signer = AzureKeyVaultSigner(
    vault_url=f"https://{KV_NAME}.vault.azure.net/",
    key_name="arg-signing-key",
    algorithm="ES256",
)

emitter = TelemetryEmitter(
    session_id=session_id,
    user_id=user_id,
    model_deployment_id="claude-sonnet-4-6-tutoring",
    signer=signer,
)
```

See `ops/kms-signing/README.md` for full KMS integration details including key rotation.

---

## Architecture Reference

```
Agent runtime
    │
    ▼
Enforcement sidecar (this service)
    │   ├── ProxyGate.request()          evaluates every outbound call
    │   ├── FinancialCircuitBreaker      monitors call patterns
    │   └── DriftDetector               monitors output vectors
    │
    ├─► arg-telemetry-events            telemetry-payload.json per turn
    ├─► arg-circuit-breaker-fired       BreakerEvent on state transition
    └─► arg-wallet-lifecycle            wallet issuance / revocation

        ▼ (async, non-blocking, max_block_ms=100)

Event Hubs namespace
    ├── Capture ──► Azure Blob Storage  (7-day retention → long-term archive)
    └── Consumer group ──► Audit service / SIEM
```

The enforcement gate emits to Event Hubs asynchronously and never blocks
the synchronous enforcement decision on telemetry I/O.
