import { createRequire } from "node:module";
import { resolve } from "node:path";
import { setImmediate } from "node:timers/promises";
import { parentPort, workerData } from "node:worker_threads";

const require = createRequire(import.meta.url);
const preload = require(workerData.preload);
for (const name of workerData.libraries) preload.preloadLibrary(resolve(workerData.runtime, name));
const addon = require(workerData.addon);
const events = [];
let pendingSend = false;
let settlePending;
const manager = new addon.Manager({
  appName: "native-model-http-transport-worker-cleanup",
  appDataDir: workerData.directory,
  logsDir: resolve(workerData.directory, "logs"),
  modelCacheDir: resolve(workerData.directory, "models"),
  disableNonessentialTelemetry: true,
  modelHttpTransportCallback(event) {
    events.push(event.type);
    if (event.type === workerData.block) {
      parentPort.postMessage("blocked");
      return new Promise(() => {});
    }
    if (workerData.rejectNotifications) {
      if (event.type === "close" && settlePending !== undefined) {
        const settle = settlePending;
        settlePending = undefined;
        void setImmediate().then(() => {
          events.push("late-settlement");
          settle({ statusCode: 200, headers: [] });
        });
      }
      if (event.type === "cancel" || event.type === "close") return Promise.reject(new Error(`rejected ${event.type}`));
      if (workerData.rejectNotifications === "cancel" && event.type === "send" && !pendingSend) {
        pendingSend = true;
        return new Promise((resolve) => {
          settlePending = resolve;
        });
      }
    }
    if (event.type === "send") return Promise.resolve({ statusCode: 403, headers: [] });
    if (event.type === "read") return Promise.resolve(new Uint8Array());
    return undefined;
  },
});
const catalog = await manager.getCatalogAsync();
await Promise.all(Array.from({ length: workerData.operations ?? 1 }, () => catalog.getModelsAsync()));
if (workerData.rejectNotifications) {
  // Give unhandled rejections a full event-loop turn to surface before reporting success.
  await setImmediate();
  await setImmediate();
  manager.dispose();
  parentPort.postMessage(events);
}
