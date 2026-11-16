# Custom model HTTP transport

`Configuration(model_http_transport=transport)` selects application-owned HTTP for
model acquisition. This requires **native API 5**. The binding negotiates API
5 → 4 → 3 → 2; without a transport, older runtimes continue to work. A configured
transport is explicitly rejected on API 2–4, before reading the new table slot.
The HTTP structures use version **1**; other versioned SDK payloads remain at **2**.

The public API exports `HttpHeader`, `HttpRequest`, `HttpResponseHead`,
`ModelHttpTransport`, and `ModelHttpOperation` from `foundry_local_sdk`.
No particular Python HTTP client is required.

```python
from foundry_local_sdk import Configuration, FoundryLocalManager

config = Configuration(
    app_name="MyApp",
    model_http_transport=my_transport,  # implements ModelHttpTransport
)
with FoundryLocalManager(config) as manager:
    models = manager.catalog.list_models()
```

Registration does not mutate `config`, its dictionaries, or its transport.
Omitting the transport (`None`) selects native HTTP; transport failures never
fall back to native HTTP. A manager keeps the transport registered at creation.

## Optional HTTPX adapter

Install the extra, then explicitly import the adapter. Importing the core SDK
does not import HTTPX or the adapter. Missing HTTPX produces an actionable
`ImportError`; missing transitive dependencies retain their original error.

```bash
pip install 'foundry-local-sdk[httpx]'
```

```python
import ssl
import httpx
from foundry_local_sdk import Configuration, FoundryLocalManager
from foundry_local_sdk.httpx_transport import HttpxModelHttpTransport

with HttpxModelHttpTransport(
    verify=ssl.create_default_context(),
    limits=httpx.Limits(max_connections=16, max_keepalive_connections=8),
    # proxy="http://my-proxy:8080",
) as transport:
    config = Configuration(app_name="MyApp", model_http_transport=transport)
    with FoundryLocalManager(config) as manager:
        models = manager.catalog.list_models()
```

The adapter implements the neutral `ModelHttpTransport` protocol with one owned
`httpx.AsyncClient` and event-loop thread. `create_operation` allocates state only.
Synchronous SDK callbacks wait on async tasks, releasing the GIL while waiting.
`cancel` cancels the **actual pending send/read task** on that loop. The synchronous
callback waits until cancellation has unwound I/O before returning; cancelling
the cross-thread future alone would return too early. The total deadline starts
at `send` and covers later reads, including buffered chunks.

The default network transport uses HTTP/1.1 with zero connection retries.
Redirect following and client authentication flows (which can replay requests)
are disabled on each send. Response bytes come from `aiter_raw()`, not decoded
content. Oversized response chunks are retained for subsequent partial reads;
only the synchronous caller writes the borrowed native buffer. Duplicate request
and response fields, binary bodies, range/conditional headers and encoded signed
query/path bytes are preserved.

The adapter fails **before I/O** if HTTPX would rewrite a request target. Supply
absolute HTTP(S) URLs without userinfo or fragments and percent-encode non-ASCII
or space characters. Literal `.`/`..` path segments that HTTPX normalizes are
rejected rather than silently changing a signed request.

### Options and customization

- `proxy`: an explicit HTTPX proxy URL or `httpx.Proxy`.
- `verify`: the HTTPX TLS verification setting, preferably `True` (the default)
  or an application-configured `ssl.SSLContext`.
- `limits`: `httpx.Limits` for connection pooling.
- `transport`: an application-created `httpx.AsyncBaseTransport`. Configure its
  proxy, TLS and limits yourself; combining it with those adapter options raises
  `ValueError` rather than silently ignoring them.
- `client_factory`: a synchronous callable receiving the network transport and
  returning a **fresh** `httpx.AsyncClient`. It runs on the adapter's loop; use
  the supplied network transport. This supports application event hooks and
  instrumentation without making the core interfaces depend on HTTPX.

The default transport disables HTTPX environment configuration; pass proxy and
TLS settings explicitly. A custom transport may implement other network policy.
For example:

```python
def create_client(network: httpx.AsyncBaseTransport) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=network,
        follow_redirects=False,
        timeout=None,
        trust_env=False,
        # event_hooks={"request": [my_async_request_hook]},
    )

transport = HttpxModelHttpTransport(client_factory=create_client)
```

