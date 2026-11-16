# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License.
"""cffi callback ownership for the API v5 model HTTP extension."""

from __future__ import annotations

import ctypes
import importlib
import threading
import time
from collections.abc import Callable
from typing import Any

from foundry_local_sdk.model_http_transport import HttpHeader, HttpRequest, HttpResponseHead, ModelHttpTransport


class _Stopped(Exception):
    pass


class _Operation:
    def __init__(self, bridge: _TransportBridge, request: HttpRequest, operation: Any) -> None:
        self.bridge = bridge
        self.request = request
        self.operation = operation
        self.cancelled = threading.Event()
        self.expired = threading.Event()
        self.deadline: float | None = None
        self.timer: threading.Thread | None = None
        self.finished = threading.Event()
        self.storage: list[Any] = []
        self.handle = bridge.ffi.new_handle(self)

    def check(self) -> None:
        if self.deadline is not None and time.monotonic() >= self.deadline:
            self.expired.set()
        if self.cancelled.is_set() or self.expired.is_set():
            raise _Stopped()

    def cancel(self) -> None:
        self.cancelled.set()
        self.operation.cancel()

    def expire(self) -> None:
        self.expired.set()
        try:
            self.operation.cancel()
        except BaseException:  # noqa: BLE001, S110 -- foreign boundary must contain BaseException, without leaking secrets
            # Even SystemExit/KeyboardInterrupt must not escape a callback or timer.
            pass

    def watch_deadline(self) -> None:
        assert self.deadline is not None
        while True:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                self.expire()
                return
            # Native budgets are uint64; Event.wait cannot accept their full range.
            if self.finished.wait(min(remaining, threading.TIMEOUT_MAX)):
                return

    def send(self, response: Any) -> None:
        self.check()
        if self.request.timeout_ms:
            seconds = self.request.timeout_ms / 1000
            self.deadline = time.monotonic() + seconds
            timer = threading.Thread(target=self.watch_deadline, daemon=True)
            timer.start()
            self.timer = timer
        head = self.operation.send()
        self.check()
        if not isinstance(head, HttpResponseHead):
            raise TypeError("send must return HttpResponseHead")
        ffi = self.bridge.ffi
        headers = ffi.new("flHttpHeader[]", len(head.headers)) if head.headers else ffi.NULL
        self.storage.append(headers)
        for i, header in enumerate(head.headers):
            name = ffi.new("char[]", header.name)
            value = ffi.new("char[]", header.value)
            self.storage.extend((name, value))
            headers[i].name = name
            headers[i].value = value
        response.status_code = head.status_code
        response.headers = headers
        response.header_count = len(head.headers)

    def read(self, buffer: Any, capacity: int, output: Any) -> None:
        self.check()
        view = memoryview(self.bridge.ffi.buffer(buffer, capacity))
        try:
            count = self.operation.readinto(view)
        finally:
            view.release()
        self.check()
        if type(count) is not int or not 0 <= count <= capacity:
            raise ValueError("readinto returned an invalid byte count")
        output[0] = count

    def close(self) -> None:
        try:
            if self.timer is not None:
                self.finished.set()
                # A firing deadline must finish interrupting I/O before close.
                self.timer.join()
        finally:
            self.operation.close()


