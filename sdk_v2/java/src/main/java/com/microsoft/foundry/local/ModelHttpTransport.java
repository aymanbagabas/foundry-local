// Copyright (c) Microsoft Corporation. Licensed under the MIT License.
package com.microsoft.foundry.local;

import java.io.IOException;

/** Client-neutral model acquisition transport. Distinct operations can run concurrently on native workers. */
@FunctionalInterface
public interface ModelHttpTransport {
    /** Allocate local state only. Do not start network I/O or wait for network activity. */
    ModelHttpOperation createOperation(ModelHttpRequest request) throws IOException;
}
