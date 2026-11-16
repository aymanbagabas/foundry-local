# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License.
"""Real API v5 registration and worker callbacks; no model weights or network.

Import errors are deliberately failures, not skips: this suite validates the
compiled extension and a runtime supplied with FOUNDRY_LOCAL_LIB_DIR.
"""

from __future__ import annotations

import gc
import importlib
import runpy
import socket
import threading
import weakref
from pathlib import Path
from types import SimpleNamespace

import pytest

from foundry_local_sdk import Catalog, CatalogType, Configuration, FoundryLocalException, FoundryLocalManager
from foundry_local_sdk._native.api import api, ffi
from foundry_local_sdk._native.http_transport import _TransportBridge

_EXAMPLE = runpy.run_path(str(Path(__file__).parents[2] / "examples/custom_model_http_transport.py"))
InMemoryTransport = _EXAMPLE["InMemoryTransport"]
CATALOG_URL = _EXAMPLE["CATALOG_URL"]


def configuration(tmp_path, transport, **kwargs):
    return Configuration(
        "PythonNativeHttpTransportTests",
        app_data_dir=str(tmp_path),
        model_cache_dir=str(tmp_path / "models"),
        logs_dir=str(tmp_path / "logs"),
        catalog_urls=[(CATALOG_URL, None)],
        model_http_transport=transport,
        disable_nonessential_telemetry=True,
        **kwargs,
    )


def test_native_example_registers_and_streams():
    assert api.api_version == 5
    _EXAMPLE["main"]()


@pytest.mark.parametrize("shared_host", [False, True])
def test_native_manager_retains_callbacks_after_python_owners_are_collected(tmp_path, shared_host):
    transport = InMemoryTransport()
    reference = weakref.ref(transport)
    config = configuration(tmp_path, transport, use_shared_host=shared_host)
    before = vars(config).copy()
    native_config = config._build_native()
    assert vars(config) == before
    del before, config, transport
    gc.collect()
    assert reference() is not None
    output = ffi.new("flManager**")
    try:
        api.check_status(api.root.Manager_Create(native_config, output))
    finally:
        api.config.Configuration_Release(native_config)
    try:
        gc.collect()
        assert reference() is not None
        catalog = ffi.new("flCatalog**")
        api.check_status(api.root.Manager_GetCatalogByType(output[0], int(CatalogType.PUBLIC), catalog))
        assert Catalog(catalog[0]).list_models() == []
        transport = reference()
        assert transport.requests
        assert transport.closed == len(transport.requests)
        assert transport.reads > len(transport.requests)
        del transport
    finally:
        api.root.Manager_Release(output[0])
    gc.collect()
    assert reference() is None


def test_native_configuration_replacement_and_clear_release_contexts(tmp_path):
    config = configuration(tmp_path, None)._build_native()
    try:
        first = _TransportBridge(InMemoryTransport(), api._get())
        second = _TransportBridge(InMemoryTransport(), api._get())
        first_ref, second_ref = weakref.ref(first), weakref.ref(second)
        api.check_status(api.config.SetModelHttpTransport(config, first.table))
        del first
        gc.collect()
        assert first_ref() is not None
        api.check_status(api.config.SetModelHttpTransport(config, second.table))
        del second
        gc.collect()
        assert first_ref() is None
        assert second_ref() is not None
        api.check_status(api.config.SetModelHttpTransport(config, ffi.NULL))
        gc.collect()
        assert second_ref() is None
    finally:
        api.config.Configuration_Release(config)


@pytest.mark.parametrize("version", [2, 3, 4])
def test_native_old_api_tables_still_configure_without_transport(tmp_path, monkeypatch, version):
    native = importlib.import_module("foundry_local_sdk._native.api")
    library = native._lib
    requested = []

    def negotiate(candidate):
        requested.append(candidate)
        return library.FoundryLocalGetApi(candidate) if candidate <= version else ffi.NULL

    monkeypatch.setattr(native, "_lib", SimpleNamespace(FoundryLocalGetApi=negotiate))
    runtime = native._Api()
    assert runtime.api_version == version
    assert requested[0] == 5
    output = ffi.new("flConfiguration**")
    runtime.check_status(runtime.config.Create(b"OldApiTransportTest", output))
    try:
        config = configuration(tmp_path, None)
        config._apply_settings(output[0], runtime, ffi)
        config.model_http_transport = InMemoryTransport()
        with pytest.raises(FoundryLocalException, match="API v5"):
            config._apply_settings(output[0], runtime, ffi)
    finally:
        runtime.config.Configuration_Release(output[0])


