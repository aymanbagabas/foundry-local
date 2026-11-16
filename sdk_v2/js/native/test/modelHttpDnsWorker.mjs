import assert from "node:assert/strict";
import * as http from "node:http";

import { FoundryLocalManager, NodeModelHttpTransport } from "../../dist/index.js";

const [directory, httpPort, dnsServer] = process.argv.slice(2);
const transport = new NodeModelHttpTransport({
  dnsServers: [dnsServer],
  request(url, options, onResponse) {
    return http.request(
      new URL(`http://catalog.binding.test:${httpPort}${url.pathname}${url.search}`),
      options,
      onResponse,
    );
  },
});
const manager = new FoundryLocalManager({
  appName: "native-parallel-node-dns",
  appDataDir: directory,
  logsDir: `${directory}/logs`,
  modelCacheDir: `${directory}/models`,
  disableNonessentialTelemetry: true,
  modelHttpTransport: transport,
});
try {
  const catalog = await manager.getCatalogAsync();
  const results = await Promise.all(Array.from({ length: 8 }, () => catalog.getModels()));
  assert(results.every((models) => models.length === 1 && models[0].alias === "dns-model"));
  console.log(`8 native queries completed with UV_THREADPOOL_SIZE=${process.env.UV_THREADPOOL_SIZE}`);
} finally {
  manager.dispose();
}
