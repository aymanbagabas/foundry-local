# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License.
"""Optional adapter tests with real loopback sockets, without the native library."""

from __future__ import annotations

import asyncio
import concurrent.futures
import gc
import gzip
import os
import ssl
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from foundry_local_sdk import HttpHeader, HttpRequest
from foundry_local_sdk.httpx_transport import HttpxModelHttpTransport


@pytest.fixture
def server():
    requests, entered, disconnected = [], threading.Event(), threading.Event()
    encoded = gzip.compress(b"Keep the HTTP content coding intact.", mtime=0)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args):
            pass

        def do_GET(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            requests.append((self.command, self.path, list(self.headers.raw_items()), body))
            if self.path.startswith("/stall"):
                if self.path == "/stall-read":
                    self.send_response(200)
                    self.send_header("Content-Length", "10")
                    self.end_headers()
                    self.wfile.flush()
                entered.set()
                self.connection.settimeout(6)
                try:
                    if self.connection.recv(1) == b"":
                        disconnected.set()
                except ConnectionResetError:
                    disconnected.set()
                except TimeoutError:
                    pass
                self.close_connection = True
                return
            if self.path == "/disconnect":
                self.close_connection = True
                return
            payload = body if self.command == "POST" else b"streamed response"
            status = 200
            if self.path == "/redirect":
                status, payload = 302, b"redirect"
            elif self.path == "/error":
                status, payload = 503, b"HTTP errors are responses"
            elif self.path == "/compressed":
                payload = encoded
            elif self.path == "/challenge":
                status = 401
            self.send_response(status)
            if self.path == "/chunked":
                self.send_header("Transfer-Encoding", "chunked")
            else:
                self.send_header("Content-Length", str(len(payload) + (10 if self.path == "/truncated" else 0)))
            self.send_header("X-Duplicate", "first")
            self.send_header("X-Duplicate", "second")
            self.send_header("X-Bytes", "\xff")
            if status == 302:
                self.send_header("Location", "/target")
            if status == 401:
                self.send_header("WWW-Authenticate", 'Digest realm="example", nonce="nonce", qop="auth"')
            if self.path == "/compressed":
                self.send_header("Content-Encoding", "gzip")
            self.end_headers()
            if self.command != "HEAD":
                if self.path == "/chunked":
                    for offset in range(0, len(payload), 3):
                        chunk = payload[offset : offset + 3]
                        self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                    self.wfile.write(b"0\r\n\r\n")
                else:
                    self.wfile.write(payload)
                self.wfile.flush()
            if self.path == "/truncated":
                self.close_connection = True

        do_POST = do_GET
        do_HEAD = do_GET

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=lambda: httpd.serve_forever(poll_interval=0.02), daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}", requests, entered, disconnected, encoded
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(2)
        assert not thread.is_alive()


def read_all(operation, capacity=3):
    output = bytearray()
    with memoryview(bytearray(capacity)) as buffer:
        while count := operation.readinto(buffer):
            assert 0 < count <= capacity
            output.extend(buffer[:count])
        assert operation.readinto(buffer) == 0
    return bytes(output)


def test_create_operation_does_not_send_and_unsent_close_releases_state(server):
    url, requests, *_ = server
    with HttpxModelHttpTransport() as adapter:
        operation = adapter.create_operation(HttpRequest("GET", url))
        assert not requests
        operation.close()
        operation.close()
        assert not adapter._operations
        with pytest.raises(RuntimeError, match="closed"):
            operation.send()


