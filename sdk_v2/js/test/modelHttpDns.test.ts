import { execFile } from "node:child_process";
import type { LookupAddress, LookupOptions } from "node:dns";
import { once } from "node:events";
import { mkdirSync, mkdtempSync, rmSync } from "node:fs";
import * as http from "node:http";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";

import { afterAll, beforeAll, describe, expect, it } from "vitest";

import { ModelHttpLookup } from "../src/detail/modelHttpLookup.js";
import { NodeModelHttpTransport } from "../src/index.js";
import { createModelHttpDns } from "./_fixtures/modelHttpDns.js";

const root = fileURLToPath(new URL("../", import.meta.url));

describe("model HTTP DNS and native worker-pool safety", () => {
  let dns: Awaited<ReturnType<typeof createModelHttpDns>>;
  let server: http.Server;
  let port: number;
  const body = JSON.stringify({
    totalCount: 1,
    continuationToken: "",
    summaries: [
      {
        assetId: "azureml://registries/test/models/dns-model-cpu/versions/1",
        name: "dns-model-cpu",
        alias: "dns-model",
        version: "1",
        foundryLocal: "",
        variantInformation: {
          parents: [{ assetId: "azureml://registries/test/models/dns-model/versions/1" }],
          variantMetadata: { modelType: "ONNX", device: "cpu", executionProvider: "CPUExecutionProvider" },
        },
      },
    ],
  });

  beforeAll(async () => {
    dns = await createModelHttpDns();
    server = http.createServer((request, response) => {
      request.resume();
      request.on("end", () => response.writeHead(200).end(body));
    });
    server.listen(0, "127.0.0.1");
    await once(server, "listening");
    const address = server.address();
    if (address === null || typeof address === "string") throw new Error("Missing HTTP address");
    port = address.port;
  });

  afterAll(async () => {
    server.closeAllConnections();
    await new Promise<void>((resolve) => server.close(() => resolve()));
    await dns.close();
  });

  it.each(["1", "4"])("completes eight concurrent native SDK queries with a %s-thread pool", async (pool) => {
    mkdirSync(resolve(root, "build"), { recursive: true });
    const directory = mkdtempSync(resolve(root, "build/parallel-http-dns-"));
    try {
      const result = await promisify(execFile)(
        process.execPath,
        [resolve(root, "native/test/modelHttpDnsWorker.mjs"), directory, String(port), dns.servers[0] ?? ""],
        { timeout: 20_000, env: { ...process.env, UV_THREADPOOL_SIZE: pool } },
      );
      expect(result.stderr).toBe("");
      expect(result.stdout.trim()).toBe(`8 native queries completed with UV_THREADPOOL_SIZE=${pool}`);
      expect(dns.queries.some((query) => query.hostname === "catalog.binding.test")).toBe(true);
    } finally {
      rmSync(directory, { recursive: true, force: true });
    }
  });

  it("supports both address families, all/single results, ordering, partial failure and explicit hint rejection", async () => {
    const resolver = new ModelHttpLookup(dns.servers);
    const lookup = (hostname: string, options: LookupOptions): Promise<string | LookupAddress[]> =>
      new Promise((resolve, reject) =>
        resolver.lookup(hostname, options, (error, addresses) => (error ? reject(error) : resolve(addresses))),
      );
    try {
      expect(await lookup("dual.binding.test", { all: true, hints: 0, order: "ipv6first" })).toEqual([
        { address: "::1", family: 6 },
        { address: "127.0.0.1", family: 4 },
      ]);
      expect(await lookup("dual.binding.test", { all: true, family: 4 })).toEqual([
        { address: "127.0.0.1", family: 4 },
      ]);
      expect(await lookup("dual.binding.test", { all: false, family: 6 })).toBe("::1");
      expect(await lookup("v4.binding.test", { all: true })).toEqual([{ address: "127.0.0.1", family: 4 }]);
      await expect(lookup("missing.binding.test", { all: true })).rejects.toMatchObject({ code: "ENOTFOUND" });
      await expect(lookup("dual.binding.test", { hints: 8 })).rejects.toMatchObject({ code: "EINVAL" });
    } finally {
      resolver.cancel();
    }
  });

  it.each(["deadline", "abort"])("interrupts pending DNS on %s without late request completion", async (mode) => {
    const controller = new AbortController();
    const operation = new NodeModelHttpTransport({ dnsServers: dns.servers }).createOperation({
      method: "GET",
      url: `http://slow.${mode}.binding.test:${port}/`,
      headers: [],
      body: new Uint8Array(),
      timeoutMs: mode === "deadline" ? 40 : 1000,
      signal: controller.signal,
    });
    try {
      const pending = operation.send();
      if (mode === "abort") {
        await dns.waitForQuery("slow.abort.binding.test");
        controller.abort();
      }
      await expect(pending).rejects.toMatchObject({
        name: mode === "deadline" ? "Error" : "AbortError",
      });
    } finally {
      operation.close();
      operation.close();
    }
  });

  it.each(["options", "agent"])(
    "preserves caller %s lookup instead of using the default DNS channel",
    async (source) => {
      let calls = 0;
      const lookup: NonNullable<http.RequestOptions["lookup"]> = (_hostname, options, callback) => {
        ++calls;
        queueMicrotask(() =>
          options.all ? callback(null, [{ address: "127.0.0.1", family: 4 }]) : callback(null, "127.0.0.1", 4),
        );
      };
      const agent = new http.Agent({ lookup });
      const adapter = new NodeModelHttpTransport(
        source === "options" ? { lookup, dnsServers: dns.servers } : { httpAgent: agent, dnsServers: dns.servers },
      );
      const operation = adapter.createOperation({
        method: "GET",
        url: `http://override.binding.test:${port}/`,
        headers: [],
        body: new Uint8Array(),
        timeoutMs: 1000,
        signal: new AbortController().signal,
      });
      try {
        expect((await operation.send()).statusCode).toBe(200);
        expect(calls).toBe(1);
        expect(dns.queries.filter((query) => query.hostname === "override.binding.test")).toEqual([]);
      } finally {
        operation.close();
        agent.destroy();
      }
    },
  );
});
