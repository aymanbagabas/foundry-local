import assert from "node:assert/strict";

import { FoundryLocalManager } from "../../dist/index.js";

const config = Object.freeze({ appName: "old-runtime", libraryPath: process.argv[2] });
const manager = new FoundryLocalManager(config);
assert.equal(manager.disposed, false);
manager.dispose();
assert.equal(manager.disposed, true);
assert.throws(
  () =>
    new FoundryLocalManager({
      ...config,
      modelHttpTransport: {
        createOperation() {
          throw new Error("Old runtime must reject before dispatch");
        },
      },
    }),
  (error) => error.name === "FoundryLocalError" && error.code === 1 && /API v5/.test(error.message),
);
console.log(`API ${process.env.FOUNDRY_TEST_OLD_API}: default accepted, transport rejected`);
