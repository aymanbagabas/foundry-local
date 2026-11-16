# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License.
"""Transport contracts and callback bridge, using a mock runtime (no native library)."""

from __future__ import annotations

import gc
import importlib.util
import runpy
import socket
import sys
import threading
import weakref
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from foundry_local_sdk import Configuration, FoundryLocalException, HttpHeader, HttpRequest, HttpResponseHead

_PATH = Path(__file__).parents[2] / "src/foundry_local_sdk/_native/http_transport.py"
_SPEC = importlib.util.spec_from_file_location("http_transport_isolated", _PATH)
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
_TransportBridge = _MODULE._TransportBridge


@pytest.fixture
def binding(monkeypatch):
    builder = Path(__file__).parents[2] / "src/foundry_local_sdk/_native/build_cffi.py"
    ffi = runpy.run_path(str(builder))["ffi"]
    errors = []

    def status(code, message):
        errors.append((code, message))
        return ffi.cast("flStatusPtr", code)

    runtime = SimpleNamespace(root=SimpleNamespace(Status_Create=status))
    monkeypatch.setitem(
        sys.modules,
        "foundry_local_sdk._native.api",
        SimpleNamespace(
            ffi=ffi,
            _cffi_mod=object(),
            _preloaded_native_deps=[],
            os=__import__("os"),
        ),
    )
    return ffi, runtime, errors


class Operation:
    def __init__(self):
        self.closed = 0
        self.cancelled = 0
        self.sent = 0
        self.count = 2
        self.error = None
        self.view = None

    def send(self):
        self.sent += 1
        if self.error:
            raise self.error
        return HttpResponseHead(403, (HttpHeader(b"x-test", b"\xff"), HttpHeader(b"x-test", b"two")))

    def readinto(self, view):
        self.view = view
        if self.error:
            raise self.error
        view[:2] = b"ok"
        return self.count

    def cancel(self):
        self.cancelled += 1

    def close(self):
        self.closed += 1


def create(binding, operation=None, *, timeout=0):
    ffi, runtime, _errors = binding
    operation = operation or Operation()
    transport = SimpleNamespace(create_operation=lambda request: operation)
    bridge = _TransportBridge(transport, runtime)
    strings = [ffi.new("char[]", s) for s in (b"POST", b"https://example.invalid/a?sig=exact", b"a\0b")]
    data = ffi.new(
        "flHttpRequestData*",
        {
            "version": 1,
            "method": strings[0],
            "url": strings[1],
            "body": ffi.cast("uint8_t*", strings[2]),
            "body_size": 3,
            "timeout_ms": timeout,
        },
    )
    output = ffi.new("void**")
    assert bridge.table.CreateOperation(bridge.handle, data, output) == ffi.NULL
    return bridge, output[0], operation


@pytest.mark.parametrize("version", [2, 3, 4])
def test_configured_transport_rejects_old_table_without_tail_read(version):
    config = Configuration("App", model_http_transport=SimpleNamespace())
    with pytest.raises(FoundryLocalException, match="API v5"):
        config._apply_settings(object(), SimpleNamespace(api_version=version), object())


@pytest.mark.parametrize("version", [2, 3, 4])
def test_unset_transport_does_not_read_new_slot(version):
    runtime = MagicMock(api_version=version)
    runtime.config = SimpleNamespace(SetDefaultLogLevel=MagicMock(), SetAdditionalOptions=MagicMock())
    Configuration("App")._apply_settings(object(), runtime, MagicMock())


@pytest.mark.parametrize("name,value", [(b"", b"x"), (b"a:b", b"x"), (b"x", b"a\0b"), (b"x", b"a\rb"), (b"x", b"a\nb")])
def test_header_rejects_invalid_fields(name, value):
    with pytest.raises(ValueError):
        HttpHeader(name, value)


def test_value_contracts_are_owned_and_immutable():
    headers = [HttpHeader(b"x", b"\xff")]
    body = bytearray(b"abc")
    request = HttpRequest("POST", "https://example.invalid", headers, body)
    headers.clear()
    body.clear()
    assert request.headers == (HttpHeader(b"x", b"\xff"),)
    assert request.body == b"abc"
    with pytest.raises(AttributeError):
        request.url = "other"


