// Copyright (c) Microsoft Corporation. Licensed under the MIT License.
package com.microsoft.foundry.local;

import com.sun.jna.Pointer;
import com.sun.jna.ptr.PointerByReference;
import java.util.ArrayList;
import java.util.EnumMap;
import java.util.HashSet;
import java.util.Map;
import java.util.Objects;
import java.util.Set;

/**
 * Owns the native singleton and every session. Catalogs/models are borrowed views.
 *
 * <p>Only one manager may be open at a time. After close, another manager can be created in
 * the same JVM only with the same resolved runtime directory used by the first manager.
 */
public final class FoundryLocalManager implements AutoCloseable {
    private static volatile FoundryLocalManager open;
    private static boolean exitHookInstalled;
    final NativeApi api;
    final Set<OwnedSession> sessions = new HashSet<>();
    private final Map<CatalogType, Catalog> catalogs = new EnumMap<>(CatalogType.class);
    private final boolean customModelTransport;
    private Pointer handle;

    public FoundryLocalManager(Configuration configuration) {
        NativeApi.outsideCallback();
        customModelTransport = configuration.modelHttpTransport() != null;
        synchronized (FoundryLocalManager.class) {
            if (open != null) throw new IllegalStateException("Only one FoundryLocalManager may be open per JVM");
            installExitHook();
            api = NativeApi.load(configuration.runtimeDirectory());
            if (configuration.modelHttpTransport() != null) NativeApi.requireTransportApi(api.apiVersion);
            Pointer config = api.create(api.config, NativeApi.ConfigurationApi.CREATE, configuration.appName());
            try {
                for (String url : configuration.catalogUrls()) {
                    api.check(api.config.pointer(NativeApi.ConfigurationApi.ADD_CATALOG_URL, config, url, null));
                }
                if (configuration.modelHttpTransport() != null) {
                    try (var bridge = new NativeModelHttpTransport(api, configuration.modelHttpTransport())) {
                        api.check(NativeApi.registerModelHttpTransport(
                                api.apiVersion, api.config, config, bridge.table.getPointer()));
                    }
                }
                api.check(api.config.pointer(
                        NativeApi.ConfigurationApi.SET_DEFAULT_LOG_LEVEL, config,
                        configuration.logLevel().nativeValue()));
                api.check(api.config.pointer(
                        NativeApi.ConfigurationApi.SET_APP_DATA_DIRECTORY,
                        config,
                        configuration.appDataDirectory().toString()));
                api.check(api.config.pointer(
                        NativeApi.ConfigurationApi.SET_MODEL_CACHE_DIRECTORY,
                        config,
                        configuration.modelCacheDirectory().toString()));
                PointerByReference pairs = new PointerByReference();
                api.root.call(NativeApi.Root.KEY_VALUE_PAIRS_CREATE, pairs);
                try {
                    api.root.call(
                            NativeApi.Root.KEY_VALUE_PAIRS_ADD,
                            pairs.getValue(),
                            "DisableNonessentialTelemetry",
                            Boolean.toString(configuration.disableNonessentialTelemetry()));
                    api.check(api.config.pointer(
                            NativeApi.ConfigurationApi.SET_ADDITIONAL_OPTIONS,
                            config,
                            pairs.getValue()));
                } finally {
                    api.root.call(NativeApi.Root.KEY_VALUE_PAIRS_RELEASE, pairs.getValue());
                }
                handle = api.create(api.root, NativeApi.Root.MANAGER_CREATE, config);
                open = this;
            } finally {
                api.config.call(NativeApi.ConfigurationApi.RELEASE, config);
            }
        }
    }

    // The native runtime must release its manager before its C++ static destructors run at
    // process exit; otherwise the manager's logger teardown can use already-destroyed globals.
    private static void installExitHook() {
        if (exitHookInstalled) return;
        Runtime.getRuntime().addShutdownHook(new Thread(FoundryLocalManager::closeAtExit, "foundry-local-exit"));
        exitHookInstalled = true;
    }

    private static void closeAtExit() {
        FoundryLocalManager manager = open;
        if (manager == null) return;
        try {
            manager.close();
        } catch (RuntimeException | Error ignored) {
            // Best effort: a close failure must not block process exit.
        }
    }

    public String runtimeVersion() { return api.version; }
    public String nativeTarget() { return NativeApi.target(); }

    public Catalog catalog() {
        return catalog(CatalogType.PUBLIC);
    }

    public Catalog catalog(CatalogType type) {
        NativeApi.outsideCallback();
        Objects.requireNonNull(type, "type");
        synchronized (this) {
            checkOpen();
            if (type == CatalogType.AZURE_REGISTRY && customModelTransport) {
                throw new FoundryLocalException(1, "Azure Registry catalogs do not support custom model HTTP transport");
            }
            if (type == CatalogType.AZURE_REGISTRY && api.apiVersion < 4) {
                throw new UnsupportedOperationException("Azure Registry catalogs require native C API 4");
            }
            return catalogs.computeIfAbsent(type, value -> new Catalog(
                    this,
                    api.create(
                            api.root,
                            NativeApi.Root.MANAGER_GET_CATALOG_BY_TYPE,
                            handle,
                            value.nativeValue()),
                    value));
        }
    }

    void checkOpen() {
        NativeApi.outsideCallback();
        if (handle == null) throw new IllegalStateException("Manager is closed");
    }

    @Override public void close() {
        NativeApi.outsideCallback();
        synchronized (this) {
            if (handle == null) return;
            Throwable failure = null;
            try {
                api.check(api.root.pointer(NativeApi.Root.MANAGER_SHUTDOWN, handle));
            } catch (RuntimeException | Error e) {
                failure = NativeApi.preserveFailure(failure, e);
            }
            for (OwnedSession session : new ArrayList<>(sessions)) {
                try {
                    session.close();
                } catch (RuntimeException | Error e) {
                    failure = NativeApi.preserveFailure(failure, e);
                }
            }
            try {
                api.root.call(NativeApi.Root.MANAGER_RELEASE, handle);
            } catch (RuntimeException | Error e) {
                failure = NativeApi.preserveFailure(failure, e);
            } finally {
                catalogs.clear();
                handle = null;
                synchronized (FoundryLocalManager.class) {
                    open = null;
                }
            }
            NativeApi.rethrow(failure);
        }
    }
}
