# Document, filesystem and communication driver support

This page distinguishes implemented behavior from external service evidence.
An injected SDK-port test proves adapter translation and shared behavior; it is
not certification of a running cloud or database service.

| Subsystem | Implemented behavior | Evidence available in this checkout | Remaining work |
|---|---|---|---|
| DocumentManager / MongoDB | Named, lazy, application-owned connections; runtime topology/version capability checks; BSON-preserving CRUD, aggregation, indexes, bulk operations, explicit sessions, commit/abort, time-series options and retention configuration | Unit regressions and public `create_application(providers=...)` lifecycle test; opt-in real suite | Run real standalone and replica-set profiles; verify elapsed retention; search outbox/checkpoint projection |
| Local / memory filesystem | Existing logical-key, binary, range, checksum and scoped/read-only behavior | Existing shared local tests | Platform-specific filesystem certification |
| S3-compatible filesystem | Streamed multipart writes with abort on failure, bounded reads, range, paginated list, metadata/checksum, copy/move/delete | Injected S3 SDK-port tests plus shared binary-integrity behavior | Real S3 and chosen compatible service profiles |
| Azure Blob filesystem | Chunked SDK download/upload, range, list, metadata/checksum, copy/move/delete | Injected Azure SDK-port tests plus shared behavior | Real Azure/Azurite profile |
| GCS filesystem | SDK file streams with generation-pinned reads, range, list, metadata/checksum, copy/move/delete | Injected GCS SDK-port tests plus shared behavior | Real GCS profile |
| SFTP filesystem | Bounded read/write, traversal-safe logical keys, discovered symlink rejection, private creation permissions, list/copy/move/delete | Local-filesystem-backed SFTP port test | Real SFTP host profile and host-key configuration evidence |
| NotificationService / MailManager | Versioned queue jobs; per-recipient/channel consent and current-policy checks; locale templates; quiet hours; attachment references; named, failover and round-robin channels | Unit routing, race, replay, codec, revocation, quiet-hour and failure tests | Provider-specific channels, authenticated feedback webhooks, production queue deployment |
| SMTPChannel | Explicit TLS/authentication, MIME rendering, one recipient per send, bounded checksum-verified attachments, acceptance/temporary rejection/permanent rejection/unknown outcomes | Actual loopback SMTP wire test and injected TLS/failure tests | Production SMTP service and delivery-feedback profile |
| Other communication channels | Typed injected `ChannelDriver` extension point for SMS, chat, webhook, push, database and realtime | No-network recording driver | Vendor-specific transport implementations and service evidence |
| Pub/sub and broadcast | Existing in-process subscription/replay/checkpoint and authorized broadcast implementations | Existing local tests | Durable external event-stream adapters and restart evidence; process-memory replay is not durable storage |

## Document registration

`DocumentManager` is available from `af.domain.document.manager`.
`mongo_document_registration` is available from
`af.infrastructure.mongo.provider`. The package does not connect to MongoDB
unless the application explicitly installs a provider and resolves a connection.

```python
from af.bootstrap import create_application
from af.domain.document.manager import DocumentManager, bind_document_manager
from af.framework.container import ProviderRegistrar, ServiceProvider
from af.infrastructure.mongo.provider import mongo_document_registration

class Documents(ServiceProvider):
    provider_id = "app.documents"

    def register(self, registrar: ProviderRegistrar) -> None:
        bind_document_manager(
            registrar,
            connections={"primary": {
                "driver": "mongodb",
                "url": "mongodb://localhost:27017",
                "database": "application",
            }},
            default="primary",
            deployment_profile="local",
            policy_version="v1",
            registrations=(mongo_document_registration(),),
        )

app = create_application(providers=(Documents(),))
documents = app.container.resolve(DocumentManager)
# Within async application work:
# async with documents.transaction() as session:
#     await documents.insert_one("records", {"value": 3}, session=session)
# Every operation in the transaction must explicitly carry session=.
app.shutdown()
```

A standalone server rejects transaction entry before creating a session or
performing a write. Single-member replica sets can support transactions; host
count is not used as a substitute for topology. A session from another driver,
or an already closed session, is rejected before database access.

The existing MongoDB API remains asynchronous in shape over synchronous PyMongo
calls. Hosts requiring nonblocking database IO should isolate these calls in a
worker; this release does not claim a native asynchronous MongoDB adapter.

