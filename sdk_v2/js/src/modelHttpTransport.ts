// Copyright (c) Microsoft Corporation. All rights reserved.
// Licensed under the MIT License.

/** Duplicate-preserving HTTP field. Values are field bytes, not UTF-8 text; NUL, CR and LF are forbidden. */
export type ModelHttpHeader = readonly [name: string, value: Uint8Array];

/** Owned request snapshot. Do not modify its buffers. Creation must not initiate network I/O. */
export interface ModelHttpRequest {
  readonly method: "GET" | "HEAD" | "POST";
  readonly url: string;
  readonly headers: readonly ModelHttpHeader[];
  readonly body: Uint8Array;
  /** Deadline for send and all reads, measured from send entry. Zero means no deadline. */
  readonly timeoutMs: number;
  /** Aborted on cancellation, deadline expiry, or release of an unfinished operation. */
  readonly signal: AbortSignal;
}

/** HTTP errors (including 3xx, 4xx and 5xx) are successful responses, not transport failures. */
export interface ModelHttpResponseHead {
  readonly statusCode: number;
  readonly headers: readonly ModelHttpHeader[];
}

/** One request. Methods run on the JS thread; native workers await their results without blocking the event loop. */
export interface ModelHttpOperation {
  /** Send once, resolving at response headers without buffering the body. */
  send(): Promise<ModelHttpResponseHead>;
  /** Serial reads, each at most capacity bytes. Short reads are valid; zero bytes means permanent EOF. */
  read(capacity: number): Promise<Uint8Array>;
  /** Prompt, nonblocking, idempotent interruption, including pending send/read. Must not throw. */
  cancel(): void;
  /** Prompt, nonblocking, idempotent resource release. Called once by the SDK, even after failure. Must not throw. */
  close(): void;
}

/**
 * Application-owned model acquisition HTTP policy. Covers public catalog, registry and blob requests, not inference,
 * telemetry or execution-provider downloads. Azure Registry catalogs are currently unsupported.
 *
 * Implementations must preserve URLs, duplicate headers and raw payload bytes; do not retry, follow redirects or
 * decompress. Enforce timeoutMs across send and reads, and interrupt pending I/O on cancellation. Never invoke manager
 * disposal or recursive model acquisition from a callback. Keep error messages free of credentials and signed URLs.
 */
export interface ModelHttpTransport {
  /** Allocate only: no I/O, no awaiting network activity. */
  createOperation(request: ModelHttpRequest): ModelHttpOperation;
}