@pytest.mark.parametrize("status", [0, 99, 600, True, 200.0])
def test_response_rejects_invalid_status(status):
    with pytest.raises(ValueError):
        HttpResponseHead(status)


def test_create_copies_request_without_sending_and_release_unsent(binding):
    bridge, handle, operation = create(binding)
    state = bridge.operations[handle]
    assert state.request.body == b"a\0b"
    assert state.request.url.endswith("?sig=exact")
    assert operation.sent == 0
    bridge.table.ReleaseOperation(handle)
    assert operation.closed == 1
    assert not bridge.operations


def test_response_headers_survive_gc_and_partial_read_eof(binding):
    ffi, _, errors = binding
    bridge, handle, operation = create(binding)
    response = ffi.new("flHttpResponseData*", {"version": 1})
    assert bridge.table.Send(handle, response) == ffi.NULL
    gc.collect()
    assert response.status_code == 403
    assert [(ffi.string(response.headers[i].name), ffi.string(response.headers[i].value)) for i in range(2)] == [
        (b"x-test", b"\xff"),
        (b"x-test", b"two"),
    ]
    buffer, count = ffi.new("uint8_t[]", 8), ffi.new("size_t*")
    assert bridge.table.Read(handle, buffer, 8, count) == ffi.NULL
    assert count[0] == 2 and bytes(ffi.buffer(buffer, 2)) == b"ok"
    with pytest.raises(ValueError):
        len(operation.view)
    operation.count = 0
    assert bridge.table.Read(handle, buffer, 8, count) == ffi.NULL
    assert count[0] == 0
    bridge.table.ReleaseOperation(handle)
    assert not errors


@pytest.mark.parametrize("count", [-1, 9, True, None, 1.5])
def test_invalid_read_counts_return_network_status_and_zero_output(binding, count):
    ffi, _, errors = binding
    bridge, handle, operation = create(binding)
    operation.count = count
    output = ffi.new("size_t*", 123)
    assert bridge.table.Read(handle, ffi.new("uint8_t[]", 8), 8, output) != ffi.NULL
    assert output[0] == 0 and errors[-1][0] == 6
    bridge.table.ReleaseOperation(handle)


@pytest.mark.parametrize("failure", [ValueError("secret signed-url"), KeyboardInterrupt(), SystemExit()])
@pytest.mark.parametrize("phase", ["create", "send", "read", "cancel", "close"])
def test_base_exceptions_are_contained_and_messages_redacted(binding, failure, phase):
    ffi, _runtime, errors = binding
    bridge, handle, operation = create(binding)
    if phase == "create":

        def fail(request):
            raise failure

        bridge.transport.create_operation = fail
        output = ffi.new("void**", handle)
        method, url = ffi.new("char[]", b"GET"), ffi.new("char[]", b"https://example.invalid")
        data = ffi.new("flHttpRequestData*", {"version": 1, "method": method, "url": url})
        assert bridge.table.CreateOperation(bridge.handle, data, output) != ffi.NULL
        assert output[0] == ffi.NULL
    elif phase == "send":
        operation.error = failure
        assert bridge.table.Send(handle, ffi.new("flHttpResponseData*")) != ffi.NULL
    elif phase == "read":
        operation.error = failure
        assert bridge.table.Read(handle, ffi.new("uint8_t[]", 8), 8, ffi.new("size_t*")) != ffi.NULL
    else:

        def fail():
            raise failure

        setattr(operation, phase, fail)
        if phase == "cancel":
            bridge.table.Cancel(handle)
        else:
            bridge.table.ReleaseOperation(handle)
            assert not bridge.operations
            return
    bridge.table.ReleaseOperation(handle)
    assert all(b"secret" not in msg for _, msg in errors)


def test_native_retain_roots_callbacks_runtime_and_context_until_final_release(binding):
    ffi, _runtime, _ = binding
    bridge, handle, operation = create(binding)
    reference = weakref.ref(bridge)
    context = bridge.table.user_data
    # Copy the function pointers just as native Configuration does.
    table = ffi.new("flHttpTransport*", bridge.table[0])
    table.Retain(context)
    table.Retain(context)
    del bridge
    gc.collect()
    assert reference() is not None
    table.Release(context)
    gc.collect()
    assert reference() is not None
    table.ReleaseOperation(handle)
    table.Release(context)
    gc.collect()
    assert reference() is None
    assert operation.closed == 1


