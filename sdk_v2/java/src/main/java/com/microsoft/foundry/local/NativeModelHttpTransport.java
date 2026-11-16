// Copyright (c) Microsoft Corporation. Licensed under the MIT License.
package com.microsoft.foundry.local;

import com.sun.jna.Callback;
import com.sun.jna.Memory;
import com.sun.jna.Pointer;
import com.sun.jna.Structure;
import com.sun.jna.ptr.LongByReference;
import com.sun.jna.ptr.PointerByReference;
import java.io.InterruptedIOException;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;
import java.util.Objects;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicLong;

/** Roots callback code, runtime, and owned operation storage until the final native release. */
final class NativeModelHttpTransport implements AutoCloseable {
    private static final AtomicLong IDS = new AtomicLong();
    private static final ConcurrentHashMap<Long, NativeModelHttpTransport> ROOTS = new ConcurrentHashMap<>();
    private static final System.Logger LOG = System.getLogger(NativeModelHttpTransport.class.getName());
    private final NativeApi api;
    private final ModelHttpTransport transport;
    private final long id = IDS.incrementAndGet();
    private final AtomicInteger references = new AtomicInteger(1);
    private final ConcurrentHashMap<Long, Operation> operations = new ConcurrentHashMap<>();
    final TransportData table = new TransportData();

    NativeModelHttpTransport(NativeApi api, ModelHttpTransport transport) {
        this.api = api;
        this.transport = transport;
        table.userData = new Pointer(id);
        table.retain = context -> quiet(() -> references.incrementAndGet());
        table.release = context -> quiet(this::close);
        table.create = (context, request, output) -> fallible(null, () -> {
            output.setValue(null);
            RequestData data = new RequestData(request);
            if (data.version != 1) throw new IllegalArgumentException("Unsupported HTTP request version");
            List<ModelHttpHeader> headers = new ArrayList<>();
            for (int i = 0, count = Math.toIntExact(data.headerCount); i < count; i++) {
                Pointer field = data.headers.share(i * 16L);
                headers.add(new ModelHttpHeader(field.getPointer(0).getString(0, "ISO-8859-1"),
                        field.getPointer(8).getString(0, "ISO-8859-1")));
            }
            int size = Math.toIntExact(data.bodySize);
            ModelHttpRequest owned = new ModelHttpRequest(NativeApi.text(data.method), NativeApi.text(data.url),
                    headers, size == 0 ? new byte[0] : data.body.getByteArray(0, size), data.timeoutMillis);
            ModelHttpOperation implementation = Objects.requireNonNull(transport.createOperation(owned), "operation");
            long operationId = IDS.incrementAndGet();
            operations.put(operationId, new Operation(owned, implementation));
            output.setValue(new Pointer(operationId));
        });
        table.send = (handle, response) -> {
            Operation operation = operations.get(Pointer.nativeValue(handle));
            return fallible(operation, () -> {
                synchronized (operation) {
                    operation.check();
                    ModelHttpResponseHead head = Objects.requireNonNull(operation.implementation.send(), "response");
                    operation.check();
                    if (response.getInt(0) != 1) throw new IllegalArgumentException("Unsupported HTTP response version");
                    operation.headers(head);
                    response.setInt(4, head.statusCode());
                    response.setPointer(8, operation.headerArray);
                    response.setLong(16, head.headers().size());
                }
            });
        };
        table.read = (handle, buffer, capacity, output) -> {
            Operation operation = operations.get(Pointer.nativeValue(handle));
            return fallible(operation, () -> {
                output.setValue(0);
                synchronized (operation) {
                    operation.check();
                    // Bound each Java allocation, not the response stream. Short reads are part of the ABI.
                    byte[] bytes = new byte[(int) Math.min(capacity, 64 * 1024L)];
                    if (bytes.length == 0) throw new IllegalArgumentException("Read capacity must be positive");
                    int count = operation.implementation.read(bytes, 0, bytes.length);
                    operation.check();
                    if (count < 0 || count > bytes.length) throw new IllegalArgumentException("Invalid read count");
                    buffer.write(0, bytes, 0, count);
                    output.setValue(count);
                }
            });
        };
        table.cancel = handle -> quiet(() -> {
            Operation operation = operations.get(Pointer.nativeValue(handle));
            operation.cancelled = true;
            operation.implementation.cancel();
        });
        table.releaseOperation = handle -> quiet(() -> {
            Operation operation = operations.remove(Pointer.nativeValue(handle));
            if (operation != null) {
                try {
                    operation.implementation.close();
                } finally {
                    operation.disposeHeaders();
                }
            }
        });
        table.write();
        ROOTS.put(id, this);
    }

