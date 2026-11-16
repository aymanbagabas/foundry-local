# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License.
"""Optional HTTPX implementation of the neutral model HTTP transport protocols."""

from __future__ import annotations

import asyncio
import concurrent.futures
import ssl
import threading
import time
import warnings
from collections.abc import AsyncIterator, Callable, Coroutine
from typing import Any, Self, TypeVar
from urllib.parse import urlsplit

try:
    import httpx
except ModuleNotFoundError as exc:
    if exc.name != "httpx":
        raise
    raise ImportError("HTTPX transport requires: pip install 'foundry-local-sdk[httpx]'") from exc

from foundry_local_sdk.model_http_transport import HttpHeader, HttpRequest, HttpResponseHead, ModelHttpOperation

_T = TypeVar("_T")


class HttpxModelHttpTransport:
    """Streaming HTTPX adapter with an owned AsyncClient and event-loop thread.

    Close managers before closing this adapter. A supplied HTTPX transport and
    the client returned by ``client_factory`` transfer ownership to the adapter.
    The factory runs on its loop and must use the supplied transport. Custom
    transports/hooks must not retry, redirect, decode, or suppress cancellation.
    """

    def __init__(
        self,
        *,
        proxy: str | httpx.Proxy | None = None,
        verify: ssl.SSLContext | bool | str = True,
        limits: httpx.Limits | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        client_factory: Callable[[httpx.AsyncBaseTransport], httpx.AsyncClient] | None = None,
    ) -> None:
        if transport is not None and (proxy is not None or verify is not True or limits is not None):
            raise ValueError("Configure proxy, TLS and limits on the custom HTTPX transport itself")
        self._lock = threading.Lock()
        self._closed = False
        self._stopped = threading.Event()
        self._operations: set[_HttpxOperation] = set()
        self._cleanup_errors: list[Exception] = []
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, args=(self._loop,), name="foundry-httpx", daemon=True)
        try:
            self._thread.start()
        except BaseException:
            self._loop.close()
            self._closed = True
            self._stopped.set()
            raise

        async def open_client() -> httpx.AsyncClient:
            network = (
                transport
                if transport is not None
                else httpx.AsyncHTTPTransport(
                    proxy=proxy,
                    verify=verify,
                    trust_env=False,
                    limits=limits if limits is not None else httpx.Limits(),
                    retries=0,
                    http1=True,
                    http2=False,
                )
            )
            try:
                client = (
                    client_factory(network)
                    if client_factory is not None
                    else httpx.AsyncClient(transport=network, follow_redirects=False, timeout=None, trust_env=False)
                )
                if not isinstance(client, httpx.AsyncClient) or client.is_closed:
                    raise TypeError("client_factory must return a fresh, open httpx.AsyncClient")
                return client
            except BaseException:
                await network.aclose()
                raise

        try:
            self._client = self._submit(open_client()).result()
        except BaseException:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join()
            self._closed = True
            self._stopped.set()
            raise

    @staticmethod
    def _run(loop: asyncio.AbstractEventLoop) -> None:
        asyncio.set_event_loop(loop)
        try:
            loop.run_forever()
        finally:
            loop.close()

    def _check_thread(self) -> None:
        if threading.current_thread() is self._thread:
            raise RuntimeError("Synchronous HTTPX adapter calls cannot run on its event-loop thread")

    def _submit(
        self,
        coroutine: Coroutine[Any, Any, _T],
        *,
        closing: bool = False,
    ) -> concurrent.futures.Future[_T]:
        try:
            self._check_thread()
            with self._lock:
                if self._closed and not closing:
                    raise RuntimeError("HTTPX transport is closed")
                return asyncio.run_coroutine_threadsafe(self._guard(coroutine), self._loop)
        except BaseException:
            coroutine.close()
            raise

    @staticmethod
    async def _guard(coroutine: Coroutine[Any, Any, _T]) -> _T:
        try:
            return await coroutine
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            if isinstance(exc, Exception):
                raise
            # asyncio otherwise lets these terminate its loop, stranding native waiters.
            raise RuntimeError("HTTPX application callback aborted") from exc

    def create_operation(self, request: HttpRequest) -> ModelHttpOperation:
        """Allocate one operation without scheduling tasks or performing network I/O."""
        with self._lock:
            if self._closed:
                raise RuntimeError("HTTPX transport is closed")
            operation = _HttpxOperation(self, request)
            self._operations.add(operation)
            return operation

    def _discard(self, operation: _HttpxOperation, error: Exception | None) -> None:
        with self._lock:
            self._operations.discard(operation)
            if error is not None:
                # The native ReleaseOperation callback has no error channel.
                # Surface cleanup failures at the application's adapter.close().
                self._cleanup_errors.append(error)

    def close(self) -> None:
        """Cancel/drain operations, close the client, and join the owned loop.

        Cleanup errors, including errors contained by native release callbacks,
        are reported as an ExceptionGroup after all cleanup has been attempted.
        """
        self._check_thread()
        with self._lock:
            already_closed = self._closed
            self._closed = True
            operations = tuple(self._operations)
        if already_closed:
            self._stopped.wait()
            return
        try:
            for operation in operations:
                operation.cancel()
            for operation in operations:
                try:
                    operation.close()
                except Exception:  # noqa: BLE001, S112 -- recorded by operation.close and reported below
                    continue
            try:
                self._submit(self._client.aclose(), closing=True).result()
            except Exception as exc:  # noqa: BLE001 -- finish shutdown before reporting failures
                self._cleanup_errors.append(exc)
            try:
                self._submit(self._loop.shutdown_asyncgens(), closing=True).result()
                self._submit(self._loop.shutdown_default_executor(), closing=True).result()
            except Exception as exc:  # noqa: BLE001 -- finish shutdown before reporting failures
                self._cleanup_errors.append(exc)
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join()
            self._stopped.set()
        if self._cleanup_errors:
            raise ExceptionGroup("HTTPX transport cleanup failed", self._cleanup_errors)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def __del__(self) -> None:
        if hasattr(self, "_thread") and not self._stopped.is_set():
            try:
                self.close()
            except Exception:  # noqa: BLE001 -- finalizer is only a safety net
                warnings.warn("Unclosed HTTPX transport could not be cleaned up", ResourceWarning, stacklevel=2)


