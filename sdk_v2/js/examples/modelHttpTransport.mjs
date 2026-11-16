// Copyright (c) Microsoft Corporation. All rights reserved.
// Licensed under the MIT License.
// Run after npm run build: node examples/modelHttpTransport.mjs
import assert from "node:assert/strict";
import { once } from "node:events";
import { mkdir, mkdtemp, rm } from "node:fs/promises";
import * as http from "node:http";
import { fileURLToPath } from "node:url";

import { FoundryLocalManager, NodeModelHttpTransport } from "../dist/index.js";

const build = fileURLToPath(new URL("../build/", import.meta.url));
await mkdir(build, { recursive: true });
const directory = await mkdtemp(`${build}model-http-example-`);
const observed = [];
const server = http.createServer(async (request, response) => {
  const chunks = [];
  for await (const chunk of request) chunks.push(chunk);
  observed.push({ method: request.method, path: request.url, body: Buffer.concat(chunks).toString() });
  if (request.url === "/cancel") {
    response.writeHead(200, { "content-length": 6 });
    response.flushHeaders();
    return;
  }
  const payload = Buffer.from(
    JSON.stringify({
      totalCount: 1,
      continuationToken: "",
      summaries: [
        {
          assetId: "azureml://registries/test/models/example-cpu/versions/1",
          name: "example-cpu",
          alias: "example",
          version: "1",
          foundryLocal: "",
          variantInformation: {
            parents: [{ assetId: "azureml://registries/test/models/example/versions/1" }],
            variantMetadata: { modelType: "ONNX", device: "cpu", executionProvider: "CPUExecutionProvider" },
          },
        },
      ],
    }),
  );
  response.writeHead(200, ["Content-Type", "application/json", "X-Example", "first", "X-Example", "second"]);
  for (let offset = 0; offset < payload.length; offset += 3) {
    response.write(payload.subarray(offset, offset + 3));
    await new Promise((resolve) => setImmediate(resolve));
  }
  response.end();
});
server.listen(0, "127.0.0.1");
await once(server, "listening");
const address = server.address();
assert(address && typeof address !== "string");
const agent = new http.Agent({ keepAlive: true, maxSockets: 2 });
// Only the example redirects the destination to a deterministic fixture. Applications normally omit request and
// supply httpsAgent to configure TLS trust, proxy routing and connection pooling for the original destination.
const transport = new NodeModelHttpTransport({
  httpAgent: agent,
  request(url, options, onResponse) {
    const loopback = new URL(`http://127.0.0.1:${address.port}${url.pathname}${url.search}`);
    return http.request(loopback, { ...options, agent }, onResponse);
  },
});
let manager;
try {
  manager = new FoundryLocalManager({
    appName: "NodeModelHttpTransportExample",
    appDataDir: directory,
    logsDir: `${directory}/logs`,
    modelCacheDir: `${directory}/models`,
    disableNonessentialTelemetry: true,
    modelHttpTransport: transport,
  });
  const catalog = await manager.getCatalogAsync();
  const models = await catalog.getModels();
  assert.equal(models.length, 1);
  assert.equal(models[0].alias, "example");
  assert(observed.length > 0);
  assert(observed.every(({ method, body }) => method === "POST" && body.includes('"filters"')));
  const catalogRequests = observed.length;
  // The same adapter also interrupts an actual blocked body read without waiting for its deadline.
  const controller = new AbortController();
  const operation = transport.createOperation({
    method: "GET",
    url: `http://127.0.0.1:${address.port}/cancel`,
    headers: [],
    body: new Uint8Array(),
    timeoutMs: 1000,
    signal: controller.signal,
  });
  try {
    await operation.send();
    const pending = operation.read(2);
    const cancelled = assert.rejects(pending, { name: "AbortError" });
    controller.abort();
    await cancelled;
  } finally {
    operation.close();
  }
  console.log(`Native Node HTTP transport: ${catalogRequests} loopback catalog request(s), 1 model (example)`);
  console.log("Blocked body read cancelled with AbortSignal");
} finally {
  manager?.dispose();
  agent.destroy();
  server.closeAllConnections();
  await new Promise((resolve) => server.close(resolve));
  await rm(directory, { recursive: true, force: true });
}
