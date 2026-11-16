import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    include: ["native/test/**/*.test.ts"],
    environment: "node",
    fileParallelism: false,
    pool: "forks",
    testTimeout: 30_000,
  },
});
