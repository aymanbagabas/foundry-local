// Copyright (c) Microsoft Corporation. Licensed under the MIT License.
package com.microsoft.foundry.local;

import static org.junit.jupiter.api.Assertions.*;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.net.InetSocketAddress;
import java.net.ServerSocket;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.KeyStore;
import java.util.List;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicReference;
import java.util.zip.GZIPOutputStream;
import javax.net.ssl.KeyManagerFactory;
import javax.net.ssl.SSLContext;
import javax.net.ssl.TrustManagerFactory;
import org.apache.hc.core5.http.HttpHost;
import org.junit.jupiter.api.Test;

class ApacheModelHttpTransportTest {
    @Test void applicationTrustWorksWithoutDisablingCertificateOrHostnameChecks() throws Exception {
        Path directory = Path.of("target", "model-http-tls-" + java.util.UUID.randomUUID());
        Files.createDirectories(directory);
        Path keys = directory.resolve("fixture.p12");
        var executor = Executors.newCachedThreadPool();
        com.sun.net.httpserver.HttpsServer server = null;
        try {
            var keytool = new ProcessBuilder(Path.of(System.getProperty("java.home"), "bin", "keytool").toString(),
                    "-genkeypair", "-alias", "localhost", "-keyalg", "RSA", "-keysize", "2048",
                    "-validity", "1", "-dname", "CN=localhost", "-ext", "SAN=dns:localhost,ip:127.0.0.1",
                    "-storetype", "PKCS12", "-keystore", keys.toString(), "-storepass", "fixture-password",
                    "-keypass", "fixture-password", "-noprompt").redirectErrorStream(true).start();
            try {
                assertTrue(keytool.waitFor(20, TimeUnit.SECONDS));
                assertEquals(0, keytool.exitValue(), new String(keytool.getInputStream().readAllBytes(),
                        StandardCharsets.UTF_8));
            } finally {
                if (keytool.isAlive()) keytool.destroyForcibly().waitFor();
            }
            KeyStore store = KeyStore.getInstance("PKCS12");
            try (var input = Files.newInputStream(keys)) { store.load(input, "fixture-password".toCharArray()); }
            var keyManagers = KeyManagerFactory.getInstance(KeyManagerFactory.getDefaultAlgorithm());
            keyManagers.init(store, "fixture-password".toCharArray());
            var trustManagers = TrustManagerFactory.getInstance(TrustManagerFactory.getDefaultAlgorithm());
            trustManagers.init(store);
            SSLContext tls = SSLContext.getInstance("TLS");
            tls.init(keyManagers.getKeyManagers(), trustManagers.getTrustManagers(), null);
            server = com.sun.net.httpserver.HttpsServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
            server.setHttpsConfigurator(new com.sun.net.httpserver.HttpsConfigurator(tls));
            server.setExecutor(executor);
            server.createContext("/", exchange -> {
                exchange.sendResponseHeaders(200, -1);
                exchange.close();
            });
            server.start();
            String url = "https://127.0.0.1:" + server.getAddress().getPort() + "/";
            try (var strict = ApacheModelHttpTransport.builder().build();
                    var operation = strict.createOperation(request(url, 5000))) {
                assertThrows(IOException.class, operation::send, "Untrusted certificates must fail");
            }
            try (var trusted = ApacheModelHttpTransport.builder().sslContext(tls).build();
                    var operation = trusted.createOperation(request(url, 5000))) {
                assertEquals(200, operation.send().statusCode());
            }
            try (var trusted = ApacheModelHttpTransport.builder().sslContext(tls)
                    .dnsResolver(new org.apache.hc.client5.http.DnsResolver() {
                        @Override public java.net.InetAddress[] resolve(String host) {
                            return new java.net.InetAddress[] {java.net.InetAddress.getLoopbackAddress()};
                        }
                        @Override public String resolveCanonicalHostname(String host) { return host; }
                    }).build();
                    var operation = trusted.createOperation(request(url.replace("127.0.0.1", "wrong.invalid"), 5000))) {
                assertThrows(IOException.class, operation::send, "Custom trust must retain hostname verification");
            }
        } finally {
            if (server != null) server.stop(0);
            executor.shutdownNow();
            assertTrue(executor.awaitTermination(5, TimeUnit.SECONDS));
            Files.deleteIfExists(keys);
            Files.delete(directory);
        }
    }

