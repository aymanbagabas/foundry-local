# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License.
"""Application-owned, synchronous streaming HTTP for model acquisition.

Operations run on native workers. Implementations must release the GIL while
blocking, and ``cancel`` must interrupt blocked I/O without taking its lock.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class HttpHeader:
    """One HTTP field; bytes preserve non-UTF-8 values and duplicate fields."""

    name: bytes
    value: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.name, bytes) or not isinstance(self.value, bytes):
            raise TypeError("HTTP fields must be bytes")
        tokens = b"!#$%&'*+-.^_`|~0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
        if not self.name or any(c not in tokens for c in self.name):
            raise ValueError("Invalid HTTP field name")
        if any(c in self.value for c in (b"\0", b"\r", b"\n")):
            raise ValueError("Invalid HTTP field value")


@dataclass(frozen=True)
class HttpRequest:
    """Owned request snapshot. Zero timeout means no deadline, otherwise total milliseconds."""

    method: str
    url: str
    headers: tuple[HttpHeader, ...] = ()
    body: bytes = b""
    timeout_ms: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "headers", tuple(self.headers))
        object.__setattr__(self, "body", bytes(self.body))


@dataclass(frozen=True)
class HttpResponseHead:
    """HTTP errors (including 3xx, 4xx, 5xx) are successful transport responses."""

    status_code: int
    headers: tuple[HttpHeader, ...] = ()

    def __post_init__(self) -> None:
        if type(self.status_code) is not int or not 100 <= self.status_code <= 599:
            raise ValueError("HTTP status must be an integer from 100 through 599")
        object.__setattr__(self, "headers", tuple(self.headers))
        if any(not isinstance(header, HttpHeader) for header in self.headers):
            raise TypeError("Response headers must be HttpHeader instances")


class ModelHttpOperation(Protocol):
    """One request, closed exactly once by the SDK, even if never sent.

    Do not retry, redirect, or decompress. ``readinto`` may return a short read;
    zero is permanent EOF. Never retain the writable view beyond the call.
    ``cancel`` must be prompt, thread-safe, idempotent and interrupt blocked I/O.
    """

    def send(self) -> HttpResponseHead: ...

    def readinto(self, buffer: memoryview) -> int: ...

    def cancel(self) -> None: ...

    def close(self) -> None: ...


class ModelHttpTransport(Protocol):
    """Concurrent operation factory. Allocate only; do no network I/O here."""

    def create_operation(self, request: HttpRequest) -> ModelHttpOperation: ...