def test_signed_target_duplicate_headers_binary_post_and_partial_reads_are_preserved(server):
    url, requests, *_ = server
    target = "/signed%2fblob?sig=Ab%2B+%2f%3D&x=&x=1"
    headers = (
        HttpHeader(b"X-Duplicate", b"first"),
        HttpHeader(b"X-Duplicate", b"second"),
        HttpHeader(b"Range", b"bytes=3-9"),
        HttpHeader(b"If-Match", b'"etag"'),
    )
    with HttpxModelHttpTransport() as adapter:
        operation = adapter.create_operation(HttpRequest("POST", url + target, headers, b"\0binary\xff"))
        try:
            response = operation.send()
            assert response.status_code == 200
            assert [h.value for h in response.headers if h.name.lower() == b"x-duplicate"] == [b"first", b"second"]
            assert [h.value for h in response.headers if h.name.lower() == b"x-bytes"] == [b"\xff"]
            assert read_all(operation, capacity=2) == b"\0binary\xff"
        finally:
            operation.close()
    assert len(requests) == 1
    method, path, received, body = requests[0]
    assert method == "POST" and path == target and body == b"\0binary\xff"
    assert [v for k, v in received if k.lower() == "x-duplicate"] == ["first", "second"]
    assert ("Range", "bytes=3-9") in received and ("If-Match", '"etag"') in received


@pytest.mark.parametrize(
    "path,status", [("/redirect", 302), ("/error", 503), ("/compressed", 200), ("/chunked", 200), ("/", 200)]
)
def test_http_errors_redirects_and_compressed_responses_do_not_trigger_policy(server, path, status):
    url, requests, _, _, encoded = server
    with HttpxModelHttpTransport() as adapter:
        operation = adapter.create_operation(HttpRequest("GET", url + path))
        try:
            assert operation.send().status_code == status
            body = read_all(operation)
            if path == "/compressed":
                assert body == encoded
            if path == "/error":
                assert body == b"HTTP errors are responses"
            if path in ("/", "/chunked"):
                assert body == b"streamed response"
        finally:
            operation.close()
    assert len(requests) == 1  # Includes no redirect to /target and no retry of 503.


def test_head_has_no_response_body(server):
    url, _, *_ = server
    with HttpxModelHttpTransport() as adapter:
        operation = adapter.create_operation(HttpRequest("HEAD", url))
        try:
            assert operation.send().status_code == 200
            assert read_all(operation) == b""
        finally:
            operation.close()


@pytest.mark.parametrize("phase", ["send", "read"])
@pytest.mark.parametrize("action", ["cancel", "deadline", "close"])
def test_blocked_socket_is_interrupted_and_loop_thread_is_joined(server, phase, action, monkeypatch):
    url, requests, entered, disconnected, _ = server
    adapter = HttpxModelHttpTransport()
    operation = adapter.create_operation(
        HttpRequest(
            "GET",
            url + f"/stall-{phase}",
            timeout_ms=300 if action == "deadline" else 5_000,
        )
    )
    worker = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    reading = threading.Event()
    try:
        if phase == "read":
            operation.send()
            original_read = operation._read

            async def read(capacity):
                reading.set()
                return await original_read(capacity)

            monkeypatch.setattr(operation, "_read", read)
            buffer = memoryview(bytearray(4))
            future = worker.submit(operation.readinto, buffer)
        else:
            future = worker.submit(operation.send)
        assert entered.wait(2)
        if phase == "read":
            assert reading.wait(2)
        if action == "cancel":
            operation.cancel()
            operation.cancel()
        elif action == "close":
            adapter.close()
        expected = TimeoutError if action == "deadline" else concurrent.futures.CancelledError
        with pytest.raises(expected):
            future.result(timeout=2)
        assert disconnected.wait(2), "Cancellation did not close the blocked HTTP socket"
        assert len(requests) == 1
    finally:
        operation.close()
        adapter.close()
        worker.shutdown()
        if phase == "read":
            buffer.release()
    assert not adapter._thread.is_alive()
    assert adapter._loop.is_closed()


def test_cancel_before_send_is_permanent_and_does_not_make_a_request(server):
    url, requests, *_ = server
    with HttpxModelHttpTransport() as adapter:
        operation = adapter.create_operation(HttpRequest("GET", url))
        operation.cancel()
        try:
            with pytest.raises(concurrent.futures.CancelledError):
                operation.send()
        finally:
            operation.close()
    assert not requests


