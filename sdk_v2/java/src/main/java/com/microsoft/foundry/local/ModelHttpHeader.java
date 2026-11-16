// Copyright (c) Microsoft Corporation. Licensed under the MIT License.
package com.microsoft.foundry.local;

import java.util.Objects;

/** One HTTP field. Values represent ISO-8859-1 bytes, not UTF-8. Duplicate fields remain separate. */
public record ModelHttpHeader(String name, String value) {
    public ModelHttpHeader {
        Objects.requireNonNull(name, "name");
        Objects.requireNonNull(value, "value");
        if (name.isEmpty() || !name.chars().allMatch(c ->
                c >= '0' && c <= '9' || c >= 'A' && c <= 'Z' || c >= 'a' && c <= 'z'
                        || "!#$%&'*+-.^_`|~".indexOf(c) >= 0)) {
            throw new IllegalArgumentException("Invalid HTTP header name");
        }
        if (!value.chars().allMatch(c -> c == '\t' || c >= 32 && c != 127 && c <= 255)) {
            throw new IllegalArgumentException("Invalid HTTP header value");
        }
    }
}