    @Test void streamsRawBytesPreservesDuplicatesQueryAndPostBody() throws Exception {
        byte[] content;
        try (var bytes = new ByteArrayOutputStream(); var gzip = new GZIPOutputStream(bytes)) {
            gzip.write("payload".getBytes(StandardCharsets.UTF_8));
            gzip.finish();
            content = bytes.toByteArray();
        }
        var observed = new AtomicReference<String>();
        var requestHeaders = new AtomicReference<List<String>>();
        var requestBody = new AtomicReference<byte[]>();
        var requestFieldBytes = new AtomicReference<String>();
        try (var server = new Server(exchange -> {
            observed.set(exchange.getRequestURI().toASCIIString());
            requestHeaders.set(exchange.getRequestHeaders().get("X-Duplicate"));
            requestBody.set(exchange.getRequestBody().readAllBytes());
            requestFieldBytes.set(exchange.getRequestHeaders().getFirst("X-Bytes"));
            exchange.getResponseHeaders().add("X-Duplicate", "one");
            exchange.getResponseHeaders().add("X-Duplicate", "two");
            exchange.getResponseHeaders().add("Content-Encoding", "gzip");
            exchange.getResponseHeaders().add("X-Bytes", "\u00ff");
            exchange.sendResponseHeaders(403, 0);
            exchange.getResponseBody().write(content);
            exchange.close();
        }); var transport = ApacheModelHttpTransport.builder().build();
                var operation = transport.createOperation(new ModelHttpRequest("POST",
                        server.url("/a%2fb?sig=a%2Bb%2f&x=+&x=%20"),
                        List.of(new ModelHttpHeader("X-Duplicate", "first"),
                                new ModelHttpHeader("X-Duplicate", "second"),
                                new ModelHttpHeader("Content-Length", "3"),
                                new ModelHttpHeader("X-Bytes", "\u00ff"),
                                new ModelHttpHeader("Range", "bytes=0-5"),
                                new ModelHttpHeader("If-Match", "\"etag\"")),
                        new byte[] {0, 1, (byte) 255}, 0))) {
            ModelHttpResponseHead head = operation.send();
            assertEquals(403, head.statusCode());
            assertTrue(head.headers().stream().anyMatch(header ->
                    header.name().equalsIgnoreCase("X-Bytes") && header.value().equals("\u00ff")));
            assertEquals(List.of("one", "two"), head.headers().stream()
                    .filter(header -> header.name().equalsIgnoreCase("X-Duplicate"))
                    .map(ModelHttpHeader::value).toList());
            var bytes = new ByteArrayOutputStream();
            byte[] buffer = new byte[3];
            for (int count; (count = operation.read(buffer, 0, buffer.length)) != 0;) bytes.write(buffer, 0, count);
            assertArrayEquals(content, bytes.toByteArray(), "No automatic decompression");
            assertEquals("/a%2fb?sig=a%2Bb%2f&x=+&x=%20", observed.get());
            assertEquals(List.of("first", "second"), requestHeaders.get());
            assertArrayEquals(new byte[] {0, 1, (byte) 255}, requestBody.get());
            assertEquals("\u00ff", requestFieldBytes.get());
        }
    }

    @Test void doesNotFollowRedirectsReplayAuthenticationOrManageCookies() throws Exception {
        AtomicInteger requests = new AtomicInteger();
        var cookie = new AtomicReference<String>();
        try (var server = new Server(exchange -> {
            requests.incrementAndGet();
            cookie.set(exchange.getRequestHeaders().getFirst("Cookie"));
            if (exchange.getRequestURI().getPath().equals("/redirect")) {
                exchange.getResponseHeaders().add("Location", "/other");
                exchange.getResponseHeaders().add("Set-Cookie", "session=secret");
                exchange.sendResponseHeaders(302, -1);
            } else if (exchange.getRequestURI().getPath().equals("/retry")) {
                exchange.getResponseHeaders().add("Retry-After", "0");
                exchange.sendResponseHeaders(503, -1);
            } else if (exchange.getRequestURI().getPath().equals("/proxy-auth")) {
                exchange.getResponseHeaders().add("Proxy-Authenticate", "Basic realm=\"proxy\"");
                exchange.sendResponseHeaders(407, -1);
            } else {
                exchange.getResponseHeaders().add("WWW-Authenticate", "Basic realm=\"test\"");
                exchange.sendResponseHeaders(401, -1);
            }
            exchange.close();
        }); var transport = ApacheModelHttpTransport.builder().build()) {
            try (var operation = transport.createOperation(request(server.url("/redirect"), 0))) {
                assertEquals(302, operation.send().statusCode());
            }
            try (var operation = transport.createOperation(request(server.url("/auth"), 0))) {
                assertEquals(401, operation.send().statusCode());
            }
            try (var operation = transport.createOperation(request(server.url("/retry"), 0))) {
                assertEquals(503, operation.send().statusCode());
            }
            try (var operation = transport.createOperation(request(server.url("/proxy-auth"), 0))) {
                assertEquals(407, operation.send().statusCode());
            }
            assertEquals(4, requests.get());
            assertNull(cookie.get());
        }
    }