class SocketOperation(Operation):
    """Actual blocking socket I/O, interrupted by shutdown rather than a flag."""

    def __init__(self, phase):
        super().__init__()
        self.reader, self.writer = socket.socketpair()
        self.entered = threading.Event()
        self.phase = phase

    def wait(self):
        self.entered.set()
        self.reader.recv(1)

    def send(self):
        if self.phase == "send":
            self.wait()
        return HttpResponseHead(200)

    def readinto(self, buffer):
        self.wait()
        return 0

    def cancel(self):
        super().cancel()
        self.reader.shutdown(socket.SHUT_RDWR)

    def close(self):
        super().close()
        self.reader.close()
        self.writer.close()


@pytest.mark.parametrize("phase", ["send", "read"])
@pytest.mark.parametrize("deadline", [False, True])
def test_cancel_and_deadline_interrupt_blocked_socket_callback(binding, phase, deadline):
    ffi, _, errors = binding
    bridge, handle, operation = create(binding, SocketOperation(phase), timeout=80 if deadline else 0)
    result = []

    def run():
        status = bridge.table.Send(handle, ffi.new("flHttpResponseData*"))
        if phase == "read" and status == ffi.NULL:
            status = bridge.table.Read(handle, ffi.new("uint8_t[]", 8), 8, ffi.new("size_t*"))
        result.append(status)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        assert operation.entered.wait(2)
        if not deadline:
            bridge.table.Cancel(handle)
        thread.join(2)
        assert not thread.is_alive(), "Blocked socket was not interrupted"
        assert result[0] != ffi.NULL
        assert errors[-1][0] == (6 if deadline else 5)
    finally:
        if thread.is_alive():
            operation.cancel()
            thread.join(2)
        bridge.table.ReleaseOperation(handle)


def test_cancel_before_send_is_sticky(binding):
    ffi, _, errors = binding
    bridge, handle, operation = create(binding)
    bridge.table.Cancel(handle)
    bridge.table.Cancel(handle)
    assert bridge.table.Send(handle, ffi.new("flHttpResponseData*")) != ffi.NULL
    assert errors[-1][0] == 5
    assert operation.sent == 0
    bridge.table.ReleaseOperation(handle)


def test_registration_failure_releases_partial_configuration_and_does_not_root_bridge(binding, monkeypatch):
    ffi, runtime, _ = binding
    released, bridges = [], []
    pointer = ffi.cast("flConfiguration*", 1)

    def create_config(name, output):
        output[0] = pointer
        return ffi.NULL

    def register(config, table):
        assert config == pointer
        assert table.version == 1
        bridges.append(weakref.ref(ffi.from_handle(table.user_data)))
        return ffi.cast("flStatusPtr", 6)

    def check_status(status):
        if status != ffi.NULL:
            raise FoundryLocalException("Registration rejected", error_code=6)

    runtime.api_version = 5
    runtime._get = lambda: runtime
    runtime.check_status = check_status
    runtime.config = SimpleNamespace(
        Create=create_config,
        SetModelHttpTransport=register,
        Configuration_Release=released.append,
    )
    module = sys.modules["foundry_local_sdk._native.api"]
    monkeypatch.setattr(module, "api", runtime, raising=False)
    config = Configuration("App", model_http_transport=SimpleNamespace())
    snapshot = vars(config).copy()
    with pytest.raises(FoundryLocalException, match="Registration rejected"):
        config._build_native()
    gc.collect()
    assert released == [pointer]
    assert bridges[0]() is None
    assert vars(config) == snapshot