class _HttpxOperation:
    def __init__(self, owner: HttpxModelHttpTransport, request: HttpRequest) -> None:
        self._owner = owner
        self._request = request
        self._lock = threading.Lock()
        self._cancelled = False
        self._closed = False
        self._closed_event = threading.Event()
        self._future: concurrent.futures.Future[Any] | None = None
        self._task: asyncio.Task[Any] | None = None
        self._deadline: float | None = None
        self._sent = False
        self._response: httpx.Response | None = None
        self._chunks: AsyncIterator[bytes] | None = None
        self._pending = b""
        self._offset = 0
        self._eof = False

    async def _perform(self, function: Callable[[], Coroutine[Any, Any, _T]]) -> _T:
        with self._lock:
            if self._closed or self._cancelled:
                raise asyncio.CancelledError()
            self._task = asyncio.current_task()
        try:
            remaining = None if self._deadline is None else max(0, self._deadline - time.monotonic())
            if remaining is not None and remaining <= 0:
                raise TimeoutError("Model HTTP operation exceeded its total deadline")
            async with asyncio.timeout(remaining):
                result = await function()
            if self._deadline is not None and time.monotonic() >= self._deadline:
                raise TimeoutError("Model HTTP operation exceeded its total deadline")
            return result
        finally:
            with self._lock:
                self._task = None

    def _call(self, function: Callable[[], Coroutine[Any, Any, _T]], *, send: bool = False) -> _T:
        with self._lock:
            if self._closed:
                raise RuntimeError("HTTPX operation is closed")
            if self._cancelled:
                raise concurrent.futures.CancelledError()
            if self._future is not None:
                raise RuntimeError("Send and readinto must be serialized per operation")
            if send:
                if self._sent:
                    raise RuntimeError("HTTPX operations can only send once")
                self._sent = True
                if self._request.timeout_ms:
                    self._deadline = time.monotonic() + self._request.timeout_ms / 1000
            future = self._owner._submit(self._perform(function))
            self._future = future
        try:
            # Cancel the asyncio task, NOT this Future: cancellation must finish
            # unwinding socket I/O before a native callback can return/release.
            return future.result()
        finally:
            with self._lock:
                # KeyboardInterrupt can interrupt the Python waiter without
                # finishing its asyncio task. Keep that task drainable by close.
                if future.done():
                    self._future = None

    async def _send(self) -> HttpResponseHead:
        url = httpx.URL(self._request.url)
        parts = urlsplit(self._request.url)
        if url.scheme not in ("http", "https") or not url.host or parts.fragment or parts.username is not None:
            raise ValueError("HTTPX transport requires an absolute HTTP(S) URL without userinfo or a fragment")
        target = (parts.path or "/") + ("?" + parts.query if "?" in self._request.url else "")
        try:
            expected = target.encode("ascii")
        except UnicodeEncodeError as exc:
            raise ValueError("HTTPX request targets must be percent-encoded ASCII") from exc
        if url.raw_path != expected:
            raise ValueError("HTTPX would rewrite the request target")
        # A standalone Request avoids merging client cookies, default headers or
        # query parameters into a native (potentially signed) request.
        request = httpx.Request(
            self._request.method,
            url,
            headers=[(header.name, header.value) for header in self._request.headers],
            content=self._request.body,
            extensions={"timeout": {"connect": None, "read": None, "write": None, "pool": None}},
        )
        self._response = await self._owner._client.send(request, stream=True, auth=None, follow_redirects=False)
        self._chunks = self._response.aiter_raw()
        return HttpResponseHead(
            self._response.status_code,
            tuple(HttpHeader(name, value) for name, value in self._response.headers.raw),
        )

    def send(self) -> HttpResponseHead:
        return self._call(self._send, send=True)

    async def _read(self, capacity: int) -> bytes:
        if self._chunks is None:
            raise RuntimeError("Read requires a successful send")
        while self._offset == len(self._pending) and not self._eof:
            try:
                self._pending = await anext(self._chunks)
                self._offset = 0
            except StopAsyncIteration:
                self._eof = True
        chunk = self._pending[self._offset : self._offset + capacity]
        self._offset += len(chunk)
        return chunk

    def readinto(self, buffer: memoryview) -> int:
        if not len(buffer) or buffer.readonly or buffer.ndim != 1 or buffer.itemsize != 1 or not buffer.c_contiguous:
            raise ValueError("readinto requires a nonempty, contiguous, writable byte buffer")
        capacity = len(buffer)
        chunk = self._call(lambda: self._read(capacity))
        # Only the synchronous caller touches the borrowed native buffer.
        with buffer.cast("B") as writable:
            writable[: len(chunk)] = chunk
        return len(chunk)

    def cancel(self) -> None:
        with self._lock:
            if self._cancelled or self._closed:
                return
            self._cancelled = True
            if self._task is not None:
                self._owner._loop.call_soon_threadsafe(self._task.cancel)

    async def _close_response(self) -> None:
        if self._response is not None:
            await self._response.aclose()
        self._pending = b""
        self._offset = 0

    def close(self) -> None:
        self._owner._check_thread()
        self.cancel()
        with self._lock:
            already_closed = self._closed
            self._closed = True
            future = self._future
        if already_closed:
            self._closed_event.wait()
            return
        error = None
        try:
            try:
                if future is not None:
                    try:
                        future.result()
                    except concurrent.futures.CancelledError:
                        pass
            finally:
                self._owner._submit(self._close_response(), closing=True).result()
        except Exception as exc:
            error = exc
            raise
        finally:
            self._owner._discard(self, error)
            self._closed_event.set()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