@pytest.mark.parametrize("failure", [OSError("secret-url"), KeyboardInterrupt(), SystemExit()])
def test_native_status_ownership_and_base_exception_containment(failure):
    def create(request):
        raise failure

    bridge = _TransportBridge(SimpleNamespace(create_operation=create), api._get())
    method = ffi.new("char[]", b"GET")
    url = ffi.new("char[]", b"https://example.invalid")
    request = ffi.new("flHttpRequestData*", {"version": 1, "method": method, "url": url})
    output = ffi.new("void**")
    status = bridge.table.CreateOperation(bridge.handle, request, output)
    assert output[0] == ffi.NULL
    with pytest.raises(FoundryLocalException) as error:
        # check_status reads and releases this owned status using the same runtime.
        api.check_status(status)
    assert error.value.error_code == 6
    assert "secret" not in str(error.value)


def test_native_registered_callback_reenters_python_and_azure_registry_is_rejected(tmp_path):
    threads = []
    caller = threading.get_ident()

    class Transport(InMemoryTransport):
        def create_operation(self, request):
            threads.append(threading.get_ident())
            gc.collect()
            return super().create_operation(request)

    transport = Transport()
    with FoundryLocalManager(configuration(tmp_path, transport)) as manager:
        assert manager.catalog.list_models() == []
        with pytest.raises(FoundryLocalException, match="(?i)(custom|transport|not.*support)"):
            manager.get_catalog(CatalogType.AZURE_REGISTRY, {"name": "example", "credential": "azure_cli"})
    assert threads
    # This catalog path invokes CreateOperation on the calling thread; other
    # native paths and cancellation monitors may use foreign worker threads.
    assert caller in threads


def test_native_registered_deadline_interrupts_real_blocked_socket(tmp_path):
    entered, closed, interrupted = threading.Event(), threading.Event(), threading.Event()
    reader, writer = socket.socketpair()
    cancel_threads = []
    caller = threading.get_ident()

    class Operation:
        def send(self):
            from foundry_local_sdk import HttpResponseHead

            return HttpResponseHead(200)

        def readinto(self, buffer):
            entered.set()
            reader.recv(1)
            interrupted.set()
            return 0

        def cancel(self):
            cancel_threads.append(threading.get_ident())
            reader.shutdown(socket.SHUT_RDWR)

        def close(self):
            reader.close()
            writer.close()
            closed.set()

    requests = []

    def create(request):
        requests.append(request)
        return Operation()

    # No deadline override: the real catalog supplies a 30-second budget.
    # Its native cancellation monitor and the Python deadline timer must be
    # able to reenter Python while a socket read releases the GIL.
    timer = threading.Timer(45, lambda: writer.close())
    timer.daemon = True
    timer.start()
    try:
        with FoundryLocalManager(configuration(tmp_path, SimpleNamespace(create_operation=create))) as manager:
            assert manager.catalog.list_models() == []
        assert requests and requests[0].timeout_ms == 30_000
        assert entered.is_set() and interrupted.is_set() and closed.is_set()
        assert cancel_threads and all(thread != caller for thread in cancel_threads)
    finally:
        timer.cancel()
        timer.join()
        reader.close()
        writer.close()


def test_native_cffi_table_layout_matches_slot_twelve():
    assert ffi.offsetof("flConfigurationApi", "SetModelHttpTransport") == 12 * ffi.sizeof("void*")
    assert ffi.offsetof("flHttpTransport", "version") == 0
    assert ffi.offsetof("flHttpRequestData", "version") == 0
    assert ffi.offsetof("flHttpResponseData", "version") == 0