class _TransportBridge:
    def __init__(self, transport: ModelHttpTransport, runtime: Any) -> None:
        api_module = importlib.import_module("foundry_local_sdk._native.api")
        self.ffi = api_module.ffi
        self.runtime = runtime
        # Retain the actual extension and load-time dependencies, not just a lazy proxy.
        self.native_lifetime = (api_module._cffi_mod, *api_module._preloaded_native_deps)
        self.transport = transport
        self.operations: dict[Any, _Operation] = {}
        self.lock = threading.Lock()
        self.handle = self.ffi.new_handle(self)
        self.callbacks: list[Any] = []
        # ffi.new_handle is NOT a GC root. Each native Retain owns a CPython
        # reference, balanced by Release, independent of Python owner scopes or cycles.
        # The SDK's API-mode extension already targets CPython's stable ABI.
        self._incref = ctypes.PYFUNCTYPE(None, ctypes.py_object)(("Py_IncRef", ctypes.pythonapi))
        self._decref = ctypes.PYFUNCTYPE(None, ctypes.py_object)(("Py_DecRef", ctypes.pythonapi))
        self.table = self.ffi.new("flHttpTransport*", {"version": 1, "user_data": self.handle})
        signatures = {
            "Retain": "void(void*)",
            "Release": "void(void*)",
            "CreateOperation": "flStatusPtr(void*, const flHttpRequestData*, void**)",
            "Send": "flStatusPtr(void*, flHttpResponseData*)",
            "Read": "flStatusPtr(void*, uint8_t*, size_t, size_t*)",
            "Cancel": "void(void*)",
            "ReleaseOperation": "void(void*)",
        }
        for name, signature in signatures.items():
            # Match FL_API_CALL on 32-bit Windows as well as the unified 64-bit ABI.
            if api_module.os.name == "nt":
                signature = signature.replace("(", " __stdcall(", 1)
            callback = self.ffi.callback(signature, self._wrap(name))
            self.callbacks.append(callback)
            setattr(self.table, name, callback)

    def _status(self, operation: _Operation | None = None) -> Any:
        if operation is not None and operation.cancelled.is_set():
            return self.runtime.root.Status_Create(5, b"Model HTTP operation cancelled")
        # Never stringify a foreign exception: it may contain credentials or throw itself.
        return self.runtime.root.Status_Create(6, b"Model HTTP transport failed or exceeded its deadline")

    def _wrap(self, name: str) -> Callable[..., Any]:
        def callback(*args: Any) -> Any:
            operation = None
            try:
                if name == "Retain":
                    self._incref(self)
                elif name == "Release":
                    self._decref(self)
                elif name == "CreateOperation":
                    args[2][0] = self.ffi.NULL
                    self._create(args[1], args[2])
                else:
                    operation = self.ffi.from_handle(args[0])
                    match name:
                        case "Send":
                            operation.send(args[1])
                        case "Read":
                            args[3][0] = 0
                            operation.read(*args[1:])
                        case "Cancel":
                            operation.cancel()
                        case "ReleaseOperation":
                            try:
                                operation.close()
                            finally:
                                with self.lock:
                                    del self.operations[args[0]]
                if name in ("CreateOperation", "Send", "Read"):
                    return self.ffi.NULL
            except BaseException:  # noqa: BLE001 -- no foreign exception may cross a C callback
                if name in ("CreateOperation", "Send", "Read"):
                    return self._status(operation)
                # Lifecycle/cancel callbacks have no error channel.
            return None

        return callback

    def _create(self, data: Any, output: Any) -> None:
        ffi = self.ffi
        if data.version != 1:
            raise ValueError("Unsupported HTTP structure version")
        request = HttpRequest(
            method=ffi.string(data.method).decode("ascii"),
            url=ffi.string(data.url).decode("utf-8"),
            headers=tuple(
                HttpHeader(ffi.string(data.headers[i].name), ffi.string(data.headers[i].value))
                for i in range(data.header_count)
            ),
            body=bytes(ffi.buffer(data.body, data.body_size)) if data.body_size else b"",
            timeout_ms=int(data.timeout_ms),
        )
        implementation = self.transport.create_operation(request)
        try:
            if not all(
                callable(getattr(implementation, name, None)) for name in ("send", "readinto", "cancel", "close")
            ):
                raise TypeError("Invalid model HTTP operation")
            operation = _Operation(self, request, implementation)
            with self.lock:
                self.operations[operation.handle] = operation
            output[0] = operation.handle
        except BaseException:
            try:
                implementation.close()
            except BaseException:  # noqa: BLE001, S110 -- preserve failure without exposing foreign exception text
                pass
            raise
