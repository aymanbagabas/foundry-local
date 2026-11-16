import { once } from "node:events";
import * as http from "node:http";
import { Agent as HttpsAgent } from "node:https";
import { gzipSync } from "node:zlib";

import { afterAll, beforeAll, describe, expect, it } from "vitest";

import { type ModelHttpRequest, NodeModelHttpTransport } from "../src/index.js";

describe("NodeModelHttpTransport (real loopback HTTP)", () => {
  const requests: {
    method: string | undefined;
    url: string | undefined;
    headers: string[];
    body: Buffer;
    port: number | undefined;
  }[] = [];
  const gzip = gzipSync("raw payload");
  let server: http.Server;
  let origin: string;

  beforeAll(async () => {
    server = http.createServer((request, response) => {
      const chunks: Buffer[] = [];
      request.on("data", (chunk: Buffer) => chunks.push(chunk));
      request.on("end", () => {
        requests.push({
          method: request.method,
          url: request.url,
          headers: request.rawHeaders,
          body: Buffer.concat(chunks),
          port: request.socket.remotePort,
        });
        if (request.url === "/disconnect") {
          request.socket.destroy();
          return;
        }
        if (request.url === "/headers") return;
        if (request.url === "/body") {
          response.writeHead(200, { "content-length": 20 });
          response.flushHeaders();
          return;
        }
        if (request.url === "/redirect") {
          response.writeHead(302, { location: "/followed" }).end("redirect");
          return;
        }
        if (request.url === "/gzip") {
          response.writeHead(200, { "content-encoding": "gzip", "content-length": gzip.length }).end(gzip);
          return;
        }
        response.writeHead(403, ["X-Duplicate", "one", "X-Duplicate", "two", "X-Bytes", "\x80\xff"]);
        response.end("abcdef");
      });
    });
    server.listen(0, "127.0.0.1");
    await once(server, "listening");
    const address = server.address();
    if (address === null || typeof address === "string") throw new Error("Missing HTTP address");
    origin = `http://127.0.0.1:${address.port}`;
  });

  afterAll(async () => {
    server.closeAllConnections();
    await new Promise<void>((resolve, reject) => server.close((error) => (error ? reject(error) : resolve())));
  });

  function input(path: string, extra: Partial<ModelHttpRequest> = {}): ModelHttpRequest {
    return {
      url: `${origin}${path}`,
      method: "GET",
      headers: [],
      body: new Uint8Array(),
      timeoutMs: 1000,
      signal: new AbortController().signal,
      ...extra,
    };
  }

  it("allocates without I/O and preserves method, query, duplicate field bytes and POST body", async () => {
    const adapter = new NodeModelHttpTransport();
    const before = requests.length;
    const operation = adapter.createOperation(
      input("/dot/../signed%2fpath?sig=a%2Bb%2F%3D&x=1&x=2", {
        method: "POST",
        headers: [
          ["X-Request", Uint8Array.from([0x80, 0xff])],
          ["X-Request", Buffer.from("two")],
        ],
        body: Uint8Array.from([0, 1, 255]),
      }),
    );
    expect(requests.length).toBe(before);
    try {
      const head = await operation.send();
      expect(head.statusCode).toBe(403);
      expect(head.headers.filter(([name]) => name === "X-Duplicate")).toEqual([
        ["X-Duplicate", Buffer.from("one")],
        ["X-Duplicate", Buffer.from("two")],
      ]);
      expect(head.headers).toContainEqual(["X-Bytes", Buffer.from([0x80, 0xff])]);
      expect(requests.at(-1)).toMatchObject({
        method: "POST",
        url: "/dot/../signed%2fpath?sig=a%2Bb%2F%3D&x=1&x=2",
        body: Buffer.from([0, 1, 255]),
      });
      expect(requests.at(-1)?.headers.slice(0, 4)).toEqual(["X-Request", "\x80\xff", "X-Request", "two"]);
      expect(await operation.read(2)).toEqual(Buffer.from("ab"));
      expect(await operation.read(3)).toEqual(Buffer.from("cde"));
      expect(await operation.read(4)).toEqual(Buffer.from("f"));
      expect(await operation.read(4)).toEqual(new Uint8Array());
      expect(await operation.read(4)).toEqual(new Uint8Array());
    } finally {
      operation.close();
      operation.close();
    }
    await expect(operation.read(1)).rejects.toMatchObject({ name: "AbortError" });
    await expect(operation.send()).rejects.toThrow("already sent");
  });

  it("does not follow redirects, retry broken connections, or decompress response bytes", async () => {
    const adapter = new NodeModelHttpTransport({ httpAgent: false });
    const before = requests.length;
    const redirect = adapter.createOperation(input("/redirect"));
    const compressed = adapter.createOperation(input("/gzip"));
    const broken = adapter.createOperation(input("/disconnect"));
    try {
      expect((await redirect.send()).statusCode).toBe(302);
      expect((await compressed.send()).statusCode).toBe(200);
      const chunks: Uint8Array[] = [];
      for (;;) {
        const chunk = await compressed.read(3);
        if (chunk.length === 0) break;
        expect(chunk.length).toBeLessThanOrEqual(3);
        chunks.push(chunk);
      }
      expect(Buffer.concat(chunks)).toEqual(gzip);
      await expect(broken.send()).rejects.toThrow("connection failed");
      expect(requests.slice(before).map((request) => request.url)).toEqual(["/redirect", "/gzip", "/disconnect"]);
    } finally {
      redirect.close();
      compressed.close();
      broken.close();
    }
  });

  it.each(["/headers", "/body"])("aborts pending network work at %s through AbortSignal", async (path) => {
    const controller = new AbortController();
    const operation = new NodeModelHttpTransport().createOperation(input(path, { signal: controller.signal }));
    try {
      const send = operation.send();
      const pending = path === "/body" ? send.then(() => operation.read(5)) : send;
      const rejected = expect(pending).rejects.toMatchObject({ name: "AbortError" });
      if (path === "/body") await send;
      else await once(server, "request");
      controller.abort();
      await rejected;
      operation.cancel();
      await expect(operation.read(1)).rejects.toMatchObject({ name: "AbortError" });
    } finally {
      operation.close();
    }
  });

  it("supports cancellation before send and explicit cancellation during a body read", async () => {
    const adapter = new NodeModelHttpTransport();
    const before = requests.length;
    const early = adapter.createOperation(input("/headers"));
    early.cancel();
    await expect(early.send()).rejects.toMatchObject({ name: "AbortError" });
    expect(requests.length).toBe(before);
    early.close();
    const active = adapter.createOperation(input("/body"));
    try {
      await active.send();
      const pending = active.read(2);
      const rejected = expect(pending).rejects.toMatchObject({ name: "AbortError" });
      active.cancel();
      active.cancel();
      await rejected;
    } finally {
      active.close();
    }
  });

  it.each(["/headers", "/body"])("enforces a total deadline while blocked at %s", async (path) => {
    const operation = new NodeModelHttpTransport().createOperation(input(path, { timeoutMs: 35 }));
    try {
      if (path === "/headers") await expect(operation.send()).rejects.toThrow("deadline exceeded");
      else {
        await operation.send();
        await expect(operation.read(2)).rejects.toThrow("deadline exceeded");
      }
    } finally {
      operation.close();
    }
  });

  it("returns HEAD EOF without waiting for the advertised body and validates read capacities", async () => {
    const operation = new NodeModelHttpTransport().createOperation(input("/", { method: "HEAD" }));
    try {
      expect((await operation.send()).statusCode).toBe(403);
      expect(await operation.read(2)).toEqual(new Uint8Array());
      for (const capacity of [0, -1, 0.5, Number.NaN, Number.MAX_SAFE_INTEGER + 1]) {
        await expect(operation.read(capacity)).rejects.toThrow("positive safe integer");
      }
    } finally {
      operation.close();
    }
  });

  it("passes caller-owned Agents and snapshots options without modifying caller settings", async () => {
    const httpAgent = new http.Agent({ keepAlive: true, maxSockets: 1 });
    const httpsAgent = new HttpsAgent({ keepAlive: true, maxSockets: 2 });
    let seenAgent: http.RequestOptions["agent"];
    const options = Object.freeze({
      httpAgent,
      httpsAgent,
      request(url: URL, init: http.RequestOptions, listener: (response: http.IncomingMessage) => void) {
        seenAgent = init.agent;
        // Explicit loopback routing is an external boundary; the adapter itself never rewrites destinations.
        return http.request(new URL(`${origin}${url.pathname}${url.search}`), { ...init, agent: httpAgent }, listener);
      },
    });
    const operation = new NodeModelHttpTransport(options).createOperation(
      input("/agent", { url: "https://catalog.invalid/agent?sig=unchanged%2F" }),
    );
    try {
      expect((await operation.send()).statusCode).toBe(403);
      expect(seenAgent).toBe(httpsAgent);
      expect(options.httpsAgent.options.maxSockets).toBe(2);
      expect(options.httpAgent.maxSockets).toBe(1);
      expect(requests.at(-1)?.url).toBe("/agent?sig=unchanged%2F");
    } finally {
      operation.close();
      httpAgent.destroy();
      httpsAgent.destroy();
    }
  });

  it("releases completed operations without destroying caller-pooled sockets", async () => {
    const agent = new http.Agent({ keepAlive: true, maxSockets: 1 });
    const adapter = new NodeModelHttpTransport({ httpAgent: agent });
    const ports: (number | undefined)[] = [];
    try {
      for (let i = 0; i < 2; ++i) {
        const controller = new AbortController();
        const operation = adapter.createOperation(input(`/pool-${i}`, { signal: controller.signal }));
        try {
          expect((await operation.send()).statusCode).toBe(403);
          while ((await operation.read(2)).length > 0) {
            // Drain through the adapter's pull interface before returning the socket to the Agent.
          }
          ports.push(requests.at(-1)?.port);
          // Native release aborts the operation signal, even when the response has already reached EOF.
          controller.abort();
        } finally {
          operation.close();
        }
      }
      expect(ports[0]).toBeTypeOf("number");
      expect(ports[1]).toBe(ports[0]);
    } finally {
      agent.destroy();
    }
  });
});