    @Test void applicationProxyReceivesOriginalAbsoluteUrlWithoutDnsLookup() throws Exception {
        var observed = new AtomicReference<String>();
        try (var proxy = new Server(exchange -> {
            observed.set(exchange.getRequestURI().toString());
            exchange.sendResponseHeaders(200, -1);
            exchange.close();
        }); var transport = ApacheModelHttpTransport.builder()
                .proxy(new HttpHost("http", "127.0.0.1", proxy.server.getAddress().getPort())).build();
                var operation = transport.createOperation(request("http://nonexistent.invalid/a%2fb?x=a%2Bb", 0))) {
            assertEquals(200, operation.send().statusCode());
            assertEquals("http://nonexistent.invalid/a%2fb?x=a%2Bb", observed.get());
        }
    }

    @Test void supportsHeadAndValidatedRequestFraming() throws Exception {
        var encoding = new AtomicReference<String>();
        var body = new AtomicReference<byte[]>();
        try (var server = new Server(exchange -> {
            encoding.set(exchange.getRequestHeaders().getFirst("Transfer-Encoding"));
            body.set(exchange.getRequestBody().readAllBytes());
            exchange.getResponseHeaders().set("Content-Length", "123");
            exchange.sendResponseHeaders(200, -1);
            exchange.close();
        }); var transport = ApacheModelHttpTransport.builder().build()) {
            try (var operation = transport.createOperation(new ModelHttpRequest("HEAD", server.url("/head"),
                    List.of(), new byte[0], 0))) {
                assertEquals(200, operation.send().statusCode());
                assertEquals(0, operation.read(new byte[1], 0, 1));
            }
            try (var operation = transport.createOperation(new ModelHttpRequest("POST", server.url("/chunked"),
                    List.of(new ModelHttpHeader("Transfer-Encoding", "chunked")), new byte[] {1, 2, 3}, 0))) {
                assertEquals(200, operation.send().statusCode());
                assertEquals("chunked", encoding.get());
                assertArrayEquals(new byte[] {1, 2, 3}, body.get());
            }
            for (var fields : List.of(
                    List.of(new ModelHttpHeader("Content-Length", "4")),
                    List.of(new ModelHttpHeader("Content-Length", "0"), new ModelHttpHeader("Transfer-Encoding", "chunked")))) {
                assertThrows(IllegalArgumentException.class, () -> transport.createOperation(
                        new ModelHttpRequest("POST", server.url("/invalid"), fields, new byte[0], 0)));
            }
        }
    }

    @Test void createAndCancelBeforeSendNeverAccessNetwork() throws Exception {
        AtomicInteger resolutions = new AtomicInteger();
        try (var transport = ApacheModelHttpTransport.builder().dnsResolver(new org.apache.hc.client5.http.DnsResolver() {
            @Override public java.net.InetAddress[] resolve(String host) throws java.net.UnknownHostException {
                resolutions.incrementAndGet();
                throw new java.net.UnknownHostException("test");
            }
            @Override public String resolveCanonicalHostname(String host) { return host; }
        }).build(); var operation = transport.createOperation(request("http://unused.invalid/", 0))) {
            assertEquals(0, resolutions.get());
            operation.cancel();
            operation.cancel();
            assertThrows(IOException.class, operation::send);
            assertThrows(IOException.class, () -> operation.read(new byte[1], 0, 1));
            assertEquals(0, resolutions.get());
        }
    }

    @Test void cancelsBlockedHeadersAndBodyWithoutTakingReadMonitor() throws Exception {
        for (boolean body : List.of(false, true)) {
            try (var blocked = new Blocked(body);
                    var transport = ApacheModelHttpTransport.builder().build();
                    var operation = transport.createOperation(request(blocked.server.url("/blocked"), 0))) {
                Future<?> pending = blocked.executor.submit(() -> assertThrows(IOException.class, () -> {
                    blocked.worker.set(Thread.currentThread());
                    if (body) {
                        assertEquals(200, operation.send().statusCode());
                        blocked.readStarted.countDown();
                        operation.read(new byte[1], 0, 1);
                    } else {
                        operation.send();
                    }
                }));
                assertTrue(blocked.entered.await(5, TimeUnit.SECONDS));
                if (body) assertTrue(blocked.readStarted.await(5, TimeUnit.SECONDS));
                awaitSocketRead(blocked.worker.get());
                operation.cancel();
                pending.get(5, TimeUnit.SECONDS);
                assertThrows(IOException.class, () -> operation.read(new byte[1], 0, 1));
            }
        }
    }