Run the optional real-service profile with `AF_MONGODB_TEST_URL` configured and
`pytest tests/integration/test_mongodb_document.py`. The fixture creates and
removes a uniquely named test database. A skip is not service evidence. Current
retention checks inspect configuration; they do not wait for MongoDB's TTL monitor.

## Storage clients and capabilities

Import adapters from `af.filesystem.cloud` or `af.filesystem.sftp`. Supply an
already configured SDK client/container/bucket or authenticated SFTP client.
SDK installation, credentials, TLS/host-key checks, retries and remote-access
policy remain explicit application configuration. The adapters do not install
SDKs or discover credentials on import. Clients remain caller-owned.

Use `object_filesystem_registration("s3", factory)` with an existing
`FilesystemManager` / `bind_filesystem_manager`. The factory accepts immutable
connection configuration and returns `S3FilesystemDriver(client, bucket)`;
Azure, GCS and SFTP use the same registration mechanism. Resolution probes the
configured bucket/container/root before publishing capability evidence.

Remote disks require an operator-configured private bucket/container/root.
Per-object public visibility, append, locking, externally managed multipart
sessions, version selection and signed URLs are not advertised by the concrete
cloud adapters. Unsupported operations raise explicitly. The generic
`ObjectStorageFilesystemDriver` can accept an explicit signing port when an
application supplies and verifies one.

Writes and reads use bounded chunks, without a whole-object memory buffer.
S3 uses 8 MiB multipart parts, imposing a 10,000-part (about 78 GiB) adapter limit;
a failure aborts the unfinished multipart upload. Metadata SHA-256 inspection
streams the object, adding one object read; provider ETags are not SHA-256.
Metadata may expose an observed version identifier without promising historical
version access. Copy streams into a replacement object, and move then deletes
the source. Neither operation promises an atomic POSIX rename or concurrent
writer isolation.

SFTP rejects discovered symlinks and traversal keys. Its remote root must be
exclusively controlled: the protocol offers no portable `openat`/`O_NOFOLLOW`
primitive against a malicious concurrent path replacement. Normal remote writes
are not atomic and can leave partial content after an interrupted transfer.

## Communication delivery semantics

`NotificationService.plan` returns one immutable `QueueJob` per authorized,
consented recipient/channel; `enqueue` accepts the existing queue dispatch port.
The service is a queue-worker callable, and `deliver` additionally returns its
receipt. Recipient resolution and authorization are constructor-injected and
run again immediately before delivery, so revoked consent suppresses a queued
send. Quiet hours use the recipient's time zone and real UTC instants across DST.
A worker encountering newly changed quiet hours receives `DeliveryDeferred`;
its host must reschedule according to `until` if ordinary retry limits would
expire sooner. No high-level exactly-once guarantee is made.

`SQLiteDeliveryStore` claims an idempotency key transactionally across processes
and retains receipts across restart. Reusing a key with changed content or policy
fails. Only definite `RETRYABLE_FAILURE` can be reclaimed automatically. An
in-flight `PENDING` receipt after a crash and an `UNKNOWN` acceptance outcome
require reconciliation, preventing blind duplicate SMTP submissions. There is
no automatic recovery claim for the send/receipt crash gap.

`ACCEPTED` means the SMTP submission server accepted responsibility; it does not
mean an inbox received the message. Authenticated application feedback can
record `BOUNCED`, `COMPLAINT` or `SUPPRESSED`. The receipt store does not validate
external webhook signatures; a transport adapter must do that first.
Failover advances only after definite retryable non-acceptance. It never sends
to a second provider following an unknown acceptance outcome. Attachments retain
logical disk/key/digest references in the queue, and SMTP verifies bytes and the
configured aggregate size before opening a connection.

`tests/integration/test_smtp_local.py` submits only to a loopback capture server;
its `.test` recipient addresses are never sent to an external service.

Primary API references used for adapter semantics:

- [MongoDB time-series granularity](https://www.mongodb.com/docs/manual/core/timeseries/timeseries-granularity/)
- [MongoDB transaction deployment requirements](https://www.mongodb.com/docs/manual/core/transactions-production-consideration/)
- [Boto3 S3 API](https://docs.aws.amazon.com/boto3/latest/reference/services/s3.html)
- [Azure BlobClient](https://learn.microsoft.com/python/api/azure-storage-blob/azure.storage.blob.blobclient)
- [Google Cloud Storage Blob](https://docs.cloud.google.com/python/docs/reference/storage/latest/google.cloud.storage.blob.Blob)