def test_total_deadline_covers_later_reads_and_buffered_chunks(server):
    url, *_ = server
    with HttpxModelHttpTransport() as adapter:
        operation = adapter.create_operation(HttpRequest("GET", url, timeout_ms=150))
        try:
            operation.send()
            with memoryview(bytearray(1)) as buffer:
                assert operation.readinto(buffer) == 1
                time.sleep(0.2)
                with pytest.raises(TimeoutError):
                    operation.readinto(buffer)
        finally:
            operation.close()


@pytest.mark.parametrize("path", ["/truncated", "/disconnect"])
def test_connection_and_stream_failures_are_reported_without_retry(server, path):
    url, requests, *_ = server
    with HttpxModelHttpTransport() as adapter:
        operation = adapter.create_operation(HttpRequest("GET", url + path))
        try:
            with pytest.raises(httpx.HTTPError):
                operation.send()
                read_all(operation)
        finally:
            operation.close()
    assert len(requests) == 1


@pytest.mark.parametrize("target", ["/a/../b?sig=exact", "/not-encoded-é", "/?x=a b", "/#fragment"])
def test_targets_httpx_would_rewrite_are_rejected_before_io(server, target):
    url, requests, *_ = server
    with HttpxModelHttpTransport() as adapter:
        operation = adapter.create_operation(HttpRequest("GET", url + target))
        try:
            with pytest.raises(ValueError):
                operation.send()
        finally:
            operation.close()
    assert not requests


@pytest.mark.parametrize("path", ["/redirect", "/challenge"])
def test_client_factory_settings_are_not_modified_or_merged_into_native_requests(server, path):
    url, requests, *_ = server
    clients = []

    def factory(network):
        client = httpx.AsyncClient(
            transport=network,
            follow_redirects=True,
            auth=httpx.DigestAuth("user", "password"),
            headers={"X-Unwanted": "value"},
            params={"unwanted": "value"},
            cookies={"cookie": "value"},
        )
        clients.append(client)
        return client

    with HttpxModelHttpTransport(client_factory=factory) as adapter:
        operation = adapter.create_operation(HttpRequest("GET", url + path))
        try:
            operation.send()
            read_all(operation)
            client = clients[0]
            assert client.follow_redirects is True
            assert client.auth is not None
            assert client.params["unwanted"] == "value"
            assert client.headers["X-Unwanted"] == "value"
        finally:
            operation.close()
    assert len(requests) == 1
    assert requests[0][1] == path
    received = {name.lower() for name, _ in requests[0][2]}
    assert not received.intersection({"x-unwanted", "authorization", "cookie"})
    assert clients[0].is_closed


def test_explicit_proxy_is_used_even_for_a_different_origin(server):
    url, requests, *_ = server
    with HttpxModelHttpTransport(proxy=url) as adapter:
        operation = adapter.create_operation(HttpRequest("GET", "http://example.invalid/signed?sig=%2b"))
        try:
            assert operation.send().status_code == 200
            read_all(operation)
        finally:
            operation.close()
    assert requests[0][1] == "http://example.invalid/signed?sig=%2b"


def test_tls_proxy_and_limits_options_reach_default_transport_unchanged(monkeypatch):
    options = []
    context = ssl.create_default_context()
    limits = httpx.Limits(max_connections=3)

    def network(**kwargs):
        options.append(kwargs)
        return httpx.MockTransport(lambda request: httpx.Response(200, content=b""))

    monkeypatch.setattr(httpx, "AsyncHTTPTransport", network)
    with HttpxModelHttpTransport(proxy="http://proxy.invalid:8080", verify=context, limits=limits):
        pass
    assert options[0]["verify"] is context
    assert options[0]["limits"] is limits and limits.max_connections == 3
    assert options[0]["proxy"] == "http://proxy.invalid:8080"
    assert options[0]["retries"] == 0
    assert options[0]["http2"] is False and options[0]["trust_env"] is False
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname


def test_custom_transport_with_ignored_network_options_is_rejected():
    with pytest.raises(ValueError, match="custom HTTPX transport"):
        HttpxModelHttpTransport(
            transport=httpx.MockTransport(lambda request: httpx.Response(200)), proxy="http://proxy"
        )


