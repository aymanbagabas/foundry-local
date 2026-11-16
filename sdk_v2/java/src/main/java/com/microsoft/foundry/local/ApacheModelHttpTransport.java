// Copyright (c) Microsoft Corporation. Licensed under the MIT License.
package com.microsoft.foundry.local;

import java.io.IOException;
import java.io.InputStream;
import java.io.InterruptedIOException;
import java.net.URI;
import java.util.ArrayList;
import java.util.Objects;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ScheduledFuture;
import java.util.concurrent.ScheduledThreadPoolExecutor;
import java.util.concurrent.TimeUnit;
import javax.net.ssl.SSLContext;
import org.apache.hc.client5.http.DnsResolver;
import org.apache.hc.client5.http.classic.methods.HttpUriRequestBase;
import org.apache.hc.client5.http.config.ConnectionConfig;
import org.apache.hc.client5.http.config.RequestConfig;
import org.apache.hc.client5.http.impl.classic.CloseableHttpClient;
import org.apache.hc.client5.http.impl.classic.HttpClients;
import org.apache.hc.client5.http.impl.io.PoolingHttpClientConnectionManagerBuilder;
import org.apache.hc.client5.http.ssl.ClientTlsStrategyBuilder;
import org.apache.hc.core5.http.ClassicHttpResponse;
import org.apache.hc.core5.http.Header;
import org.apache.hc.core5.http.HttpHost;
import org.apache.hc.core5.http.io.entity.ByteArrayEntity;
import org.apache.hc.core5.http.io.SocketConfig;
import org.apache.hc.core5.io.CloseMode;
import org.apache.hc.core5.io.ModalCloseable;
import org.apache.hc.core5.util.Timeout;

/**
 * Optional Apache HttpClient 5 adapter. Applications must add the optional httpclient5 dependency.
 * The transport owns its connection pool; close it after all managers that use it close.
 * DNS resolvers must return promptly; JVM name resolution cannot be interrupted by socket cancellation.
 */
public final class ApacheModelHttpTransport implements ModelHttpTransport, AutoCloseable {
    private static final ScheduledThreadPoolExecutor DEADLINES = deadlines();
    private final CloseableHttpClient client;
    private final Set<Operation> operations = ConcurrentHashMap.newKeySet();
    private boolean closed;

    private ApacheModelHttpTransport(Builder builder) {
        var connections = PoolingHttpClientConnectionManagerBuilder.create()
                .setDefaultConnectionConfig(builder.connectionConfig)
                .setDefaultSocketConfig(builder.socketConfig)
                .setMaxConnTotal(builder.maxConnections)
                .setMaxConnPerRoute(builder.maxConnectionsPerRoute);
        if (builder.sslContext != null) {
            connections.setTlsSocketStrategy(ClientTlsStrategyBuilder.create()
                    .setSslContext(builder.sslContext).buildClassic());
        }
        if (builder.dnsResolver != null) connections.setDnsResolver(builder.dnsResolver);
        // No arbitrary HttpClientBuilder hook: interceptors and execution chains can bypass these guarantees.
        var http = HttpClients.custom().setConnectionManager(connections.build())
                .disableAutomaticRetries().disableRedirectHandling().disableContentCompression()
                .disableCookieManagement().disableAuthCaching()
                .setDefaultRequestConfig(RequestConfig.custom()
                        .setConnectionRequestTimeout(builder.connectionRequestTimeout)
                        .setResponseTimeout(Timeout.DISABLED)
                        .setAuthenticationEnabled(false)
                        .setRedirectsEnabled(false)
                        .setContentCompressionEnabled(false)
                        .setExpectContinueEnabled(false)
                        .setProtocolUpgradeEnabled(false)
                        .setHardCancellationEnabled(true).build());
        if (builder.proxy != null) http.setProxy(builder.proxy);
        client = http.build();
    }

    public static Builder builder() { return new Builder(); }

    /** Safe network settings. Protocol policy cannot be replaced through this builder. */
    public static final class Builder {
        private SSLContext sslContext;
        private HttpHost proxy;
        private DnsResolver dnsResolver;
        private ConnectionConfig connectionConfig = ConnectionConfig.custom()
                .setConnectTimeout(Timeout.DISABLED).setSocketTimeout(Timeout.DISABLED).build();
        private SocketConfig socketConfig = SocketConfig.custom().setSoTimeout(Timeout.DISABLED).build();
        private Timeout connectionRequestTimeout = Timeout.DISABLED;
        private int maxConnections = 25;
        private int maxConnectionsPerRoute = 5;

        /** Set application trust and key material. Standard hostname and certificate checks remain enabled. */
        public Builder sslContext(SSLContext value) { sslContext = Objects.requireNonNull(value); return this; }
        public Builder proxy(HttpHost value) { proxy = Objects.requireNonNull(value); return this; }
        public Builder dnsResolver(DnsResolver value) { dnsResolver = Objects.requireNonNull(value); return this; }
        public Builder connectionConfig(ConnectionConfig value) {
            connectionConfig = Objects.requireNonNull(value);
            return this;
        }
        public Builder socketConfig(SocketConfig value) {
            socketConfig = Objects.requireNonNull(value);
            return this;
        }
        public Builder connectionRequestTimeout(Timeout value) {
            connectionRequestTimeout = Objects.requireNonNull(value);
            return this;
        }
        public Builder maxConnections(int total, int perRoute) {
            if (total <= 0 || perRoute <= 0) throw new IllegalArgumentException("Connection limits must be positive");
            maxConnections = total;
            maxConnectionsPerRoute = perRoute;
            return this;
        }
        public ApacheModelHttpTransport build() { return new ApacheModelHttpTransport(this); }
    }

