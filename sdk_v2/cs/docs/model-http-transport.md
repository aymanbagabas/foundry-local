# Model HTTP transports

`Configuration.ModelHttpTransport` replaces HTTP for **public catalog, registry
resolution and blob model acquisition**. Foundry retains download orchestration,
retry decisions, range concurrency, cache/resume, checksums and completion.
Inference, telemetry and execution-provider downloads are not covered.
Callbacks stay client-local with `UseSharedHost`.

Requires native **API 5**, transport contract **1**; item payload version stays **2**.
The SDK negotiates 5 → 4 → 3 → 2. Older runtimes remain usable without a transport;
configuring one explicitly fails with `NotImplemented` rather than reading a
missing table entry or silently falling back to built-in networking.
`CatalogType.AzureRegistry` with a transport currently returns `NotImplemented`.

## HttpClient adapter

```csharp
using var transport = new HttpClientModelHttpTransport();
await FoundryLocalManager.CreateAsync(new Configuration
{
    AppName = "my-app",
    ModelHttpTransport = transport,
    DisableNonessentialTelemetry = true
}, NullLogger.Instance);
using var manager = FoundryLocalManager.Instance; // Dispose BEFORE transport.
var catalog = await manager.GetCatalogAsync();
var models = await catalog.ListModelsAsync();
```

The adapter owns its handler and uses `ResponseHeadersRead` and cancellable stream
reads. Automatic redirects, decompression, cookies and retry policies are disabled.
It uses one-shot HTTP/1.1 connections (`Connection: close`, zero pooled connection
lifetime, a per-operation connection-attempt guard) and exact HTTP version policy.
The guard rejects HttpClient's internal replay before opening another socket.
This deliberately trades connection reuse for native
coordinator ownership of retries. HTTP status codes (including 3xx, 4xx, 5xx) are returned
as responses. TLS validation is never disabled.

On .NET 8/9 the adapter preserves escaped path/query text without URI canonicalization
and uses Latin-1 header encoding to round-trip field bytes. Constructing the adapter
from the netstandard2.0 assembly throws `PlatformNotSupportedException`: that platform's
HttpClient API cannot prevent internal replay. The full custom transport interface
and native registration remain available on netstandard2.0.
For application-specific proxy, TLS, authentication, connection-pool or stricter
wire-level requirements, implement `IModelHttpTransport` instead of injecting an
arbitrarily configured `HttpClient` with unverifiable redirect/retry policies.

## Custom implementation contract

* `CreateOperation(ModelHttpRequest)` allocates local state only; no network I/O.
  Requests are owned immutable snapshots: method, original URL, ordered duplicate
  headers, exact body bytes and total timeout milliseconds. Header/body getters
  return copies. Never log signed URLs, credentials or body data.
* The SDK owns the resulting `IModelHttpOperation` and calls `Dispose` exactly once,
  even if send never starts or fails. The application owns the transport and keeps
  it usable until all managers using it have been disposed. Configuration replacement
  does not alter an existing manager. Caller configuration collections are not mutated.
* `SendAsync(CancellationToken)` sends once and returns a `ModelHttpResponseHead`
  without buffering the body. `ReadAsync(Memory<byte>, CancellationToken)` then
  streams into the supplied buffer. Reads are serialized per operation; different
  operations may run concurrently. Short reads are valid. Zero is permanent EOF.
  The buffer must not be used after the task completes.
* Respect GET/HEAD/POST, original signed URL path/query, range/conditional/checksum
  fields and exact POST bodies. Never automatically retry, follow redirects, or
  decompress content. Header values are bytes, not necessarily UTF-8. Duplicates
  remain separate; do not comma-split. NUL, CR and LF are rejected.
* Cancellation is sticky, may precede send, and interrupts blocked send/read through
  their token. Implementations **must honor tokens promptly** and must not register
  blocking cancellation callbacks. One deadline spans send and all reads, beginning
  at send; zero means no deadline. Custom operation timeouts are cooperative: ignoring
  the token can block native workers and manager disposal. Deadline expiry maps to
  native `Network`; explicit native cancellation maps to `OperationCancelled`.
* Native workers synchronously wait for asynchronous operation tasks. Do not depend
  on a caller synchronization context, recursively acquire models, or dispose managers
  from callbacks. Use `ConfigureAwait(false)` in implementation code.
* HTTP errors are normal response heads. Managed transport exceptions become generic,
  secret-safe statuses allocated by the **same loaded native runtime**, with ownership
  transferred to native. Exceptions never cross callbacks (including `Dispose`).

The bridge roots callback delegates, transport contexts and operation handles through
native retain/release ownership. Response header allocations survive until operation
release. Library loads are process-lifetime; callbacks cannot outlive an unloaded runtime.
The native coordinator drains callbacks before releasing operations; the bridge also
drains its deadline timer before disposing the token source.

## Deterministic registered native example

[`examples/ModelHttpTransport`](../examples/ModelHttpTransport) is explicitly an
**in-memory** transport. It registers through public `Configuration`, initializes a
real native manager and parses an empty catalog through repeated three-byte reads.
It asserts zero models, actual callback invocation and balanced operation release.
No external network, credentials, inference or model weights are required.

```sh
dotnet run --project sdk_v2/cs/examples/ModelHttpTransport
```

For an actual HTTP catalog endpoint, append `-- --http <catalog-url>`. That mode
registers `HttpClientModelHttpTransport` through the same public configuration and
prints the model count; unlike the default in-memory mode it performs network I/O.

Use the normal SDK native runtime package or local native redirect described in the
README. The focused TUnit suite is `ModelHttpTransportTests`; invoke its executable
runner with `--treenode-filter '/*/*/ModelHttpTransportTests/*'` (a successful
`dotnet test` exit alone does not establish that TUnit executed tests).
