# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License.
"""Register the optional HTTPX adapter with a real native SDK and loopback HTTP.

    pip install 'foundry-local-sdk[httpx]'
    python examples/httpx_model_http_transport.py

No external catalog, model weights, or credentials are required.
"""

from __future__ import annotations

import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from foundry_local_sdk import Configuration, FoundryLocalManager
from foundry_local_sdk.httpx_transport import HttpxModelHttpTransport


def main(*, use_shared_host: bool = False) -> None:
    requests: list[tuple[str, bytes]] = []

    class CatalogHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args: object) -> None:
            pass

        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            requests.append((self.path, body))
            payload = b'{"value":[]}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("X-Example", "first")
            self.send_header("X-Example", "second")
            self.end_headers()
            for offset in range(0, len(payload), 3):
                self.wfile.write(payload[offset : offset + 3])
                self.wfile.flush()

    server = ThreadingHTTPServer(("127.0.0.1", 0), CatalogHandler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.02), daemon=True)
    thread.start()
    try:
        # The adapter owns its AsyncClient/loop. Close the manager first so all
        # native-retained operations finish before stopping that loop.
        with HttpxModelHttpTransport() as transport, tempfile.TemporaryDirectory(prefix="foundry-httpx-") as data:
            config = Configuration(
                app_name="PythonHttpxModelHttpTransport",
                app_data_dir=data,
                model_cache_dir=f"{data}/models",
                logs_dir=f"{data}/logs",
                catalog_urls=[(f"http://127.0.0.1:{server.server_port}/asset-gallery/v1.0/models", None)],
                model_http_transport=transport,
                disable_nonessential_telemetry=True,
                use_shared_host=use_shared_host,
            )
            with FoundryLocalManager(config) as manager:
                assert manager.catalog.list_models() == []
            assert requests and all(path == "/asset-gallery/v1.0/models" and body for path, body in requests)
        print(f"Native HTTPX transport: {len(requests)} loopback catalog requests, 0 models, adapter closed")
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


if __name__ == "__main__":
    main()
