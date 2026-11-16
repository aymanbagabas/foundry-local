// Copyright (c) Microsoft Corporation. Licensed under the MIT License.
package com.microsoft.foundry.local;

import java.net.URI;
import java.util.List;
import java.util.Objects;

/** Detached request data. The timeout covers send and all reads; zero means no deadline. */
public final class ModelHttpRequest {
    private final String method;
    private final String url;
    private final List<ModelHttpHeader> headers;
    private final byte[] body;
    private final long timeoutMillis;

    public ModelHttpRequest(String method, String url, List<ModelHttpHeader> headers,
            byte[] body, long timeoutMillis) {
        if (!List.of("GET", "HEAD", "POST").contains(Objects.requireNonNull(method, "method"))) {
            throw new IllegalArgumentException("Only GET, HEAD, and POST are supported");
        }
        URI uri = URI.create(Objects.requireNonNull(url, "url"));
        if (!("http".equalsIgnoreCase(uri.getScheme()) || "https".equalsIgnoreCase(uri.getScheme()))
                || uri.getHost() == null || uri.getRawUserInfo() != null || uri.getRawFragment() != null) {
            throw new IllegalArgumentException("An absolute HTTP(S) URL without user info or fragment is required");
        }
        if (timeoutMillis < 0) throw new IllegalArgumentException("timeoutMillis must be nonnegative");
        this.method = method;
        this.url = url;
        this.headers = List.copyOf(headers);
        this.body = Objects.requireNonNull(body, "body").clone();
        this.timeoutMillis = timeoutMillis;
    }

    public String method() { return method; }
    public String url() { return url; }
    public List<ModelHttpHeader> headers() { return headers; }
    public byte[] body() { return body.clone(); }
    public long timeoutMillis() { return timeoutMillis; }
}
