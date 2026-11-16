// Copyright (c) Microsoft Corporation. Licensed under the MIT License.
package com.microsoft.foundry.local;

import java.nio.charset.StandardCharsets;
import java.nio.file.Path;
import java.util.List;
import java.util.concurrent.atomic.AtomicInteger;

/** Separate JVM with only the SDK and JNA, not Apache, on its class path. */
public final class NativeModelHttpCoreProbe {
    public static void main(String[] args) throws Exception {
        try {
            Class.forName("org.apache.hc.client5.http.impl.classic.CloseableHttpClient");
            throw new AssertionError("Apache must not be on this probe's class path");
        } catch (ClassNotFoundException expected) {
            // The core API must not need the optional adapter dependency.
        }
        Path work = Path.of(args[1]);
        boolean oversized = "oversized".equals(args[2]);
        var closed = new AtomicInteger();
        var created = new AtomicInteger();
        var sent = new AtomicInteger();
        var bytes = new AtomicInteger();
        var maxRead = new AtomicInteger();
        ModelHttpTransport transport = request -> {
            created.incrementAndGet();
            return new ModelHttpOperation() {
                final byte[] response = "{\"totalCount\":0,\"continuationToken\":\"\",\"summaries\":[]}"
                        .getBytes(StandardCharsets.UTF_8);
                int position;
                @Override public ModelHttpResponseHead send() {
                    sent.incrementAndGet();
                    return new ModelHttpResponseHead(200, List.of(new ModelHttpHeader("Content-Type", "application/json")));
                }
                @Override public int read(byte[] buffer, int offset, int length) {
                    if (oversized) {
                        java.util.Arrays.fill(buffer, offset, offset + length, (byte) ' ');
                        bytes.addAndGet(length);
                        position += length;
                        return length;
                    }
                    int count = Math.min(3, response.length - position);
                    System.arraycopy(response, position, buffer, offset, count);
                    position += count;
                    return count;
                }
                @Override public void cancel() {}
                @Override public void close() {
                    maxRead.accumulateAndGet(position, Math::max);
                    closed.incrementAndGet();
                }
            };
        };
        Configuration configuration = Configuration.builder("java-neutral-transport-probe")
                .runtimeDirectory(Path.of(args[0])).appDataDirectory(work.resolve("app"))
                .modelCacheDirectory(work.resolve("models")).modelHttpTransport(transport).build();
        try (var manager = new FoundryLocalManager(configuration)) {
            if (oversized) {
                // Native catalogs can recover from a failed source with an empty list.
                if (!manager.catalog().models().isEmpty() || bytes.get() <= 16 * 1024 * 1024
                        || maxRead.get() > 16 * 1024 * 1024 + 64 * 1024) {
                    throw new AssertionError("Native metadata limit was not enforced");
                }
            } else if (!manager.catalog().models().isEmpty()) {
                throw new AssertionError("Expected an empty catalog");
            }
        }
        if (created.get() == 0 || sent.get() == 0 || created.get() != closed.get()) {
            throw new AssertionError("Operation ownership was not balanced");
        }
        if (configuration.modelHttpTransport() != transport) throw new AssertionError("Configuration changed");
        System.out.println("neutral-core-without-Apache: " + args[2]);
    }
}
