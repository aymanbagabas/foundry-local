// Copyright (c) Microsoft Corporation. Licensed under the MIT License.
package com.microsoft.foundry.local;

import java.io.IOException;

/**
 * One streamed HTTP exchange. Send and read are serial; cancel can race either method.
 * Implementations must not retry, follow redirects, decompress, or replay authentication.
 * Implementations must enforce the request's total deadline during blocked send and read calls.
 */
public interface ModelHttpOperation extends AutoCloseable {
    ModelHttpResponseHead send() throws IOException;

    /** Read at most length bytes. A partial read is valid; zero means permanent EOF. Do not retain the buffer. */
    int read(byte[] buffer, int offset, int length) throws IOException;

    /** Interrupt blocked I/O promptly. Cancellation is thread-safe, idempotent, and permanent, even before send. */
    void cancel();

    /** Close resources, including operations that never reached send. No other callback runs during close. */
    @Override void close() throws IOException;
}