def test_response_cleanup_error_is_rethrown_at_adapter_close():
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"body"

        async def aclose(self):
            raise RuntimeError("response cleanup failed")

    transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=Stream()))
    adapter = HttpxModelHttpTransport(transport=transport)
    operation = adapter.create_operation(HttpRequest("GET", "http://example.invalid"))
    operation.send()
    with pytest.raises(RuntimeError, match="response cleanup failed"):
        operation.close()
    with pytest.raises(ExceptionGroup, match="cleanup failed") as error:
        adapter.close()
    assert "response cleanup failed" in str(error.value.exceptions[0])
    assert not adapter._thread.is_alive()


def test_client_cleanup_error_is_reported_after_loop_is_stopped():
    class Transport(httpx.AsyncBaseTransport):
        async def aclose(self):
            raise RuntimeError("network cleanup failed")

    adapter = HttpxModelHttpTransport(transport=Transport())
    with pytest.raises(ExceptionGroup) as error:
        adapter.close()
    assert "network cleanup failed" in str(error.value.exceptions[0])
    assert not adapter._thread.is_alive()
    adapter.close()


@pytest.mark.parametrize(
    "error", [RuntimeError("factory failed"), SystemExit("factory aborted"), BaseException("aborted")]
)
def test_factory_failure_does_not_leak_the_event_loop_thread(error):
    before = {thread.ident for thread in threading.enumerate()}

    def factory(network):
        raise error

    with pytest.raises(RuntimeError):
        HttpxModelHttpTransport(client_factory=factory)
    assert not [
        thread for thread in threading.enumerate() if thread.ident not in before and thread.name == "foundry-httpx"
    ]


@pytest.mark.parametrize("dependency", ["httpx", "httpcore"])
def test_optional_import_does_not_load_httpx_and_missing_dependency_is_explicit(dependency):
    code = """
import builtins, sys
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name == sys.argv[1]:
        raise ModuleNotFoundError("test missing dependency", name=name)
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
import foundry_local_sdk
assert "foundry_local_sdk.httpx_transport" not in sys.modules
assert "httpx" not in sys.modules
try:
    from foundry_local_sdk.httpx_transport import HttpxModelHttpTransport
    with HttpxModelHttpTransport():
        pass
except ImportError as error:
    if sys.argv[1] == "httpx":
        assert "foundry-local-sdk[httpx]" in str(error)
    else:
        assert isinstance(error, ModuleNotFoundError) and error.name == "httpcore"
else:
    raise AssertionError("Missing optional dependency was not reported")
"""
    environment = dict(os.environ, PYTHONPATH=str(Path(__file__).parents[2] / "src"))
    result = subprocess.run(
        [sys.executable, "-c", code, dependency],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_adapter_loop_rejects_recursive_synchronous_calls_instead_of_deadlocking():
    adapter = HttpxModelHttpTransport()

    async def close_on_loop():
        with pytest.raises(RuntimeError, match="event-loop thread"):
            adapter.close()

    try:
        asyncio.run_coroutine_threadsafe(close_on_loop(), adapter._loop).result(timeout=2)
    finally:
        adapter.close()


def test_cancellation_waits_for_async_io_cleanup_before_the_sync_callback_returns():
    entered, unwinding, allow_cleanup = threading.Event(), threading.Event(), threading.Event()

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            try:
                entered.set()
                await asyncio.Event().wait()
                yield b"unreachable"
            finally:
                unwinding.set()
                while not allow_cleanup.is_set():
                    await asyncio.sleep(0.001)

    adapter = HttpxModelHttpTransport(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=Stream())),
    )
    operation = adapter.create_operation(HttpRequest("GET", "http://example.invalid"))
    worker = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    buffer = memoryview(bytearray(4))
    try:
        operation.send()
        future = worker.submit(operation.readinto, buffer)
        assert entered.wait(2)
        operation.cancel()
        assert unwinding.wait(2)
        assert not future.done(), "Native callback returned before the cancelled coroutine finished cleanup"
        # Repeated cancellation must not interrupt that cleanup a second time.
        operation.cancel()
        allow_cleanup.set()
        with pytest.raises(concurrent.futures.CancelledError):
            future.result(timeout=2)
    finally:
        allow_cleanup.set()
        operation.close()
        adapter.close()
        worker.shutdown()
        buffer.release()


