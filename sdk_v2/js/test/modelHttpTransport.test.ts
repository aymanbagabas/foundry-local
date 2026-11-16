import { execFile } from "node:child_process";
import { once } from "node:events";
import { mkdirSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import * as http from "node:http";
import { resolve } from "node:path";
import { setImmediate } from "node:timers/promises";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";

import { afterEach, describe, expect, it } from "vitest";

import {
  CatalogType,
  type FoundryLocalConfig,
  FoundryLocalManager,
  type ModelHttpRequest,
  type ModelHttpTransport,
  NodeModelHttpTransport,
} from "../src/index.js";

const root = fileURLToPath(new URL("../", import.meta.url));
const summaries = JSON.stringify({
  totalCount: 1,
  continuationToken: "",
  summaries: [
    {
      assetId: "azureml://registries/test/models/transport-model-cpu/versions/1",
      name: "transport-model-cpu",
      alias: "transport-model",
      version: "1",
      foundryLocal: "",
      variantInformation: {
        parents: [{ assetId: "azureml://registries/test/models/transport-model/versions/1" }],
        variantMetadata: { modelType: "ONNX", device: "cpu", executionProvider: "CPUExecutionProvider" },
      },
    },
  ],
});

describe("public model HTTP transport registration", () => {
  let manager: FoundryLocalManager | undefined;
  const directories: string[] = [];

  function config(transport: ModelHttpTransport): FoundryLocalConfig {
    mkdirSync(resolve(root, "build"), { recursive: true });
    const directory = mkdtempSync(resolve(root, "build/public-http-transport-"));
    directories.push(directory);
    return {
      appName: "public-js-model-http-transport",
      appDataDir: directory,
      logsDir: resolve(directory, "logs"),
      modelCacheDir: resolve(directory, "models"),
      disableNonessentialTelemetry: true,
      modelHttpTransport: transport,
    };
  }

  afterEach(async () => {
    manager?.dispose();
    manager?.dispose();
    manager = undefined;
    await setImmediate();
    for (const directory of directories.splice(0)) rmSync(directory, { recursive: true, force: true });
  });

  it("roots a client-neutral transport, streams partial reads, and leaves the supplied configuration unchanged", async () => {
    const events: string[] = [];
    const requests: ModelHttpRequest[] = [];
    const body = new TextEncoder().encode(summaries);
    const transport: ModelHttpTransport = {
      createOperation(request) {
        requests.push(request);
        events.push("create");
        let offset = 0;
        return {
          async send() {
            await setImmediate();
            events.push("send");
            return { statusCode: 200, headers: [["content-type", Buffer.from("application/json")]] };
          },
          async read(capacity) {
            events.push("read");
            const chunk = body.slice(offset, offset + Math.min(capacity, 3));
            offset += chunk.length;
            return chunk;
          },
          cancel() {
            events.push("cancel");
          },
          close() {
            events.push("close");
          },
        };
      },
    };
    const supplied = Object.freeze(config(transport));
    const snapshot = { ...supplied };
    manager = new FoundryLocalManager(supplied);
    expect(events).toEqual([]);
    const [catalog, same] = await Promise.all([manager.getCatalogAsync(), manager.getCatalogAsync()]);
    expect(catalog).toBe(same);
    expect(events).toEqual([]);
    let turns = 0;
    const timer = setInterval(() => ++turns, 1);
    try {
      const models = await catalog.getModels();
      expect(models).toHaveLength(1);
      expect(models[0]?.alias).toBe("transport-model");
      expect(models[0]?.variants[0]?.id).toBe("transport-model-cpu:1");
    } finally {
      clearInterval(timer);
    }
    await setImmediate();
    expect(turns).toBeGreaterThan(0);
    expect(events[0]).toBe("create");
    expect(events[1]).toBe("send");
    expect(events.at(-1)).toBe("close");
    expect(events.filter((event) => event === "close")).toHaveLength(1);
    expect(events.filter((event) => event === "read")).toHaveLength(Math.ceil(body.length / 3) + 1);
    expect(requests[0]?.method).toBe("POST");
    expect(requests[0]?.signal.aborted).toBe(true);
    expect(supplied).toEqual(snapshot);
    expect(supplied.modelHttpTransport).toBe(transport);
    expect(() => manager?.catalog).toThrowError(expect.objectContaining({ name: "FoundryLocalError", code: 4 }));
    const count = events.length;
    await expect(manager.getCatalogAsync(CatalogType.AzureRegistry, { name: "unsupported" })).rejects.toMatchObject({
      name: "FoundryLocalError",
      code: 1,
    });
    expect(events.length).toBe(count);
    manager.dispose();
    expect(() => catalog.name).toThrowError(expect.objectContaining({ name: "FoundryLocalError", code: 4 }));
    await expect(catalog.getModels()).rejects.toMatchObject({ name: "FoundryLocalError", code: 4 });
    expect(events.length).toBe(count);
  });

  it("downloads through the real Node adapter and cancels Model.download(signal) at a progress checkpoint", async () => {
    const paths: string[] = [];
    const ranges: (string | string[] | undefined)[] = [];
    const server = http.createServer(async (request, response) => {
      for await (const _chunk of request) {
        // Consume POST request bytes before responding.
      }
      const path = request.url ?? "";
      paths.push(`${request.method} ${path}`);
      let payload = summaries;
      let contentType = "application/json";
      if (path.includes("comp=list")) {
        contentType = "application/xml";
        payload =
          '<EnumerationResults ServiceEndpoint="https://test.blob.core.windows.net/" ContainerName="container">' +
          "<Blobs><Blob><Name>blob.bin</Name><Properties>" +
          "<Last-Modified>Wed, 14 Oct 2026 00:00:00 GMT</Last-Modified><Etag>test-etag</Etag>" +
          "<Content-Length>32</Content-Length><BlobType>BlockBlob</BlobType></Properties></Blob></Blobs>" +
          "<NextMarker /></EnumerationResults>";
      } else if (path.includes("modelregistry")) {
        payload = '{"blobSasUri":"https://test.blob.core.windows.net/container?sig=test%2F"}';
      } else if (path.includes("/container/blob.bin")) {
        const head = request.method === "HEAD";
        const headers = {
          etag: '"test-etag"',
          "last-modified": "Wed, 14 Oct 2026 00:00:00 GMT",
          "x-ms-creation-time": "Wed, 14 Oct 2026 00:00:00 GMT",
          "x-ms-server-encrypted": "true",
          "x-ms-blob-type": "BlockBlob",
          "content-length": 32,
        };
        if (head) response.writeHead(200, headers).end();
        else {
          ranges.push(request.headers["x-ms-range"]);
          response
            .writeHead(206, { ...headers, "content-range": "bytes 0-31/32", "content-md5": "SaaDheEjzuynJH8eW6AEpQ==" })
            .end("*".repeat(32));
        }
        return;
      }
      response.writeHead(200, { "content-type": contentType, "content-length": Buffer.byteLength(payload) });
      response.end(payload);
    });
    server.listen(0, "127.0.0.1");
    await once(server, "listening");
    const address = server.address();
    if (address === null || typeof address === "string") throw new Error("Missing HTTP address");
    const agent = new http.Agent({ keepAlive: true });
    const adapter = new NodeModelHttpTransport({
      request(url, options, listener) {
        return http.request(
          new URL(`http://127.0.0.1:${address.port}${url.pathname}${url.search}`),
          { ...options, agent },
          listener,
        );
      },
    });
    try {
      manager = new FoundryLocalManager(config(adapter));
      const catalog = await manager.getCatalogAsync();
      const models = await catalog.getModels();
      expect(models).toHaveLength(1);
      const model = models[0]?.variants[0];
      if (model === undefined) throw new Error("Missing exact model variant");
      await model.download();
      expect(paths.some((path) => path.startsWith("POST "))).toBe(true);
      expect(paths.some((path) => path.includes("comp=list"))).toBe(true);
      expect(paths.some((path) => path.startsWith("HEAD "))).toBe(true);
      expect(ranges).toEqual(["bytes=0-31"]);
      expect(model.isCached).toBe(true);
      expect(readFileSync(resolve(model.path, "blob.bin"), "utf8")).toBe("*".repeat(32));
      model.removeFromCache();
      expect(model.isCached).toBe(false);
      const controller = new AbortController();
      await expect(model.download(() => controller.abort(), controller.signal)).rejects.toMatchObject({
        name: "AbortError",
      });
      const progressError = new Error("application progress failure");
      await expect(
        model.download(() => {
          throw progressError;
        }),
      ).rejects.toBe(progressError);
    } finally {
      manager?.dispose();
      agent.destroy();
      server.closeAllConnections();
      await new Promise<void>((resolve) => server.close(() => resolve()));
    }
  });

  it("executes the documented example through the public SDK and native catalog", async () => {
    const result = await promisify(execFile)(process.execPath, ["examples/modelHttpTransport.mjs"], {
      cwd: root,
      timeout: 15_000,
    });
    expect(result.stderr).toBe("");
    expect(result.stdout).toContain("Native Node HTTP transport: 1 loopback catalog request(s), 1 model (example)");
    expect(result.stdout).toContain("Blocked body read cancelled with AbortSignal");
  });

  it.each(["create", "send", "read", "oversized"])(
    "contains %s failures, safely releases operations and never exposes callback credentials",
    async (failure) => {
      let acquisition = false;
      let allocated = 0;
      let released = 0;
      let duplicateReleases = 0;
      const body = Buffer.from(summaries);
      const secret = "secret-token-must-not-escape";
      manager = new FoundryLocalManager(
        config({
          createOperation() {
            if (acquisition && failure === "create") throw new Error(`https://private.invalid/?sig=${secret}`);
            ++allocated;
            let offset = 0;
            let closed = false;
            return {
              async send() {
                if (acquisition && failure === "send") throw new Error(`https://private.invalid/?sig=${secret}`);
                return { statusCode: 200, headers: [] };
              },
              async read(capacity) {
                if (acquisition) {
                  if (failure === "oversized") return new Uint8Array(capacity + 1);
                  throw new Error(`https://private.invalid/?sig=${secret}`);
                }
                const bytes = body.subarray(offset, offset + capacity);
                offset += bytes.length;
                return bytes;
              },
              cancel() {},
              close() {
                if (closed) ++duplicateReleases;
                closed = true;
                ++released;
              },
            };
          },
        }),
      );
      const catalog = await manager.getCatalogAsync();
      const models = await catalog.getModels();
      const model = models[0]?.variants[0];
      if (model === undefined) throw new Error("Missing model");
      acquisition = true;
      const error: unknown = await model.download().then(
        () => undefined,
        (error: unknown) => error,
      );
      expect(error).toMatchObject({ name: "FoundryLocalError", code: 6 });
      expect(String(error)).not.toContain(secret);
      await setImmediate();
      expect(released).toBe(allocated);
      expect(duplicateReleases).toBe(0);
    },
  );
});