    @Override public void close() {
        if (references.decrementAndGet() == 0) ROOTS.remove(id);
    }

    private Pointer fallible(Operation operation, Checked action) {
        boolean previous = NativeApi.IN_CALLBACK.get();
        NativeApi.IN_CALLBACK.set(true);
        try {
            action.run();
            return null;
        } catch (Throwable failure) {
            int code = operation != null && operation.cancelled ? 5
                    : failure instanceof java.io.IOException ? 6 : failure instanceof IllegalArgumentException ? 3 : 2;
            // Never put exception messages, URLs, credentials, or request bodies in a native status.
            return api.root.pointer(NativeApi.Root.STATUS_CREATE, code,
                    code == 5 ? "Model HTTP operation cancelled" : "Model HTTP callback failed");
        } finally {
            NativeApi.IN_CALLBACK.set(previous);
        }
    }

    private static void quiet(Checked action) {
        boolean previous = NativeApi.IN_CALLBACK.get();
        NativeApi.IN_CALLBACK.set(true);
        try {
            action.run();
        } catch (Throwable failure) {
            // These native callbacks have no status return. Report failure without exposing application secrets.
            LOG.log(System.Logger.Level.ERROR, "Model HTTP lifecycle or cancellation callback failed");
        } finally {
            NativeApi.IN_CALLBACK.set(previous);
        }
    }

    @FunctionalInterface private interface Checked { void run() throws Exception; }

    private static final class Operation {
        final ModelHttpRequest request;
        final ModelHttpOperation implementation;
        final List<Memory> strings = new ArrayList<>();
        Memory headerArray;
        volatile boolean cancelled;

        Operation(ModelHttpRequest request, ModelHttpOperation implementation) {
            this.request = request;
            this.implementation = implementation;
        }

        void check() throws InterruptedIOException {
            if (cancelled) throw new InterruptedIOException("Model HTTP operation cancelled");
        }

        void headers(ModelHttpResponseHead head) {
            if (head.headers().isEmpty()) return;
            headerArray = new Memory(head.headers().size() * 16L);
            for (int i = 0; i < head.headers().size(); i++) {
                ModelHttpHeader header = head.headers().get(i);
                headerArray.setPointer(i * 16L, string(header.name()));
                headerArray.setPointer(i * 16L + 8, string(header.value()));
            }
        }

        private Memory string(String value) {
            byte[] bytes = value.getBytes(StandardCharsets.ISO_8859_1);
            Memory memory = new Memory(bytes.length + 1L);
            strings.add(memory);
            memory.write(0, bytes, 0, bytes.length);
            memory.setByte(bytes.length, (byte) 0);
            return memory;
        }

        void disposeHeaders() {
            for (Memory string : strings) string.close();
            if (headerArray != null) headerArray.close();
        }
    }

    interface ContextCallback extends Callback { void invoke(Pointer context); }
    interface CreateCallback extends Callback {
        Pointer invoke(Pointer context, Pointer request, PointerByReference output);
    }
    interface SendCallback extends Callback { Pointer invoke(Pointer operation, Pointer response); }
    interface ReadCallback extends Callback {
        Pointer invoke(Pointer operation, Pointer buffer, long capacity, LongByReference count);
    }

    @Structure.FieldOrder({"version", "userData", "retain", "release", "create", "send", "read",
            "cancel", "releaseOperation"})
    public static class TransportData extends Structure {
        public int version = 1;
        public Pointer userData;
        public ContextCallback retain, release;
        public CreateCallback create;
        public SendCallback send;
        public ReadCallback read;
        public ContextCallback cancel, releaseOperation;
    }

    @Structure.FieldOrder({"version", "method", "url", "headers", "headerCount", "body", "bodySize", "timeoutMillis"})
    public static class RequestData extends Structure {
        public int version = 1;
        public Pointer method, url, headers;
        public long headerCount;
        public Pointer body;
        public long bodySize, timeoutMillis;
        public RequestData() {}
        RequestData(Pointer pointer) { super(pointer); read(); }
    }
}
