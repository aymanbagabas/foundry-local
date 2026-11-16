// Copyright (c) Microsoft Corporation. All rights reserved.
// Licensed under the MIT License.

import assert from "node:assert/strict";
import { lookup } from "node:dns";
import * as http from "node:http";
import { parentPort, workerData } from "node:worker_threads";

import { FoundryLocalManager, NodeModelHttpTransport } from "../../dist/index.js";

const { directory, port, stage } = workerData;
const agent = new http.Agent({ lookup });
const adapter = new NodeModelHttpTransport({ httpAgent: agent });
let concurrentQueries = false;
let blockedReads = 0;
const transport = {
  createOperation(request) {
    const original = new URL(request.url);
    const concurrent = concurrentQueries;
    const operation = adapter.createOperation({
      ...request,
      url: concurrent
        ? `http://localhost:${port}/shutdown-${stage}${original.pathname}${original.search}`
        : `http://127.0.0.1:${port}/warmup${original.pathname}${original.search}`,
    });
    let firstRead = true;
    return {
      send: () => operation.send(),
      read(capacity) {
        if (concurrent && firstRead) {
          firstRead = false;
          if (++blockedReads === 8) parentPort.postMessage("eight blocked reads");
        }
        return operation.read(capacity);
      },
      cancel: () => operation.cancel(),
      close: () => operation.close(),
    };
  },
};
const manager = new FoundryLocalManager({
  appName: "native-node-http-environment-shutdown",
  appDataDir: directory,
  logsDir: `${directory}/logs`,
  modelCacheDir: `${directory}/models`,
  disableNonessentialTelemetry: true,
  modelHttpTransport: transport,
});
try {
  const catalog = await manager.getCatalogAsync();
  assert.equal((await catalog.getModels()).length, 1);
  concurrentQueries = true;
  await Promise.all(Array.from({ length: 8 }, () => catalog.getModelVersions("dns-model")));
  throw new Error("Blocked HTTP unexpectedly completed");
} finally {
  manager.dispose();
  agent.destroy();
}
