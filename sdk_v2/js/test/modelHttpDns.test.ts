// Copyright (c) Microsoft Corporation. All rights reserved.
// Licensed under the MIT License.

import { execFile } from "node:child_process";
import { once } from "node:events";
import { mkdirSync, mkdtempSync, rmSync } from "node:fs";
import * as http from "node:http";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";
import { Worker } from "node:worker_threads";

import { afterAll, beforeAll, describe, expect, it } from "vitest";

const root = fileURLToPath(new URL("../", import.meta.url));

describe("model HTTP OS DNS and dedicated native execution", () => {
  let server: http.Server;
  let port: number;
  const pending = new Map<string, http.ServerResponse[]>();
  const barriers = new Map<string, () => void>();
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
    server = http.createServer((request, response) => {
      request.resume();
      request.on("end", () => {
        const key = request.url?.split("/")[1] ?? "";
        if (key === "warmup") {
          response.writeHead(200).end(body);
          return;
        }
        const batch = pending.get(key) ?? [];
        batch.push(response);
        pending.set(key, batch);
        // All eight SDK calls must perform HTTP concurrently, not just queue behind a cached catalog lookup.
        if (batch.length === 8) {
          if (key === "shutdown-send" || key === "shutdown-read") {
            if (key === "shutdown-read") for (const waiting of batch) waiting.writeHead(200).flushHeaders();
            barriers.get(key)?.();
          } else {
            for (const waiting of batch) waiting.writeHead(200).end(body);
          }
        }
      });
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
  });

  for (const pool of ["1", "4"]) {
    it.each(["default", "agent", "lookup", "agent-lookup"])(
      `completes eight concurrent native SDK queries using %s OS lookup with a ${pool}-thread pool`,
      async (mode) => {
        mkdirSync(resolve(root, "build"), { recursive: true });
        const directory = mkdtempSync(resolve(root, "build/parallel-http-dns-"));
        const key = `${mode}-${pool}`;
        try {
          const result = await promisify(execFile)(
            process.execPath,
            [resolve(root, "native/test/modelHttpDnsWorker.mjs"), directory, String(port), mode],
            { timeout: 20_000, env: { ...process.env, UV_THREADPOOL_SIZE: pool } },
          );
          expect(result.stderr).toBe("");
          expect(JSON.parse(result.stdout)).toEqual({
            queries: 8,
            pool,
            mode,
            osLookups: expect.any(Number),
            customLookups: mode.includes("lookup") ? 8 : 0,
          });
          const report: { osLookups: number } = JSON.parse(result.stdout);
          expect(report.osLookups).toBeGreaterThanOrEqual(8);
          expect(pending.get(key)).toHaveLength(8);
        } finally {
          pending.delete(key);
          rmSync(directory, { recursive: true, force: true });
        }
      },
      25_000,
    );
  }

  it.each(["send", "read"])(
    "joins eight dedicated jobs when an environment terminates during real HTTP %s",
    async (stage) => {
      mkdirSync(resolve(root, "build"), { recursive: true });
      const directory = mkdtempSync(resolve(root, "build/shutdown-http-dns-"));
      const key = `shutdown-${stage}`;
      const arrivals = new Promise<void>((resolve) => barriers.set(key, resolve));
      const worker = new Worker(new URL("../native/test/modelHttpOsShutdownWorker.mjs", import.meta.url), {
        workerData: { directory, port, stage },
      });
      const blockedReads = new Promise<void>((resolve, reject) => {
        worker.once("message", () => resolve());
        worker.once("error", reject);
        worker.once("exit", (code) => reject(new Error(`Worker exited before blocked reads: ${code}`)));
      });
      try {
        await Promise.race([arrivals, blockedReads]);
        if (stage === "read") await blockedReads;
        expect(pending.get(key)).toHaveLength(8);
        expect(await worker.terminate()).toBe(1);
      } finally {
        await worker.terminate();
        pending.delete(key);
        barriers.delete(key);
        rmSync(directory, { recursive: true, force: true });
      }
    },
    20_000,
  );
});
