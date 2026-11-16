// Copyright (c) Microsoft Corporation. All rights reserved.
// Licensed under the MIT License.

import * as http from "node:http";
import * as https from "node:https";
import type { LookupFunction } from "node:net";

import type {
  ModelHttpHeader,
  ModelHttpOperation,
  ModelHttpRequest,
  ModelHttpResponseHead,
  ModelHttpTransport,
} from "./modelHttpTransport.js";

/** Optional Node client factory. It must not retry, redirect or decompress; the adapter owns cancellation and deadlines. */
export type NodeModelHttpRequestFactory = (
  url: URL,
  options: http.RequestOptions,
  onResponse: (response: http.IncomingMessage) => void,
) => http.ClientRequest;

/** Agents remain caller-owned: the adapter never mutates their settings or destroys them. */
export type NodeModelHttpTransportOptions = {
  readonly httpAgent?: http.Agent | false;
  /** Configure TLS trust, client certificates, pooling or proxy routing using an application-owned HTTPS Agent. */
  readonly httpsAgent?: https.Agent | false;
  /** Advanced routing/testing boundary. Defaults to node:http or node:https based on the request URL. */
  readonly request?: NodeModelHttpRequestFactory;
  /** Optional application lookup override. By default Node uses the OS resolver and honors the selected Agent. */
  readonly lookup?: LookupFunction;
};

/**
 * Streaming Node HTTP(S) model transport. Uses raw header arrays and paused response reads; never retries, follows
 * redirects or decompresses. Default TLS verification is unchanged. Caller-owned Agents control connection policy.
 */
export class NodeModelHttpTransport implements ModelHttpTransport {
  readonly #options: NodeModelHttpTransportOptions;

  constructor(options: NodeModelHttpTransportOptions = {}) {
    this.#options = Object.freeze({ ...options });
  }

  createOperation(request: ModelHttpRequest): ModelHttpOperation {
    return new NodeModelHttpOperation(request, this.#options);
  }
}

class NodeModelHttpOperation implements ModelHttpOperation {
  readonly #input: ModelHttpRequest;
  readonly #options: NodeModelHttpTransportOptions;
  #request: http.ClientRequest | undefined;
  #response: http.IncomingMessage | undefined;
  #timer: ReturnType<typeof setTimeout> | undefined;
  #error: Error | undefined;
  #sent = false;
  #closed = false;
  #reading = false;
  #wake: (() => void) | undefined;
  #rejectSend: ((error: Error) => void) | undefined;
  readonly #onAbort = (): void => this.cancel();
  readonly #onResponseError = (): void => this.#fail(new Error("Model HTTP response failed"));
  readonly #onResponseAbort = (): void => this.#fail(new Error("Model HTTP response interrupted"));
  readonly #onReadable = (): void => this.#wake?.();
  readonly #onEnd = (): void => {
    this.#clearTimer();
    this.#wake?.();
  };
  readonly #onClose = (): void => {
    if (!this.#response?.complete) this.#fail(new Error("Model HTTP response interrupted"));
    this.#wake?.();
  };
  readonly #onRequestError = (): void => this.#fail(new Error("Model HTTP connection failed"));

  constructor(request: ModelHttpRequest, options: NodeModelHttpTransportOptions) {
    this.#input = request;
    this.#options = options;
  }

