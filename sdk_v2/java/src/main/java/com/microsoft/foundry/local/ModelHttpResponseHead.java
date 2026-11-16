// Copyright (c) Microsoft Corporation. Licensed under the MIT License.
package com.microsoft.foundry.local;

import java.util.List;

/** Immutable status and duplicate-preserving response headers. HTTP errors are successful responses. */
public record ModelHttpResponseHead(int statusCode, List<ModelHttpHeader> headers) {
    public ModelHttpResponseHead {
        if (statusCode < 100 || statusCode > 599) throw new IllegalArgumentException("Invalid HTTP status");
        headers = List.copyOf(headers);
    }
}
