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
