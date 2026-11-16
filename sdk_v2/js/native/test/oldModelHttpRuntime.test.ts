import { execFile } from "node:child_process";
import { mkdirSync, mkdtempSync, rmSync } from "node:fs";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";

import { afterAll, beforeAll, describe, expect, it } from "vitest";

const root = fileURLToPath(new URL("../../", import.meta.url));

// This runtime-double build needs a POSIX compiler/linker. Production bridge tests run on every supported platform.
describe.skipIf(process.platform === "win32")("older native HTTP transport API negotiation", () => {
  let directory: string;
  beforeAll(async () => {
    mkdirSync(resolve(root, "build"), { recursive: true });
    directory = mkdtempSync(resolve(root, "build/old-http-runtime-"));
    const leaf = process.platform === "darwin" ? "libfoundry_local.dylib" : "libfoundry_local.so";
    await promisify(execFile)(process.env.CXX ?? "c++", [
      "-std=c++17",
      "-Wall",
      "-Wextra",
      "-Werror",
      "-shared",
      "-fPIC",
      ...(process.platform === "darwin"
        ? ["-Wl,-install_name,libfoundry_local.dylib"]
        : ["-Wl,-soname,libfoundry_local.so"]),
      `-I${resolve(root, "../cpp/include")}`,
      resolve(root, "native/test/oldModelHttpRuntime.cc"),
      "-o",
      resolve(directory, leaf),
    ]);
  });
  afterAll(() => {
    if (directory) rmSync(directory, { recursive: true, force: true });
  });
  it.each([2, 3, 4])("accepts default configuration and explicitly rejects transport on API %i", async (version) => {
    const result = await promisify(execFile)(
      process.execPath,
      [resolve(root, "native/test/oldModelHttpRuntime.mjs"), directory],
      { timeout: 10_000, env: { ...process.env, FOUNDRY_TEST_OLD_API: String(version) } },
    );
    expect(result.stderr).toBe("");
    expect(result.stdout.trim()).toBe(`API ${version}: default accepted, transport rejected`);
  });
});