def test_parallel_operations_cancel_independently(server):
    url, _, entered, _, _ = server
    with HttpxModelHttpTransport() as adapter:
        blocked = adapter.create_operation(HttpRequest("GET", url + "/stall-send", timeout_ms=5_000))
        normal = adapter.create_operation(HttpRequest("GET", url))
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as worker:
            future = worker.submit(blocked.send)
            try:
                assert entered.wait(2)
                assert normal.send().status_code == 200
                assert read_all(normal) == b"streamed response"
                blocked.cancel()
                with pytest.raises(concurrent.futures.CancelledError):
                    future.result(timeout=2)
            finally:
                blocked.close()
                normal.close()


def test_adapter_close_cleans_unsent_operations_and_rejects_reuse():
    adapter = HttpxModelHttpTransport()
    operation = adapter.create_operation(HttpRequest("GET", "http://example.invalid"))
    adapter.close()
    assert not adapter._operations
    operation.close()
    with pytest.raises(RuntimeError, match="closed"):
        adapter.create_operation(HttpRequest("GET", "http://example.invalid"))


def test_empty_readonly_and_noncontiguous_read_buffers_are_rejected_without_io():
    with HttpxModelHttpTransport() as adapter:
        operation = adapter.create_operation(HttpRequest("GET", "http://example.invalid"))
        try:
            for value in (memoryview(b"readonly"), memoryview(bytearray()), memoryview(bytearray(4))[::2]):
                with value, pytest.raises(ValueError, match="byte buffer"):
                    operation.readinto(value)
        finally:
            operation.close()


def test_factory_invalid_result_is_reported_without_leaking_loop_thread():
    before = {thread.ident for thread in threading.enumerate()}
    with pytest.raises(TypeError, match="fresh, open"):
        HttpxModelHttpTransport(client_factory=lambda network: None)
    assert not [
        thread for thread in threading.enumerate() if thread.ident not in before and thread.name == "foundry-httpx"
    ]


def test_loop_thread_creation_failure_cleans_up_event_loop(monkeypatch):
    loops = []
    original = asyncio.new_event_loop

    def create_loop():
        loop = original()
        loops.append(loop)
        return loop

    def fail_start(thread):
        raise RuntimeError("Cannot create thread")

    monkeypatch.setattr(asyncio, "new_event_loop", create_loop)
    monkeypatch.setattr(threading.Thread, "start", fail_start)
    with pytest.raises(RuntimeError, match="Cannot create thread"):
        HttpxModelHttpTransport()
    assert loops[0].is_closed()


def test_finalizer_is_a_safety_net_for_an_idle_adapter():
    adapter = HttpxModelHttpTransport()
    thread, loop = adapter._thread, adapter._loop
    del adapter
    gc.collect()
    thread.join(2)
    assert not thread.is_alive()
    assert loop.is_closed()


def test_interrupted_sync_wait_keeps_async_io_drainable_by_operation_close(server, monkeypatch):
    url, _, entered, disconnected, _ = server
    with HttpxModelHttpTransport() as adapter:
        operation = adapter.create_operation(HttpRequest("GET", url + "/stall-send", timeout_ms=5_000))
        original_submit = adapter._submit
        interrupted = False

        def submit(coroutine, **kwargs):
            future = original_submit(coroutine, **kwargs)
            original_result = future.result

            def result(*args, **kwargs):
                nonlocal interrupted
                if not interrupted:
                    interrupted = True
                    assert entered.wait(2)
                    raise KeyboardInterrupt()
                return original_result(*args, **kwargs)

            future.result = result
            return future

        monkeypatch.setattr(adapter, "_submit", submit)
        try:
            with pytest.raises(KeyboardInterrupt):
                operation.send()
            assert operation._future is not None
        finally:
            operation.close()
        assert disconnected.wait(2)
        assert not adapter._operations