  async send(): Promise<ModelHttpResponseHead> {
    if (this.#sent) throw new Error("Model HTTP operation already sent");
    this.#sent = true;
    this.#throwIfStopped();
    const url = new URL(this.#input.url);
    if (url.protocol !== "http:" && url.protocol !== "https:") throw new Error("Unsupported model HTTP protocol");
    if (url.username !== "" || url.password !== "") throw new Error("URL credentials are not supported");
    const target = /^https?:\/\/[^/?#]*([^#]*)$/i.exec(this.#input.url)?.[1];
    if (target === undefined) throw new Error("Model HTTP URL must not contain a fragment");
    if (!Number.isSafeInteger(this.#input.timeoutMs) || this.#input.timeoutMs < 0) {
      throw new TypeError("timeoutMs must be a nonnegative safe integer");
    }
    const headers: string[] = [];
    for (const [name, value] of this.#input.headers) {
      http.validateHeaderName(name);
      const text = Buffer.from(value).toString("latin1");
      if (text.includes("\0") || text.includes("\r") || text.includes("\n")) throw new Error("Invalid HTTP header");
      http.validateHeaderValue(name, text);
      headers.push(name, text);
    }
    // Node does not synthesize Host when headers are supplied as a duplicate-preserving raw array.
    if (!this.#input.headers.some(([name]) => name.toLowerCase() === "host")) headers.push("Host", url.host);
    this.#input.signal.addEventListener("abort", this.#onAbort, { once: true });
    if (this.#input.signal.aborted) this.cancel();
    this.#throwIfStopped();
    if (this.#input.timeoutMs > 0) {
      // Node clamps oversized timer delays to 1ms. Chain bounded timers instead, preserving the original deadline.
      const deadline = performance.now() + this.#input.timeoutMs;
      const arm = (): void => {
        const remaining = deadline - performance.now();
        if (remaining <= 0) {
          this.#fail(new Error("Model HTTP deadline exceeded"));
          return;
        }
        this.#timer = setTimeout(arm, Math.min(remaining, 2_147_483_647));
        this.#timer.unref();
      };
      arm();
    }
    return new Promise<ModelHttpResponseHead>((resolve, reject) => {
      this.#rejectSend = reject;
      const agent = url.protocol === "https:" ? this.#options.httpsAgent : this.#options.httpAgent;
      // URL normalizes dot segments. Override path with the original target so signed paths/queries stay byte-exact.
      const path = target.startsWith("?") ? `/${target}` : target || "/";
      const options: http.RequestOptions = { method: this.#input.method, headers, path };
      if (agent !== undefined) options.agent = agent;
      if (this.#options.lookup !== undefined) options.lookup = this.#options.lookup;
      const factory = this.#options.request ?? (url.protocol === "https:" ? https.request : http.request);
      try {
        const request = factory(url, options, (response) => {
          this.#response = response;
          response.pause();
          response.on("error", this.#onResponseError);
          response.on("aborted", this.#onResponseAbort);
          response.on("readable", this.#onReadable);
          response.on("end", this.#onEnd);
          response.on("close", this.#onClose);
          if (this.#error !== undefined || this.#closed) {
            response.destroy();
            return;
          }
          const responseHeaders: ModelHttpHeader[] = [];
          for (let i = 0; i < response.rawHeaders.length; i += 2) {
            const name = response.rawHeaders[i];
            const value = response.rawHeaders[i + 1];
            if (name !== undefined && value !== undefined) responseHeaders.push([name, Buffer.from(value, "latin1")]);
          }
          this.#rejectSend = undefined;
          resolve({ statusCode: response.statusCode ?? 0, headers: responseHeaders });
        });
        this.#request = request;
        request.on("error", this.#onRequestError);
        if (this.#error !== undefined || this.#closed) request.destroy();
        else request.end(this.#input.body);
      } catch {
        this.#fail(new Error("Model HTTP request failed"));
      }
    });
  }

  async read(capacity: number): Promise<Uint8Array> {
    if (!Number.isSafeInteger(capacity) || capacity <= 0)
      throw new TypeError("capacity must be a positive safe integer");
    if (this.#reading) throw new Error("Concurrent HTTP reads are not supported");
    this.#throwIfStopped();
    const response = this.#response;
    if (response === undefined) throw new Error("Model HTTP read requires a successful send");
    this.#reading = true;
    try {
      for (;;) {
        this.#throwIfStopped();
        // A paused stream emits end only after a read observes the empty terminal buffer.
        response.read(0);
        if (response.readableLength > 0) {
          const chunk: unknown = response.read(Math.min(capacity, response.readableLength));
          if (chunk instanceof Uint8Array && chunk.byteLength > 0) return chunk;
        }
        if (response.readableEnded) return new Uint8Array();
        if (response.destroyed) throw new Error("Model HTTP response interrupted");
        await new Promise<void>((resolve) => {
          this.#wake = resolve;
        });
        this.#wake = undefined;
      }
    } finally {
      this.#reading = false;
    }
  }

  cancel(): void {
    this.#fail(new DOMException("Model HTTP operation cancelled", "AbortError"));
  }

  close(): void {
    if (this.#closed) return;
    this.#closed = true;
    this.#input.signal.removeEventListener("abort", this.#onAbort);
    if (this.#response?.readableEnded) {
      // Completed responses already returned their socket to the caller's Agent. Do not destroy a pooled connection.
      this.#response.off("error", this.#onResponseError);
      this.#response.off("aborted", this.#onResponseAbort);
      this.#response.off("readable", this.#onReadable);
      this.#response.off("end", this.#onEnd);
      this.#response.off("close", this.#onClose);
      this.#request?.off("error", this.#onRequestError);
      this.#request = undefined;
      this.#response = undefined;
      this.#clearTimer();
      return;
    }
    this.#fail(new DOMException("Model HTTP operation closed", "AbortError"));
  }

  #throwIfStopped(): void {
    if (this.#error !== undefined) throw this.#error;
    if (this.#closed || this.#input.signal.aborted) {
      throw new DOMException("Model HTTP operation cancelled", "AbortError");
    }
  }

  #clearTimer(): void {
    clearTimeout(this.#timer);
    this.#timer = undefined;
  }

  #fail(error: Error): void {
    this.#error ??= error;
    this.#clearTimer();
    this.#rejectSend?.(this.#error);
    this.#rejectSend = undefined;
    if (!this.#response?.readableEnded) {
      this.#request?.destroy();
      this.#response?.destroy();
    }
    this.#wake?.();
  }
}