def test_distinct_operations_send_concurrently_and_cancel_independently(binding):
    ffi, _, errors = binding
    barrier = threading.Barrier(2)

    class ConcurrentOperation(Operation):
        def send(self):
            barrier.wait(timeout=2)
            return HttpResponseHead(200)

    first, first_handle, _ = create(binding, ConcurrentOperation())
    # Register a second handle on the same transport bridge, as native would.
    method, url = ffi.new("char[]", b"GET"), ffi.new("char[]", b"https://example.invalid")
    data = ffi.new("flHttpRequestData*", {"version": 1, "method": method, "url": url})
    output = ffi.new("void**")
    first.transport.create_operation = lambda request: ConcurrentOperation()
    assert first.table.CreateOperation(first.handle, data, output) == ffi.NULL
    other_handle = output[0]
    results = []
    workers = [
        threading.Thread(
            target=lambda handle=handle: results.append(first.table.Send(handle, ffi.new("flHttpResponseData*"))),
            daemon=True,
        )
        for handle in (first_handle, other_handle)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(3)
        assert not worker.is_alive()
    assert results == [ffi.NULL, ffi.NULL]
    first.table.Cancel(first_handle)
    assert not first.operations[other_handle].cancelled.is_set()
    for handle in (first_handle, other_handle):
        first.table.ReleaseOperation(handle)
    assert not errors


def test_deadline_is_total_budget_across_reads_not_restarted(binding):
    ffi, _, errors = binding
    bridge, handle, _operation = create(binding, timeout=100)
    assert bridge.table.Send(handle, ffi.new("flHttpResponseData*")) == ffi.NULL
    output = ffi.new("size_t*")
    assert bridge.table.Read(handle, ffi.new("uint8_t[]", 8), 8, output) == ffi.NULL
    # Wait for the actual cancellation signal, rather than a timing-sensitive sleep.
    state = bridge.operations[handle]
    assert state.expired.wait(2)
    assert bridge.table.Read(handle, ffi.new("uint8_t[]", 8), 8, output) != ffi.NULL
    assert output[0] == 0 and errors[-1][0] == 6
    bridge.table.ReleaseOperation(handle)


def test_maximum_native_timeout_does_not_overflow_thread_wait(binding):
    ffi, _, errors = binding
    bridge, handle, _operation = create(binding, timeout=2**64 - 1)
    assert bridge.table.Send(handle, ffi.new("flHttpResponseData*")) == ffi.NULL
    state = bridge.operations[handle]
    bridge.table.ReleaseOperation(handle)
    assert not state.timer.is_alive()
    assert not errors


def test_deadline_worker_start_failure_still_closes_operation(binding, monkeypatch):
    ffi, _, errors = binding
    bridge, handle, operation = create(binding, timeout=100)

    def fail_start(thread):
        raise RuntimeError("Cannot create worker")

    monkeypatch.setattr(threading.Thread, "start", fail_start)
    assert bridge.table.Send(handle, ffi.new("flHttpResponseData*")) != ffi.NULL
    bridge.table.ReleaseOperation(handle)
    assert operation.closed == 1
    assert not bridge.operations
    assert errors[-1][0] == 6


def test_create_preserves_duplicate_and_non_utf8_request_headers(binding):
    ffi, _, _ = binding
    bridge, handle, _ = create(binding)
    method, url = ffi.new("char[]", b"HEAD"), ffi.new("char[]", b"https://example.invalid/?sig=%2B")
    strings = [ffi.new("char[]", value) for value in (b"x-field", b"\xff", b"x-field", b"second")]
    headers = ffi.new(
        "flHttpHeader[]",
        [
            {"name": strings[0], "value": strings[1]},
            {"name": strings[2], "value": strings[3]},
        ],
    )
    request = ffi.new(
        "flHttpRequestData*",
        {
            "version": 1,
            "method": method,
            "url": url,
            "headers": headers,
            "header_count": 2,
        },
    )
    output = ffi.new("void**")
    assert bridge.table.CreateOperation(bridge.handle, request, output) == ffi.NULL
    copied = bridge.operations[output[0]].request
    assert copied.headers == (HttpHeader(b"x-field", b"\xff"), HttpHeader(b"x-field", b"second"))
    assert copied.url == "https://example.invalid/?sig=%2B"
    assert copied.method == "HEAD"
    bridge.table.ReleaseOperation(output[0])
    bridge.table.ReleaseOperation(handle)
