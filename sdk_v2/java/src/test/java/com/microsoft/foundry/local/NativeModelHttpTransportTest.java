// Copyright (c) Microsoft Corporation. Licensed under the MIT License.
package com.microsoft.foundry.local;

import static org.junit.jupiter.api.Assertions.*;
import static org.junit.jupiter.api.Assumptions.assumeTrue;
import com.sun.jna.Function;
import com.sun.jna.Memory;
import com.sun.jna.Pointer;
import com.sun.jna.ptr.LongByReference;
import com.sun.jna.ptr.PointerByReference;
import java.io.IOException;
import java.lang.ref.WeakReference;
import java.nio.charset.StandardCharsets;
import java.nio.file.Path;
import java.util.List;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import org.junit.jupiter.api.Test;

class NativeModelHttpTransportTest {
    private static Path runtime() {
        String runtime = System.getProperty("foundry.test.runtime", System.getenv("FOUNDRY_LOCAL_NATIVE_BIN_DIR"));
        if (Boolean.getBoolean("foundry.test.native.required")) {
            assertNotNull(runtime, "A native runtime is required");
            assertFalse(runtime.isBlank(), "A native runtime is required");
        }
        assumeTrue(runtime != null && !runtime.isBlank(), "Configure foundry.test.runtime");
        return Path.of(runtime);
    }

    @Test void neutralCoreWorksWithoutApacheAndNativeMetadataLimitStillApplies() throws Exception {
        for (String mode : List.of("success", "oversized")) {
            Process process = NativeTestProcess.start(NativeModelHttpCoreProbe.class, runtime().toAbsolutePath().toString(),
                    Path.of("target/model-http-core-" + mode).toAbsolutePath().toString(), mode);
            try {
                assertTrue(process.waitFor(30, TimeUnit.SECONDS), "Neutral core probe did not exit");
                String output = new String(process.getInputStream().readAllBytes(), StandardCharsets.UTF_8);
                assertEquals(0, process.exitValue(), output);
                assertTrue(output.contains("neutral-core-without-Apache: " + mode), output);
            } finally {
                if (process.isAlive()) {
                    process.destroyForcibly();
                    process.waitFor();
                }
            }
        }
    }

    @Test void registeredApacheExampleUsesActualConfigurationManagerAndCatalog() throws Exception {
        Path runtime = runtime().toAbsolutePath();
        String java = Path.of(System.getProperty("java.home"), "bin", "java").toString();
        var process = new ProcessBuilder(java, "-cp", System.getProperty("surefire.test.class.path"),
                Path.of("examples/ModelHttpTransportExample.java").toAbsolutePath().toString(),
                runtime.toString(), Path.of("target/model-http-example").toAbsolutePath().toString())
                .redirectErrorStream(true).start();
        try {
            assertTrue(process.waitFor(45, TimeUnit.SECONDS), "Registered native example did not exit");
            String output = new String(process.getInputStream().readAllBytes(), StandardCharsets.UTF_8);
            assertEquals(0, process.exitValue(), output);
            assertTrue(output.contains("1 model (example)"), output);
            assertTrue(output.contains("Azure Registry rejected"), output);
            System.out.print(output);
        } finally {
            if (process.isAlive()) {
                process.destroyForcibly();
                process.waitFor();
            }
        }
    }

    @Test void nativeFunctionPointersOwnRequestHeadersErrorsAndRetainedGcRoots() throws Exception {
        NativeApi api = NativeApi.load(runtime());
        var closed = new AtomicBoolean();
        var bridge = new NativeModelHttpTransport(api, request -> {
            assertThrows(IllegalStateException.class, NativeApi::outsideCallback);
            assertEquals("https://example.com/a%2fb?sig=a%2Bb", request.url());
            assertEquals(List.of(new ModelHttpHeader("X-Dup", "\u00ff"), new ModelHttpHeader("X-Dup", "two")),
                    request.headers());
            assertArrayEquals(new byte[] {0, 1, (byte) 255}, request.body());
            return new ModelHttpOperation() {
                int reads;
                @Override public ModelHttpResponseHead send() {
                    assertArrayEquals(new byte[] {0, 1, (byte) 255}, request.body());
                    return new ModelHttpResponseHead(403, List.of(
                            new ModelHttpHeader("X-Dup", "\u00ff"), new ModelHttpHeader("X-Dup", "two")));
                }
                @Override public int read(byte[] buffer, int offset, int length) {
                    if (reads++ != 0) return 0;
                    buffer[offset] = 42;
                    return 1;
                }
                @Override public void cancel() {}
                @Override public void close() { closed.set(true); }
            };
        });
        Pointer callbacks = bridge.table.getPointer();
        callVoid(callbacks, 16, bridge.table.userData);
        WeakReference<NativeModelHttpTransport> retained = new WeakReference<>(bridge);
        Pointer context = bridge.table.userData;
        bridge.close();
        bridge = null;
        System.gc();
        assertNotNull(retained.get(), "Native retain must root callback code and runtime");
        Pointer operation = null;
        try (var method = NativeApi.utf8("POST");
                var url = NativeApi.utf8("https://example.com/a%2fb?sig=a%2Bb");
                var name = NativeApi.utf8("X-Dup"); var value = new Memory(2); var two = NativeApi.utf8("two");
                var fields = new Memory(32); var body = new Memory(3); var response = new Memory(24);
                var buffer = new Memory(4)) {
            value.setByte(0, (byte) 255);
            value.setByte(1, (byte) 0);
            fields.setPointer(0, name); fields.setPointer(8, value);
            fields.setPointer(16, name); fields.setPointer(24, two);
            body.write(0, new byte[] {0, 1, (byte) 255}, 0, 3);
            var request = new NativeModelHttpTransport.RequestData();
            request.method = method; request.url = url; request.headers = fields; request.headerCount = 2;
            request.body = body; request.bodySize = 3; request.write();
            var output = new PointerByReference();
            api.check(call(callbacks, 32, context, request.getPointer(), output));
            operation = output.getValue();
            assertNotNull(operation);
            body.setByte(0, (byte) 9);
            response.clear(); response.setInt(0, 1);
            api.check(call(callbacks, 40, operation, response));
            assertEquals(403, response.getInt(4));
            assertEquals(2, response.getLong(16));
            Pointer headers = response.getPointer(8);
            System.gc();
            assertEquals("\u00ff", headers.getPointer(8).getString(0, "ISO-8859-1"));
            assertEquals("two", headers.getPointer(24).getString(0, "ISO-8859-1"));
            var count = new LongByReference();
            api.check(call(callbacks, 48, operation, buffer, 4L, count));
            assertEquals(1, count.getValue());
            assertEquals(42, buffer.getByte(0));
            api.check(call(callbacks, 48, operation, buffer, 4L, count));
            assertEquals(0, count.getValue());
            callVoid(callbacks, 56, operation);
            FoundryLocalException cancelled = assertThrows(FoundryLocalException.class,
                    () -> api.check(call(callbacks, 40, output.getValue(), response)));
            assertEquals(5, cancelled.code());
        } finally {
            if (operation != null) callVoid(callbacks, 64, operation);
            callVoid(callbacks, 24, context);
        }
        assertTrue(closed.get());
    }

