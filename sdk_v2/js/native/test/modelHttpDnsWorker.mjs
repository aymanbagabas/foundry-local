// Copyright (c) Microsoft Corporation. All rights reserved.
// Licensed under the MIT License.

import assert from "node:assert/strict";
import { createHook } from "node:async_hooks";
import { lookup as osLookup } from "node:dns";
import * as http from "node:http";

import { FoundryLocalManager, NodeModelHttpTransport } from "../../dist/index.js";

const [directory, httpPort, mode] = process.argv.slice(2);
const key = `${mode}-${process.env.UV_THREADPOOL_SIZE}`;
let customLookups = 0;
let osLookups = 0;
const hook = createHook({
  init(_id, type) {
    // Observe real getaddrinfo work, without replacing Node's DNS implementation or changing process DNS policy.
    if (type === "GETADDRINFOREQWRAP") ++osLookups;
  },
});
const lookup = (hostname, options, callback) => {
  assert.equal(hostname, "localhost");
  ++customLookups;
  osLookup(hostname, options, callback);
};
let concurrentQueries = false;
const agent = new http.Agent({
  keepAlive: true,
  ...(mode === "agent-lookup" ? { lookup } : {}),
});
const adapter = new NodeModelHttpTransport({
  ...(mode === "agent" || mode === "agent-lookup" ? { httpAgent: agent } : {}),
  ...(mode === "lookup" ? { lookup } : {}),
});
const transport = {
  createOperation(request) {
    const original = new URL(request.url);
    // Only this test transport reroutes the native destination. The actual Node adapter uses normal OS localhost DNS.
    return adapter.createOperation({
      ...request,
      url: concurrentQueries
        ? `http://localhost:${httpPort}/${key}${original.pathname}${original.search}`
        : `http://127.0.0.1:${httpPort}/warmup${original.pathname}${original.search}`,
    });
  },
};
const manager = new FoundryLocalManager({
  appName: "native-parallel-node-dns",
  appDataDir: directory,
  logsDir: `${directory}/logs`,
  modelCacheDir: `${directory}/models`,
  disableNonessentialTelemetry: true,
  modelHttpTransport: transport,
});
hook.enable();
try {
  const catalog = await manager.getCatalogAsync();
  assert.equal((await catalog.getModels()).length, 1);
  concurrentQueries = true;
  // Version queries fetch independently; repeated getModels() calls can be satisfied by the native catalog cache.
  const results = await Promise.all(Array.from({ length: 8 }, () => catalog.getModelVersions("dns-model")));
  assert(results.every((models) => models.length === 1 && models[0].alias === "dns-model"));
  assert(osLookups >= 8, "default and delegated lookups must use OS getaddrinfo, not c-ares resolve");
  assert.equal(customLookups, mode.includes("lookup") ? 8 : 0);
  console.log(
    JSON.stringify({ queries: results.length, pool: process.env.UV_THREADPOOL_SIZE, mode, osLookups, customLookups }),
  );
} finally {
  hook.disable();
  manager.dispose();
  agent.destroy();
}
