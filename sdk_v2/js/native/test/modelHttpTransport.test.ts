// Native integration coverage intentionally loads the local build, never rewrites generated prebuilds.
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import { resolve } from "node:path";
import { setImmediate } from "node:timers/promises";
import { fileURLToPath } from "node:url";
import { Worker } from "node:worker_threads";

import { afterAll, beforeAll, describe, expect, it } from "vitest";

import {
  type NativeAddon,
  type NativeCatalog,
  type NativeManager,
  type NativeModelHttpTransportEvent,
  configureNativeLoader,
} from "../../src/detail/native.js";

const root = fileURLToPath(new URL("../../", import.meta.url));
const build = resolve(root, "build");
const platform = process.platform === "win32" ? "Windows" : process.platform === "darwin" ? "macOS" : "Linux";
const runtime =
  process.env.FOUNDRY_LOCAL_TEST_LIBRARY_PATH ?? resolve(root, "../cpp/build", platform, "RelWithDebInfo/bin");
const require = createRequire(import.meta.url);

describe("model HTTP native transport bridge", () => {
  let addon: NativeAddon;
  let manager: NativeManager;
  let catalog: NativeCatalog;
  let directory: string;
  const events: NativeModelHttpTransportEvent[] = [];
  const offsets = new Map<number, number>();
  const response = Uint8Array.from([0x61, 0x62, 0x63, 0x64, 0x65, 0x66]);

  beforeAll(async () => {
    mkdirSync(build, { recursive: true });
    directory = mkdtempSync(resolve(build, "model-http-transport-"));
    configureNativeLoader({ libraryPath: runtime });
    addon = require(resolve(build, "Release/foundry_local_node.node")) as NativeAddon;
    manager = new addon.Manager({
      appName: "native-model-http-transport-tests",
      appDataDir: directory,
      logsDir: resolve(directory, "logs"),
      modelCacheDir: resolve(directory, "models"),
      disableNonessentialTelemetry: true,
      modelHttpTransportCallback(event) {
        events.push(event);
        switch (event.type) {
          case "create":
            offsets.set(event.operationId, 0);
            return undefined;
          case "send":
            // HTTP errors must still succeed at the callback layer and stream their complete response body.
            return Promise.resolve({
              statusCode: 403,
              headers: [
                ["content-length", new TextEncoder().encode("6")],
                ["x-bytes", Uint8Array.from([0x80, 0xff])],
                ["x-bytes", new TextEncoder().encode("second")],
              ],
            });
          case "read": {
            const offset = offsets.get(event.operationId) ?? 0;
            const end = Math.min(offset + 2, offset + event.capacity, response.length);
            offsets.set(event.operationId, end);
            return Promise.resolve(response.slice(offset, end));
          }
          case "close":
            offsets.delete(event.operationId);
            return undefined;
          case "cancel":
            return undefined;
        }
      },
    });
    expect(events).toEqual([]);
    catalog = await manager.getCatalogAsync();
    expect(events).toEqual([]);
  });

  afterAll(() => {
    manager?.dispose();
    if (directory) rmSync(directory, { recursive: true, force: true });
  });

  it("rejects every network-capable synchronous catalog entry before invoking transport code", () => {
    const invalidUsage = { name: "FoundryLocalError", code: 4 };
    expect(() => manager.getCatalog()).toThrowError(expect.objectContaining(invalidUsage));
    for (const invoke of [
      () => manager.startWebService(),
      () => manager.stopWebService(),
      () => manager.shutdown(),
      () => catalog.getModels(),
      () => catalog.getCachedModels(),
      () => catalog.getLoadedModels(),
      () => catalog.getModel("missing"),
      () => catalog.getModelVariant("missing:1"),
      () => Reflect.apply(catalog.getLatestVersion, catalog, []),
    ]) {
      expect(invoke).toThrowError(expect.objectContaining(invalidUsage));
    }
    expect(events).toEqual([]);
  });

  it("streams short reads and EOF, preserves HTTP errors, and closes operation state", async () => {
    expect(await catalog.getModelsAsync()).toEqual([]);
    await setImmediate();
    const create = events.find((event) => event.type === "create");
    expect(create?.type).toBe("create");
    if (create?.type !== "create") throw new Error("Missing HTTP create event");
    expect(create.request.method).toBe("POST");
    expect(create.request.url).toMatch(/^https:\/\//);
    expect(create.request.timeoutMs).toBe(30_000);
    expect(new TextDecoder().decode(create.request.body)).toContain('"filters"');
    expect(create.request.headers).toContainEqual(["content-type", new TextEncoder().encode("application/json")]);
    const operationEvents = events.filter((event) => event.operationId === create.operationId);
    expect(operationEvents.map((event) => event.type)).toEqual([
      "create",
      "send",
      "read",
      "read",
      "read",
      "read",
      "close",
    ]);
    expect(offsets.size).toBe(0);
  });

  it("exposes worker-backed cached, loaded, alias, and variant lookups", async () => {
    expect(await catalog.getCachedModelsAsync()).toEqual([]);
    expect(await catalog.getLoadedModelsAsync()).toEqual([]);
    expect(await catalog.getModelAsync("missing")).toBeUndefined();
    expect(await catalog.getModelVariantAsync("missing")).toBeUndefined();
  });

  it("allows local/exact models to reach native validation in all session constructors", async () => {
    const local = await manager.getCatalogAsync(1);
    const assets = resolve(directory, "local-assets");
    mkdirSync(assets);
    writeFileSync(resolve(assets, "genai_config.json"), '{"model":{"type":"phi3","context_length":4096}}');
    const initialEvents = events.length;
    for (const [task, Session] of [
      ["chat-completion", addon.ChatSession],
      ["embeddings", addon.EmbeddingsSession],
      ["automatic-speech-recognition", addon.AudioSession],
    ] as const) {
      const metadata = new addon.ModelInfo().setStringProperty("task", task);
      const id = `transport-local-${task}:1`;
      try {
        const registered = await local.registerModel(assets, id, metadata);
        const exact = await local.getModelVariantAsync(id);
        if (!exact) throw new Error(`Missing registered model ${id}`);
        for (const model of [registered, exact]) {
          // These intentionally unloaded models must reach the native precondition, not the transport guard.
          expect(() => new addon.Session(model)).toThrow("model must be loaded before creating a session");
          expect(() => new Session(model)).toThrow("model must be loaded before creating a session");
        }
      } finally {
        metadata.dispose();
        await local.unregisterModel(id);
      }
    }
    expect(events.length).toBe(initialEvents);
  });

  it("constructs and disposes sessions for a real loaded local model without invoking the transport", async () => {
    const local = await manager.getCatalogAsync(1);
    const metadata = new addon.ModelInfo().setStringProperty("task", "chat-completion");
    const id = "transport-tiny-local:1";
    const assets = resolve(root, "../cpp/test/testdata/tiny-paged-attention");
    const model = await local.registerModel(assets, id, metadata);
    const initialEvents = events.length;
    try {
      await model.load();
      const exact = await local.getModelVariantAsync(id);
      if (!exact) throw new Error(`Missing registered model ${id}`);
      for (const input of [model, exact]) {
        const generic = new addon.Session(input);
        const chat = new addon.ChatSession(input);
        expect(generic.isDisposed()).toBe(false);
        expect(chat.isDisposed()).toBe(false);
        generic.dispose();
        generic.dispose();
        chat.dispose();
        chat.dispose();
        expect(generic.isDisposed()).toBe(true);
        expect(chat.isDisposed()).toBe(true);
      }
      expect(events.length).toBe(initialEvents);
    } finally {
      await model.unload();
      await local.unregisterModel(id);
      metadata.dispose();
    }
  });

  it("keeps a native lease during admitted work but rejects completion after explicit disposal", async () => {
    let workerStarted = false;
    const pending = manager.getCatalogAsync(undefined, undefined, () => {
      workerStarted = true;
      manager.dispose();
    });
    await expect(pending).rejects.toMatchObject({ name: "FoundryLocalError", code: 4 });
    expect(workerStarted).toBe(true);
    manager.dispose();
    expect(manager.isDisposed()).toBe(true);
    expect(() => catalog.getModelsAsync()).toThrowError(
      expect.objectContaining({ name: "FoundryLocalError", code: 4 }),
    );
  });

  it("rejects remote default-variant resolution but permits exact remote handles", async () => {
    manager.dispose();
    const remoteDirectory = mkdtempSync(resolve(build, "model-http-remote-"));
    const body = new TextEncoder().encode(
      JSON.stringify({
        totalCount: 1,
        continuationToken: "",
        summaries: [
          {
            assetId: "azureml://registries/azureml/models/transport-remote-generic-cpu/versions/1",
            name: "transport-remote-generic-cpu",
            alias: "transport-remote",
            version: "1",
            foundryLocal: "",
            variantInformation: {
              parents: [{ assetId: "azureml://registries/azureml/models/transport-remote/versions/1" }],
              variantMetadata: { modelType: "ONNX", device: "cpu", executionProvider: "CPUExecutionProvider" },
            },
          },
        ],
      }),
    );
    const readOffsets = new Map<number, number>();
    let callbacks = 0;
    const remote = new addon.Manager({
      appName: "native-model-http-remote-handle-tests",
      appDataDir: remoteDirectory,
      logsDir: resolve(remoteDirectory, "logs"),
      modelCacheDir: resolve(remoteDirectory, "models"),
      disableNonessentialTelemetry: true,
      modelHttpTransportCallback(event) {
        ++callbacks;
        if (event.type === "create") readOffsets.set(event.operationId, 0);
        if (event.type === "send") return Promise.resolve({ statusCode: 200, headers: [] });
        if (event.type === "read") {
          const offset = readOffsets.get(event.operationId) ?? 0;
          const bytes = body.slice(offset, offset + event.capacity);
          readOffsets.set(event.operationId, offset + bytes.length);
          return Promise.resolve(bytes);
        }
        if (event.type === "close") readOffsets.delete(event.operationId);
        return undefined;
      },
    });
    try {
      const remoteCatalog = await remote.getCatalogAsync();
      const models = await remoteCatalog.getModelsAsync();
      expect(models).toHaveLength(1);
      const [container] = models;
      if (!container) throw new Error("Missing remote model");
      const variants = container.getVariants();
      expect(variants).toHaveLength(1);
      const [exact] = variants;
      if (!exact) throw new Error("Missing exact remote variant");
      await setImmediate();
      const initialCallbacks = callbacks;
      for (const Session of [addon.Session, addon.ChatSession, addon.EmbeddingsSession, addon.AudioSession]) {
        expect(() => new Session(container)).toThrow(
          "JavaScript model HTTP transports require an async SDK entry point",
        );
        expect(() => new Session(exact)).toThrow("model must be loaded before creating a session");
      }
      expect(callbacks).toBe(initialCallbacks);
    } finally {
      remote.dispose();
      rmSync(remoteDirectory, { recursive: true, force: true });
    }
  });

  it.each(["create", "send", "read"])("terminates an environment during pending %s", async (block) => {
    manager.dispose();
    const workerDirectory = mkdtempSync(resolve(build, "model-http-worker-"));
    const libraries =
      process.platform === "win32"
        ? ["onnxruntime.dll", "onnxruntime-genai.dll", "foundry_local.dll"]
        : process.platform === "darwin"
          ? ["libonnxruntime.dylib", "libonnxruntime-genai.dylib", "libfoundry_local.dylib"]
          : ["libonnxruntime.so", "libonnxruntime-genai.so", "libfoundry_local.so"];
    const worker = new Worker(new URL("./modelHttpTransportWorker.mjs", import.meta.url), {
      workerData: {
        addon: resolve(build, "Release/foundry_local_node.node"),
        preload: resolve(build, "Release/foundry_local_preload.node"),
        runtime,
        directory: workerDirectory,
        libraries,
        block,
        // More admissions than libuv threads exercises both in-flight and not-yet-executing workers.
        operations: 8,
      },
    });
    try {
      const message = await new Promise<string>((resolve, reject) => {
        worker.once("message", resolve);
        worker.once("error", reject);
        worker.once("exit", (code) => reject(new Error(`Worker exited before blocked operation: ${code}`)));
      });
      expect(message).toBe("blocked");
      expect(await worker.terminate()).toBe(1);
    } finally {
      await worker.terminate();
      rmSync(workerDirectory, { recursive: true, force: true });
    }
  });

  it.each(["close", "cancel"])(
    "consumes rejected %s notifications without an unhandled rejection",
    async (notification) => {
      manager.dispose();
      const workerDirectory = mkdtempSync(resolve(build, "model-http-rejection-"));
      const libraries =
        process.platform === "win32"
          ? ["onnxruntime.dll", "onnxruntime-genai.dll", "foundry_local.dll"]
          : process.platform === "darwin"
            ? ["libonnxruntime.dylib", "libonnxruntime-genai.dylib", "libfoundry_local.dylib"]
            : ["libonnxruntime.so", "libonnxruntime-genai.so", "libfoundry_local.so"];
      const worker = new Worker(new URL("./modelHttpTransportWorker.mjs", import.meta.url), {
        workerData: {
          addon: resolve(build, "Release/foundry_local_node.node"),
          preload: resolve(build, "Release/foundry_local_preload.node"),
          runtime,
          directory: workerDirectory,
          libraries,
          rejectNotifications: notification,
        },
      });
      try {
        const result = await new Promise<{ events: string[]; exitCode: number }>((resolve, reject) => {
          let workerEvents: string[] = [];
          worker.once("message", (value: string[]) => {
            workerEvents = value;
          });
          worker.once("error", reject);
          worker.once("exit", (exitCode) => resolve({ events: workerEvents, exitCode }));
        });
        expect(result.exitCode).toBe(0);
        expect(result.events.filter((event) => event === notification)).toEqual([notification]);
        if (notification === "cancel") expect(result.events).toContain("late-settlement");
        expect(result.events.filter((event) => event === "close")).toHaveLength(
          result.events.filter((event) => event === "create").length,
        );
      } finally {
        await worker.terminate();
        rmSync(workerDirectory, { recursive: true, force: true });
      }
    },
    45_000,
  );
});
