# Model HTTP transports

Set `FoundryLocalConfig.modelHttpTransport` to replace model-acquisition HTTP. The native coordinator still owns retries,
region selection, checksums, ranges, cache writes and resume state. There is no fallback to built-in HTTP after a custom
transport fails. Inference, telemetry and execution-provider downloads are not routed through this interface.

Requires a native **API 5** runtime. With no transport configured, API 2–4 runtimes remain supported. Configuring a
transport on those runtimes throws `FoundryLocalError` with `FlErrorCode.NotImplemented`, without reading configuration
slot 12. Transport structure version 1 and payload version 2 are unchanged. Custom transports currently reject
`CatalogType.AzureRegistry` before credential initialization; the public catalog's registry/blob paths are supported.

## Node HTTP(S) adapter

```ts
import { Agent } from "node:https";
import { FoundryLocalManager, NodeModelHttpTransport } from "foundry-local-sdk";

const agent = new Agent({ keepAlive: true, maxSockets: 8 });
try {
  using manager = new FoundryLocalManager({
    appName: "custom-network-policy",
    modelHttpTransport: new NodeModelHttpTransport({ httpsAgent: agent }),
  });
  const catalog = await manager.getCatalogAsync();
  const models = await catalog.getModels();
  console.log(models.map((model) => model.alias));
} finally {
  agent.destroy();
}
```

`httpAgent` and `httpsAgent` accept caller-owned Node Agents (or `false` to disable pooling). HTTPS Agents can configure
trusted CAs, client certificates, connection limits and application proxy routing. The adapter does not mutate supplied
options, change TLS certificate verification, destroy caller Agents, or add its own proxy/environment-variable policy.
It owns each ClientRequest/IncomingMessage until operation close. Dispose the manager before destroying shared Agents.
The transport itself owns no pool or native handle and does not require disposal.

### OS DNS and native worker concurrency

The adapter leaves Node's default lookup unchanged: `node:dns.lookup` uses the OS resolver, including its hosts-file,
NSS, search-domain and platform name-resolution policy. The adapter does not inject a resolver, replace OS policy with
c-ares, or change process-wide DNS settings or `UV_THREADPOOL_SIZE`.

Set `lookup` to a custom Node lookup function, or provide an Agent with its own lookup, to select application policy.
An explicit transport `lookup` takes precedence over the Agent. Ordinary Agents and lookups delegating to
`node:dns.lookup` require no extra pool capacity for native SDK calls waiting on that transport.

Network-capable native jobs owned by a manager with a JS transport execute on registered, joinable dedicated threads,
not the libuv worker pool needed by OS DNS and other Node clients. This includes catalog acquisition/queries, model
lifecycle/default-variant work and request preflight. Managers without a custom transport retain their existing libuv
dispatch. Completion returns through an environment-owned `uv_async_t` queue, without consuming a libuv worker slot.

Abort/close destroys the actual Node request/response and rejects pending adapter work. Node's underlying OS
`getaddrinfo` request is not independently cancellable by this adapter; late results cannot complete the closed HTTP
operation. No SDK pool-sizing requirement, DNS policy workaround, added retry, or extra network timeout is involved.

`NodeModelHttpTransportOptions.request` is an optional Node request factory for advanced network routing. It receives
the connection URL, Node RequestOptions and the response callback; return a normal `http.ClientRequest`. Preserve
`options.method`, `options.path`, the raw header array and body handling, and do not retry, redirect or decompress.
The adapter still owns the request, response, abort handling and total deadline. Policy injected by a custom factory or
Agent is the application's responsibility. Factories receive a lookup in RequestOptions only when the caller supplied
an explicit transport `lookup`; otherwise Node and the selected Agent retain their normal lookup behavior.
Never disable TLS verification as a routing shortcut.

The default factory uses `node:http`/`node:https`, not fetch:

- It sends the original path/query (including signed escapes and dot segments), preserving request headers and bodies.
  URL credentials and fragments are rejected. Header names are token strings; values are Latin-1 field bytes.
- Duplicate response fields and their ordering are preserved through `IncomingMessage.rawHeaders`.
- HTTP framing is removed, but content encoding is **not** decoded. A gzip response yields gzip bytes.
- Redirects, HTTP errors and authentication challenges are returned as response heads; there is no automatic follow-up.
- There are no adapter retries. Native coordinator retries remain native policy.
- Paused, demand-driven reads consume at most the requested capacity, including short reads and permanent zero-byte EOF.
  A model file is never buffered in full by the adapter.
- `timeoutMs` starts at send and covers headers and every body read. Zero disables the deadline. Abort and close destroy
  the actual pending request/response, independently of a blocked read.
- Adapter failures use credential-free messages; the public registration bridge also sanitizes application rejections.

An executable, network-independent example registers this adapter with a real native manager and fetches one catalog
model from a loopback server:

```sh
cd sdk_v2/js
npm run build
node examples/modelHttpTransport.mjs
# Native Node HTTP transport: 1 loopback catalog request(s), 1 model (example)
# Blocked body read cancelled with AbortSignal
```

Only the example's explicit request factory routes the catalog destination to loopback. The default adapter never
rewrites URLs. The example streams the catalog in three-byte chunks and then uses the same adapter to abort a blocked
body read. It needs no credentials, external service, model download or inference setup.

## Client-neutral interface

`ModelHttpTransport` does not depend on a particular Node client:

```ts
interface ModelHttpTransport {
  createOperation(request: ModelHttpRequest): ModelHttpOperation;
}

interface ModelHttpOperation {
  send(): Promise<ModelHttpResponseHead>;
  read(capacity: number): Promise<Uint8Array>;
  cancel(): void;
  close(): void;
}
```

The SDK supplies an owned request snapshot containing `method` (`GET`, `HEAD` or `POST`), `url`, `body: Uint8Array`,
`headers: readonly ModelHttpHeader[]`, `timeoutMs` and `signal: AbortSignal`. A header is a readonly `[string, Uint8Array]`
pair. Do not mutate request buffers. A response head contains `statusCode` and the same duplicate-preserving header
shape. Field bytes need not be UTF-8; NUL, CR and LF are forbidden.

`createOperation` must allocate only: no I/O or waiting for network activity. Send runs once and resolves at headers.
Reads are serialized per operation, with a positive integral capacity; returning more bytes is an error. Empty bytes
mean permanent EOF, not temporary unavailability. Separate operations may execute concurrently.

`cancel` and `close` must be idempotent, nonblocking and nonthrowing. Interrupt actual pending networking in response
to `request.signal` **and/or** `cancel`, including cancellation before send. The SDK aborts the signal and invokes close
when releasing an operation; release happens after native callbacks drain. Cancellation is sticky. A rejected
`AbortError` means cancellation; other throws/rejections mean transport failure. Use credential-free messages.
Do not recursively acquire models or dispose the manager inside a callback.

### Event-loop safety and synchronous paths

Use **`await manager.getCatalogAsync()`**, not `manager.catalog` or `manager.getCatalog()`, with a JS transport.
Catalog list/lookups (`getModels`, `getModel`, cached/loaded queries, version queries) dispatch to native workers;
they are not Promise wrappers around synchronous native calls.

Configured transports explicitly reject network-capable native synchronous catalog methods, synchronous catalog
acquisition, web-service start/stop/shutdown, and constructors that could resolve a remote alias's default variant.
Use exact models obtained from `model.variants` or `catalog.getModelVariant()` for sessions. Exact remote variants and
local models still work; loaded local model/session construction is covered by real native integration tests.
Manager construction is allocation/configuration only. Existing local registration and cleanup paths remain available.

`Model.download(signal)` retains the SDK's existing **cooperative progress-checkpoint** cancellation contract.
The C++ public download method has no independent cancellation token, so a signal cannot interrupt an initial registry
request or HEAD before a progress checkpoint. This is separate from the transport callback's concurrent Cancel contract:
when native cancellation/deadline invokes Cancel, the bridge wakes native waiters immediately and aborts JS networking.
It never waits on the JS thread. Do not emulate synchronous networking with Atomics.wait, deasync or event-loop pumping.

## Binding implementation notes

The five layers are C ABI → public C++ wrapper → addon → TS detail dispatcher → public interface. The addon registers
through `Configuration::SetModelHttpTransport` and returns errors through public `foundry_local::CreateStatus` from the
same negotiated runtime. It does not call ABI tables or `foundry_local::detail` helpers.

Each configuration-retained callback context owns a dedicated TSFN. The TSFN roots the TS dispatcher, which roots the
transport and its per-operation objects/controllers. It is unreferenced while idle so an unused manager cannot keep
Node alive. Native release queues close; replacing or dropping caller configuration never invalidates manager ownership.
Request snapshots, response headers and read results have independent native-owned storage through operation release.

Promise settlement tickets never reference a worker stack or native read buffer. Cancel completes a waiting ticket
before scheduling JS abort/cancel; late settlements cannot access freed buffers. Release removes TS operation state
before cleanup. Environment teardown marks worker admission/completion closed, wakes native waiters and retains the
async cleanup hook until workers retire, without depending on user Promises settling. Node versions that drain workers
before cleanup hooks are detected by JS-thread N-API liveness checks. Admitted workers retain shared native-manager leases.
Dedicated native jobs are registered before launch and joined on the environment thread only after they finish.
Shutdown closes admission and wakes transport and progress waits without JavaScript, drains completions, and closes the
completion handle before removing the asynchronous cleanup hook. No thread is detached or forcibly terminated. Native
operations already running outside an interruptible transport/progress wait still follow the native runtime's own
cancellation and retry behavior; teardown waits for them to return.

Tests drive the public SDK, real addon/library and real HTTP sockets. They cover complete registry/list/HEAD/range
acquisition, partial reads/EOF, raw headers, no decompression/redirect/retry, cancellation, deadlines, config immutability,
native retention, disposal, pending-operation environment termination and API 2/3/4 rejection using a separate test runtime.
OS DNS tests observe real `GETADDRINFOREQWRAP` resources, using default Node lookup, ordinary caller Agents, explicit
lookups delegating to `dns.lookup`, and Agent-owned delegated lookups. Each case requires eight actual concurrent native
HTTP requests to reach a loopback response barrier with one- and four-thread libuv pools.

```sh
npm run test:types
npx tsc --noEmit
npx vitest run test/modelHttpTransport.test.ts test/nodeModelHttpTransport.test.ts test/modelHttpDns.test.ts \
  native/test/oldModelHttpRuntime.test.ts
```

The POSIX old-runtime probe requires a C++17 compiler (`CXX` if not available as `c++`) and skips on Windows; it builds
only a tiny test runtime into `build/`, not the native SDK. Its slot-12 trap must never execute. Native environment
termination probes admit more operations than the libuv pool to exercise active and queued worker cleanup.
