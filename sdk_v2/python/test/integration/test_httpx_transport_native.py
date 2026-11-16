# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License.
"""Real SDK Configuration registration with the optional HTTPX adapter and loopback HTTP."""

from __future__ import annotations

import runpy
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from foundry_local_sdk import Configuration, FoundryLocalManager
from foundry_local_sdk._native.api import api
from foundry_local_sdk.httpx_transport import HttpxModelHttpTransport


@pytest.mark.parametrize("shared_host", [False, True])
def test_native_registered_httpx_adapter_queries_real_loopback_catalog(shared_host, capsys):
    # Fail rather than skip if the requested optional adapter/native runtime is absent.
    assert api.api_version >= 5
    example = runpy.run_path(str(Path(__file__).parents[2] / "examples/httpx_model_http_transport.py"))
    example["main"](use_shared_host=shared_host)
    assert "Native HTTPX transport: 1 loopback catalog requests, 0 models, adapter closed" in capsys.readouterr().out


def test_native_registered_httpx_deadline_interrupts_a_blocked_http_body(tmp_path):
    entered, disconnected = threading.Event(), threading.Event()
    requests = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            requests.append(self.path)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "100")
            self.end_headers()
            self.wfile.flush()
            entered.set()
            self.connection.settimeout(40)
            try:
                if self.connection.recv(1) == b"":
                    disconnected.set()
            except ConnectionResetError:
                disconnected.set()
            except TimeoutError:
                pass
            self.close_connection = True

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.02), daemon=True)
    thread.start()
    try:
        # Use the real native 30-second budget: native cancellation monitor,
        # cffi bridge deadline and HTTPX's async timeout all share this lifetime.
        with HttpxModelHttpTransport() as adapter:
            config = Configuration(
                "NativeHttpxDeadline",
                app_data_dir=str(tmp_path),
                model_cache_dir=str(tmp_path / "models"),
                logs_dir=str(tmp_path / "logs"),
                catalog_urls=[(f"http://127.0.0.1:{server.server_port}/asset-gallery/v1.0/models", None)],
                model_http_transport=adapter,
                disable_nonessential_telemetry=True,
            )
            with FoundryLocalManager(config) as manager:
                assert manager.catalog.list_models() == []
            assert entered.is_set() and disconnected.wait(2)
            assert requests == ["/asset-gallery/v1.0/models"]
            assert not adapter._operations
            assert not adapter._client.is_closed
        assert not adapter._thread.is_alive()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)
        assert not thread.is_alive()