The adapter does not mutate caller configuration or change client attributes to
disable redirects/authentication. Standalone native requests deliberately do
**not** merge factory-client default headers, query parameters, cookies or auth.
Supply authentication as native request headers or via a compatible application
request hook. Native `timeout_ms` is the authoritative total budget; client
per-I/O timeouts are disabled on these requests.

Custom transports, factories and hooks are trusted application code: they must
not add retries, redirects, request replay, automatic decoding, eager body
buffering, or cancellation suppression. Do not call synchronous adapter methods
from its loop (including hooks); this is rejected to avoid deadlocks.

### Ownership and cleanup

**Keep the adapter alive and open through all manager usage. Close managers first,
then the adapter.** A supplied HTTPX transport and the factory's returned client
transfer ownership to the adapter; do not share or separately close them.
The existing native-rooted bridge retains the Python adapter and callback/runtime
state independently of Python configuration/manager scope; it does not close an
application-owned adapter when a manager releases it.

`close()` cancels/drains outstanding operations, closes responses and the client,
shuts down async generators/executor work, and joins the loop thread. It is
idempotent and can interrupt outstanding socket I/O, but early adapter disposal
still invalidates later manager requests. Cleanup failures are reported after
shutdown as an `ExceptionGroup`, including response-close errors encountered by
the otherwise-infallible native release callback. Use explicit close/context
management, not the finalizer safety net.

Cancellation of standard HTTPX socket awaits is tested with real blocked
connections. Native integration additionally blocks an HTTPX response body through
real SDK registration until the native catalog's 30-second budget expires, then
checks socket closure and operation/loop cleanup. Custom transports must cooperate
with task cancellation. Platform
synchronous DNS resolution cannot be forcibly terminated; executor shutdown
waits for resolver work to finish rather than leaking it. HTTPX application
logging/hooks can include request URLs; configure redaction before enabling
verbose logs for signed URLs.

[`examples/httpx_model_http_transport.py`](../examples/httpx_model_http_transport.py)
executes real public SDK registration and catalog requests using HTTPX against a
loopback HTTP server, with no external catalog, model weights or credentials:

```bash
FOUNDRY_LOCAL_LIB_DIR=/absolute/path/to/native/bin \
PYTHONPATH=src python examples/httpx_model_http_transport.py
```

## Implement the synchronous protocols

```python
class ModelHttpTransport(Protocol):
    def create_operation(self, request: HttpRequest) -> ModelHttpOperation: ...

class ModelHttpOperation(Protocol):
    def send(self) -> HttpResponseHead: ...
    def readinto(self, buffer: memoryview) -> int: ...
    def cancel(self) -> None: ...
    def close(self) -> None: ...
```

- `create_operation` allocates local state only. It must return promptly, with
  **no network I/O**, even DNS or connection establishment.
- `HttpRequest` is an owned, frozen snapshot: method and UTF-8 URL as `str`,
  headers as a tuple of `HttpHeader`, POST body as `bytes`, and `timeout_ms`.
  Bodies can contain embedded NUL. Zero timeout means no deadline.
- `HttpHeader.name` and `.value` are `bytes`, not decoded text. Duplicate fields
  retain their order. Names are HTTP tokens; NUL, CR and LF are rejected in values.
  At some internal native metadata boundaries, only one value per name survives.
- `send` performs one request and returns when headers are available, without
  buffering the entire body. Return `HttpResponseHead(status_code, headers)`.
  HTTP statuses 100–599, including redirects and HTTP errors, are **responses**,
  not transport exceptions.
- `readinto` writes at most `len(buffer)` bytes and returns an integer count.
  Short reads are valid; zero means permanent EOF. The writable view is borrowed
  only for that call: do not retain it or derived views. Stream model bodies
  rather than buffering entire files.
- `cancel` must promptly interrupt blocked `send` and `readinto`, without taking
  a lock held by either. It must be thread-safe and idempotent.
- `close` releases operation resources exactly once, including for an unsent,
  cancelled, or failed operation. The SDK calls it after active native callbacks
  drain; the application must not separately close an SDK-owned operation.
  The application owns the transport/client/pool itself, and must keep any
  associated event loop running until all managers and operations have closed.

