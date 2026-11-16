// Copyright (c) Microsoft Corporation. All rights reserved.
// Licensed under the MIT License.

import type { ModelHttpOperation, ModelHttpTransport } from "../modelHttpTransport.js";
import type {
  NativeModelHttpTransportCallback,
  NativeModelHttpTransportEvent,
  NativeModelHttpTransportResult,
} from "./native.js";

type Operation = {
  readonly controller: AbortController;
  readonly operation: ModelHttpOperation;
};

/** The native TSFN roots this closure for exactly as long as configuration/manager retains the transport. */
export function createModelHttpTransportCallback(transport: ModelHttpTransport): NativeModelHttpTransportCallback {
  if (transport === null || typeof transport !== "object" || typeof transport.createOperation !== "function") {
    throw new TypeError("modelHttpTransport must implement createOperation");
  }
  const operations = new Map<number, Operation>();
  const creationErrors = new Map<number, unknown>();
  return (event: NativeModelHttpTransportEvent): Promise<NativeModelHttpTransportResult> | undefined => {
    const id = event.operationId;
    if (event.type === "create") {
      try {
        const controller = new AbortController();
        const operation = transport.createOperation(Object.freeze({ ...event.request, signal: controller.signal }));
        if (
          operation === null ||
          typeof operation !== "object" ||
          typeof operation.send !== "function" ||
          typeof operation.read !== "function" ||
          typeof operation.cancel !== "function" ||
          typeof operation.close !== "function"
        ) {
          throw new TypeError("createOperation must return a ModelHttpOperation");
        }
        operations.set(id, { controller, operation });
      } catch (error) {
        creationErrors.set(id, error);
        return Promise.reject(safeError(error));
      }
      return undefined;
    }
    const state = operations.get(id);
    if (event.type === "close") {
      operations.delete(id);
      creationErrors.delete(id);
      if (state !== undefined) {
        try {
          state.controller.abort();
        } finally {
          // Remove state first: late Promise settlements cannot resurrect or reuse a released operation.
          try {
            state.operation.close();
          } catch {
            // Cleanup hooks cannot make a native release fail.
          }
        }
      }
      return undefined;
    }
    if (event.type === "cancel") {
      if (state !== undefined) {
        state.controller.abort();
        try {
          state.operation.cancel();
        } catch {
          // Native waiters are already cancelled; a broken hook must not prevent release.
        }
      }
      return undefined;
    }
    if (state === undefined) {
      return Promise.reject(safeError(creationErrors.get(id)));
    }
    try {
      const result = event.type === "send" ? state.operation.send() : state.operation.read(event.capacity);
      return Promise.resolve(result).catch((error: unknown) => {
        throw safeError(error);
      });
    } catch (error) {
      return Promise.reject(safeError(error));
    }
  };
}

function safeError(error: unknown): Error {
  // User networking errors commonly contain signed URLs. Never propagate their messages across the native boundary.
  return error instanceof Error && error.name === "AbortError"
    ? new DOMException("Model HTTP operation cancelled", "AbortError")
    : new Error("Model HTTP transport failed");
}