    @Test void translatesThrowablesIntoOwnedSameRuntimeSecretSafeStatuses() {
        NativeApi api = NativeApi.load(runtime());
        for (Throwable failure : List.of(new IOException("secret-url?sig=credential"), new AssertionError("secret"))) {
            try (var bridge = new NativeModelHttpTransport(api, request -> {
                if (failure instanceof IOException io) throw io;
                throw (AssertionError) failure;
            }); var method = NativeApi.utf8("GET"); var url = NativeApi.utf8("https://example.com")) {
                var request = new NativeModelHttpTransport.RequestData();
                request.method = method; request.url = url; request.write();
                var output = new PointerByReference(new Pointer(123));
                Pointer status = call(bridge.table.getPointer(), 32, bridge.table.userData, request.getPointer(), output);
                assertNotNull(status);
                FoundryLocalException error = assertThrows(FoundryLocalException.class, () -> api.check(status));
                assertEquals(failure instanceof IOException ? 6 : 2, error.code());
                assertFalse(error.getMessage().contains("secret"));
                assertNull(output.getValue());
                assertFalse(NativeApi.IN_CALLBACK.get());
            }
        }
    }

    @Test void concurrentNativeCancelWakesBlockedCallbackAndReturnsCancellationStatus() throws Exception {
        NativeApi api = NativeApi.load(runtime());
        CountDownLatch reading = new CountDownLatch(1);
        CountDownLatch cancelled = new CountDownLatch(1);
        var executor = Executors.newSingleThreadExecutor();
        try (var bridge = new NativeModelHttpTransport(api, request -> new ModelHttpOperation() {
            @Override public ModelHttpResponseHead send() { return new ModelHttpResponseHead(200, List.of()); }
            @Override public int read(byte[] buffer, int offset, int length) throws IOException {
                reading.countDown();
                try {
                    if (!cancelled.await(5, TimeUnit.SECONDS)) throw new AssertionError("Cancel did not wake read");
                } catch (InterruptedException interrupted) {
                    Thread.currentThread().interrupt();
                    throw new IOException(interrupted);
                }
                throw new IOException("cancelled");
            }
            @Override public void cancel() { cancelled.countDown(); }
            @Override public void close() {}
        }); var method = NativeApi.utf8("GET"); var url = NativeApi.utf8("https://example.com");
                var response = new Memory(24); var buffer = new Memory(1)) {
            var request = new NativeModelHttpTransport.RequestData();
            request.method = method; request.url = url; request.write();
            var output = new PointerByReference();
            Pointer table = bridge.table.getPointer();
            api.check(call(table, 32, bridge.table.userData, request.getPointer(), output));
            Pointer operation = output.getValue();
            try {
                response.clear(); response.setInt(0, 1);
                api.check(call(table, 40, operation, response));
                var pending = executor.submit(() -> assertThrows(FoundryLocalException.class, () ->
                        api.check(call(table, 48, operation, buffer, 1L, new LongByReference()))));
                assertTrue(reading.await(5, TimeUnit.SECONDS));
                callVoid(table, 56, operation);
                assertEquals(5, pending.get(5, TimeUnit.SECONDS).code());
            } finally {
                callVoid(table, 64, operation);
            }
        } finally {
            cancelled.countDown();
            executor.shutdownNow();
            assertTrue(executor.awaitTermination(5, TimeUnit.SECONDS));
        }
    }

    private static Pointer call(Pointer table, long offset, Object... args) {
        return Function.getFunction(table.getPointer(offset)).invokePointer(args);
    }
    private static void callVoid(Pointer table, long offset, Object... args) {
        Function.getFunction(table.getPointer(offset)).invokeVoid(args);
    }
}