`send` and `readinto` are serialized per operation, but different operations may
run concurrently. Callbacks can run on the calling thread or foreign native
workers. Blocking network calls **must release the GIL**, so another thread can
cancel them. Do not dispose managers or recursively acquire models from callbacks.
Explicitly close managers before interpreter shutdown.

## Cancellation and deadlines

The bridge makes cancellation sticky, including cancellation before `send`.
It starts a total deadline timer on entry to `send`, not at operation creation
and not anew for each read. On expiry it calls the operation's `cancel` from
another Python thread. It checks cancellation/deadline state before and after
calls, translating cancellation to native error 5 and timeout/network failures
to native error 6.

**A timer or flag cannot by itself interrupt I/O.** Your operation must actually
abort its socket/request/future in `cancel`. A socket implementation might use
thread-safe socket shutdown; an async implementation must cancel the underlying
I/O on an application-owned active event loop. Merely wrapping `requests` and
setting a cancellation flag does not satisfy this interface. This release
intentionally includes no production `requests` adapter.

The tests exercise blocking socket I/O interrupted by cancellation and deadlines.
The native suite additionally drives a blocked read through a registered catalog
transport using the real native 30-second budget, not an overridden fake timeout.

## HTTP policy

The transport controls TLS trust, proxies, authentication, and connection pools.
Keep certificate verification enabled. Preserve exact paths, signed queries,
range/conditional/checksum headers, and POST bytes. Return bytes after HTTP framing
removal, **without content decompression**. Disable all automatic retries and
redirect following, including hidden client replay behavior. Foundry owns retry,
region selection, download concurrency, ETag/checksum validation, and resume policy.

Raise an exception for DNS, connection, TLS, timeout, or response-stream failures.
The callback boundary contains **all `BaseException` subclasses**, including
`KeyboardInterrupt` and `SystemExit`; these cannot escape through C. Fallible
callbacks return owned native statuses created by the **same loaded runtime**.
Messages are deliberately generic so exception strings cannot expose credentials,
signed URLs, or request bodies. Lifecycle/cancel exceptions are contained because
those callbacks have no native error return; implementations should not raise there.

## Scope and ownership

Supported paths: the public catalog, registry resolution, blob enumeration, HEAD
and range/body downloads. Telemetry, inference traffic and execution-provider
downloads are outside this interface. **Azure Registry catalogs
(`CatalogType.AZURE_REGISTRY`) are explicitly unsupported** with a custom transport:
native code rejects them before credentials or network access.

In shared-host mode acquisition and callbacks stay in the client process; no
Python callback or handle is transferred to the shared host.

Native configuration copies the callback table and retains the bridge. Each
native retain owns a CPython reference; release balances it independently of
Python configuration/manager scope and cyclic garbage collection. Operation
handles, callback code, runtime library handles, owned request snapshots, and
response-header arrays/strings stay alive through operation release. No global
transport registry is used. The existing API-mode cffi package targets CPython;
other Python implementations are not supported by this rooting mechanism.

## Executable example

[`examples/custom_model_http_transport.py`](../examples/custom_model_http_transport.py)
registers an in-memory implementation through real SDK `Configuration` and
`FoundryLocalManager`, then queries the native public catalog. It returns an
empty catalog in three-byte reads, demonstrates duplicate response headers, and
checks balanced operation cleanup. It needs no model weights, credentials or network.
It is a deterministic demonstration, **not a network adapter**.

```bash
cd sdk_v2/python
FOUNDRY_LOCAL_LIB_DIR=/absolute/path/to/native/bin \
PYTHONPATH=src python examples/custom_model_http_transport.py
```

For development, compile the cffi wrapper against the current C header (no native
rebuild is needed if API 5 binaries already exist):

```bash
python src/foundry_local_sdk/_native/build_cffi.py
python -m pytest -q test/unit/test_model_http_transport.py
python -m pytest -q test/unit/test_httpx_transport.py
FOUNDRY_LOCAL_LIB_DIR=/absolute/path/to/native/bin \
python -m pytest -q test/integration/test_model_http_transport_native.py \
    test/integration/test_httpx_transport_native.py
```

The native test file fails rather than skips when its native dependencies are
missing. Loading follows the existing ORT → GenAI → Foundry preload contract.
See the [authoritative native contract](../../cpp/docs/model-http-transport.md).