    @Override public synchronized ModelHttpOperation createOperation(ModelHttpRequest request) throws IOException {
        if (closed) throw new IOException("Model HTTP transport is closed");
        Operation operation = new Operation(Objects.requireNonNull(request));
        operations.add(operation);
        return operation;
    }

    @Override public synchronized void close() throws IOException {
        if (closed) return;
        closed = true;
        for (Operation operation : operations) operation.cancel();
        client.close();
    }

    private static ScheduledThreadPoolExecutor deadlines() {
        var executor = new ScheduledThreadPoolExecutor(1, action -> {
            Thread thread = new Thread(action, "foundry-model-http-deadlines");
            thread.setDaemon(true);
            return thread;
        });
        executor.setRemoveOnCancelPolicy(true);
        return executor;
    }

    private final class Operation implements ModelHttpOperation {
        private final ModelHttpRequest source;
        private final HttpUriRequestBase request;
        private ClassicHttpResponse response;
        private InputStream body;
        private ScheduledFuture<?> deadline;
        private volatile boolean cancelled;
        private volatile boolean timedOut;
        private boolean sent;
        private boolean released;

        Operation(ModelHttpRequest source) {
            this.source = source;
            request = new HttpUriRequestBase(source.method(), URI.create(source.url()));
            byte[] bytes = source.body();
            boolean length = false, chunked = false;
            for (ModelHttpHeader header : source.headers()) {
                // Apache owns wire framing. Validate supplied framing, then let RequestContent emit the same value.
                if (header.name().equalsIgnoreCase("Content-Length")) {
                    if (length || !Integer.toString(bytes.length).equals(header.value())) {
                        throw new IllegalArgumentException("Content-Length must match the request body");
                    }
                    length = true;
                } else if (header.name().equalsIgnoreCase("Transfer-Encoding")) {
                    if (chunked || !header.value().equalsIgnoreCase("chunked")) {
                        throw new IllegalArgumentException("Only chunked request transfer encoding is supported");
                    }
                    chunked = true;
                } else {
                    request.addHeader(header.name(), header.value());
                }
            }
            if (length && chunked) throw new IllegalArgumentException("Conflicting HTTP request framing");
            if ("POST".equals(source.method()) || bytes.length != 0 || length || chunked) {
                request.setEntity(new ByteArrayEntity(bytes, null, chunked));
            }
        }

        private void check() throws IOException {
            if (cancelled) throw new InterruptedIOException("Model HTTP operation cancelled");
            if (timedOut) throw new IOException("Model HTTP deadline expired");
            if (released) throw new IOException("Model HTTP operation is closed");
        }

        @Override public synchronized ModelHttpResponseHead send() throws IOException {
            check();
            if (sent) throw new IOException("Model HTTP request was already sent");
            sent = true;
            if (source.timeoutMillis() > 0) {
                deadline = DEADLINES.schedule(() -> {
                    timedOut = true;
                    request.cancel();
                }, source.timeoutMillis(), TimeUnit.MILLISECONDS);
            }
            try {
                response = client.executeOpen(null, request, null);
                check();
                body = response.getEntity() == null ? InputStream.nullInputStream() : response.getEntity().getContent();
                var headers = new ArrayList<ModelHttpHeader>();
                for (Header header : response.getHeaders()) {
                    headers.add(new ModelHttpHeader(header.getName(), header.getValue()));
                }
                return new ModelHttpResponseHead(response.getCode(), headers);
            } catch (IOException failure) {
                check();
                throw new IOException("Model HTTP send failed", failure);
            }
        }

        @Override public synchronized int read(byte[] buffer, int offset, int length) throws IOException {
            Objects.checkFromIndexSize(offset, length, buffer.length);
            if (length == 0) throw new IllegalArgumentException("Read length must be positive");
            check();
            if (body == null) throw new IOException("Send must complete before read");
            try {
                int count = body.read(buffer, offset, length);
                check();
                if (count < 0 && deadline != null) deadline.cancel(false);
                return Math.max(0, count);
            } catch (IOException failure) {
                check();
                throw new IOException("Model HTTP read failed", failure);
            }
        }

        @Override public void cancel() {
            cancelled = true;
            // Apache retains this cancellation dependency through streamed body consumption.
            // It closes the active endpoint immediately, without the send/read monitor.
            request.cancel();
        }

        @Override public synchronized void close() throws IOException {
            if (released) return;
            released = true;
            if (deadline != null) deadline.cancel(false);
            request.cancel();
            try {
                if (response instanceof ModalCloseable closeable) closeable.close(CloseMode.IMMEDIATE);
                else if (response != null) response.close();
            } finally {
                operations.remove(this);
            }
        }
    }
}
