# Python model HTTP binding conventions

- Keep the API-mode cffi declarations aligned with `foundry_local_c.h`.
  `SetModelHttpTransport` is configuration slot 12; read it only after API >= 5.
  HTTP version 1 and existing payload version 2 are independent of API version 5.
- `ffi.new_handle()` does not root its own Python object. Native Retain/Release
  balance CPython references to the bridge, which retains callbacks, handles,
  the concrete runtime, and its load-time dependencies. A self-cycle alone is unsafe.
- Retain request snapshots and response header storage through ReleaseOperation.
  Catch BaseException at callback boundaries; do not stringify foreign errors.
- Cancellation/deadline tests must interrupt real blocked I/O; flags alone do not
  prove cancellation. No general requests adapter is shipped.
- Standalone cffi builds use `python/build/cffi` and copy only the extension into
  `_native`. cffi custom `target` paths can interact badly with setuptools'
  stable-ABI filename rewriting; use its default artifact name.
- The native catalog currently calls CreateOperation on the calling thread;
  cancellation monitors can reenter Python from native threads.
- The optional `httpx_transport` module alone imports HTTPX. Its owned AsyncClient
  runs on a private loop thread; cancel its asyncio Task, not the concurrent
  Future, so native callbacks wait for actual I/O unwinding before returning.
- Use standalone HTTPX Requests with raw header arrays and `aiter_raw()`.
  Disable redirects/auth replay per send, retries on the network transport,
  and per-I/O timeouts in request extensions (the native total deadline wins).
- HTTPX normalizes literal dot path segments. Reject changed raw targets before
  I/O instead of silently breaking signatures. Encoded paths/query bytes survive.
- Adapter close drains operations before client/loop shutdown. Record operation
  cleanup errors for explicit adapter.close(), because native ReleaseOperation
  has no status return. Custom factories must create owned clients using the
  supplied transport and must not suppress cancellation or introduce replay.
