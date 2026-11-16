# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License.
"""Register an application-owned catalog with the real native SDK; no network or weights.

Run with the native runtime available:
    python examples/custom_model_http_transport.py
"""

from __future__ import annotations

import tempfile
import threading

from foundry_local_sdk import (
    Configuration,
    FoundryLocalManager,
    HttpHeader,
    HttpRequest,
    HttpResponseHead,
    ModelHttpOperation,
)

CATALOG_URL = "https://example.invalid/asset-gallery/v1.0/models"


class InMemoryTransport:
    """Deterministic example only, not a network adapter."""

    def __init__(self) -> None:
        self.requests: list[HttpRequest] = []
        self.reads = 0
        self.closed = 0

    def create_operation(self, request: HttpRequest) -> ModelHttpOperation:
        if request.method != "POST" or request.url != CATALOG_URL:
            raise ValueError("Unexpected in-memory request")
        self.requests.append(request)
        return _CatalogOperation(self)


class _CatalogOperation:
    def __init__(self, owner: InMemoryTransport) -> None:
        self.owner = owner
        self.offset = 0
        self.cancelled = threading.Event()

    def send(self) -> HttpResponseHead:
        return HttpResponseHead(
            200,
            (
                HttpHeader(b"content-type", b"application/json"),
                HttpHeader(b"x-example", b"first"),
                HttpHeader(b"x-example", b"second"),
            ),
        )

    def readinto(self, buffer: memoryview) -> int:
        if self.cancelled.is_set():
            raise OSError("Operation cancelled")
        body = b'{"value":[]}'
        count = min(len(buffer), 3, len(body) - self.offset)
        buffer[:count] = body[self.offset : self.offset + count]
        self.offset += count
        self.owner.reads += 1
        return count

    def cancel(self) -> None:
        # No I/O is pending here. Network adapters must actively interrupt it.
        self.cancelled.set()

    def close(self) -> None:
        self.owner.closed += 1


def main() -> None:
    transport = InMemoryTransport()
    with tempfile.TemporaryDirectory(prefix="foundry-python-http-") as data:
        config = Configuration(
            app_name="PythonCustomModelHttpTransport",
            app_data_dir=data,
            model_cache_dir=f"{data}/models",
            logs_dir=f"{data}/logs",
            catalog_urls=[(CATALOG_URL, None)],
            model_http_transport=transport,
            disable_nonessential_telemetry=True,
        )
        with FoundryLocalManager(config) as manager:
            assert manager.catalog.list_models() == []
        assert transport.requests
        assert transport.reads > len(transport.requests)
        assert transport.closed == len(transport.requests)
        print(
            f"Native transport: {len(transport.requests)} catalog requests, "
            f"{transport.reads} partial reads, 0 models, balanced operation ownership"
        )


if __name__ == "__main__":
    main()