    @Test void totalDeadlineInterruptsBlockedHeadersAndBody() throws Exception {
        for (boolean body : List.of(false, true)) {
            try (var blocked = new Blocked(body);
                    var transport = ApacheModelHttpTransport.builder().build();
                    var operation = transport.createOperation(request(blocked.server.url("/deadline"), 500))) {
                Future<?> pending = blocked.executor.submit(() -> {
                    IOException failure = assertThrows(IOException.class, () -> {
                        operation.send();
                        if (body) operation.read(new byte[1], 0, 1);
                    });
                    assertEquals("Model HTTP deadline expired", failure.getMessage());
                });
                assertTrue(blocked.entered.await(5, TimeUnit.SECONDS));
                pending.get(5, TimeUnit.SECONDS);
            }
        }
    }

    @Test void noAutomaticRetryAfterConnectionClosesWithoutResponse() throws Exception {
        AtomicInteger requests = new AtomicInteger();
        ExecutorService executor = Executors.newSingleThreadExecutor();
        try (var server = new ServerSocket(0, 10, java.net.InetAddress.getLoopbackAddress());
                var transport = ApacheModelHttpTransport.builder().build()) {
            server.setSoTimeout(2000);
            Future<?> peer = executor.submit(() -> {
                try {
                    while (true) {
                        try (var socket = server.accept()) {
                            socket.setSoTimeout(2000);
                            var reader = new java.io.BufferedReader(new java.io.InputStreamReader(
                                    socket.getInputStream(), StandardCharsets.ISO_8859_1));
                            for (String line; (line = reader.readLine()) != null && !line.isEmpty();) { }
                            requests.incrementAndGet();
                        }
                    }
                } catch (IOException expected) {
                    // The accept timeout ends observation after the failed request returns.
                }
            });
            try (var operation = transport.createOperation(request(
                    "http://127.0.0.1:" + server.getLocalPort() + "/", 0))) {
                assertThrows(IOException.class, operation::send);
            }
            peer.get(5, TimeUnit.SECONDS);
            assertEquals(1, requests.get());
        } finally {
            executor.shutdownNow();
            assertTrue(executor.awaitTermination(5, TimeUnit.SECONDS));
        }
    }

    private static ModelHttpRequest request(String url, long timeout) {
        return new ModelHttpRequest("GET", url, List.of(), new byte[0], timeout);
    }

    private static void awaitSocketRead(Thread worker) {
        long end = System.nanoTime() + TimeUnit.SECONDS.toNanos(5);
        while (System.nanoTime() < end) {
            for (StackTraceElement frame : worker.getStackTrace()) {
                if (frame.getClassName().equals("sun.nio.ch.NioSocketImpl")
                        && frame.getMethodName().equals("implRead")) return;
            }
            java.util.concurrent.locks.LockSupport.parkNanos(TimeUnit.MILLISECONDS.toNanos(1));
        }
        fail("The operation did not block inside socket I/O");
    }

    private interface Handler { void handle(HttpExchange exchange) throws IOException; }

    private static final class Server implements AutoCloseable {
        final HttpServer server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        final ExecutorService executor = Executors.newCachedThreadPool();
        Server(Handler handler) throws IOException {
            server.createContext("/", handler::handle);
            server.setExecutor(executor);
            server.start();
        }
        String url(String path) { return "http://127.0.0.1:" + server.getAddress().getPort() + path; }
        @Override public void close() throws InterruptedException {
            server.stop(0);
            executor.shutdownNow();
            assertTrue(executor.awaitTermination(5, TimeUnit.SECONDS));
        }
    }

    private static final class Blocked implements AutoCloseable {
        final CountDownLatch entered = new CountDownLatch(1);
        final CountDownLatch release = new CountDownLatch(1);
        final CountDownLatch readStarted = new CountDownLatch(1);
        final AtomicReference<Thread> worker = new AtomicReference<>();
        final ExecutorService executor = Executors.newSingleThreadExecutor();
        final Server server;
        Blocked(boolean body) throws IOException {
            server = new Server(exchange -> {
                if (body) {
                    exchange.sendResponseHeaders(200, 10);
                    exchange.getResponseBody().flush();
                }
                entered.countDown();
                try {
                    release.await();
                } catch (InterruptedException interrupted) {
                    Thread.currentThread().interrupt();
                } finally {
                    exchange.close();
                }
            });
        }
        @Override public void close() throws Exception {
            release.countDown();
            server.close();
            executor.shutdownNow();
            assertTrue(executor.awaitTermination(5, TimeUnit.SECONDS));
        }
    }
}
