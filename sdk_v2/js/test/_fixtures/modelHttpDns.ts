import { createSocket } from "node:dgram";
import { EventEmitter, once } from "node:events";

/** A real UDP DNS boundary, not a stub of SDK internals. No external DNS requests leave these tests. */
export async function createModelHttpDns(): Promise<{
  servers: string[];
  queries: { hostname: string; family: number }[];
  waitForQuery(hostname: string): Promise<void>;
  close(): Promise<void>;
}> {
  const socket = createSocket("udp4");
  const queries: { hostname: string; family: number }[] = [];
  const notifications = new EventEmitter();
  socket.on("message", (query, peer) => {
    let offset = 12;
    const labels: string[] = [];
    while (offset < query.length) {
      const length = query[offset++] ?? 0;
      if (length === 0) break;
      labels.push(query.subarray(offset, offset + length).toString("ascii"));
      offset += length;
    }
    if (offset + 4 > query.length) return;
    const type = query.readUInt16BE(offset);
    const hostname = labels.join(".");
    queries.push({ hostname, family: type === 1 ? 4 : 6 });
    notifications.emit("query");
    if (hostname.startsWith("slow.")) return;
    const missing = hostname.startsWith("missing.") || (hostname.startsWith("v4.") && type === 28);
    const address =
      type === 1 ? Buffer.from([127, 0, 0, 1]) : Buffer.from([0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1]);
    const header = Buffer.alloc(12);
    header.writeUInt16BE(query.readUInt16BE(0), 0);
    header.writeUInt16BE(missing ? 0x8183 : 0x8180, 2);
    header.writeUInt16BE(1, 4);
    header.writeUInt16BE(missing ? 0 : 1, 6);
    const answer = Buffer.alloc(12);
    answer.writeUInt16BE(0xc00c, 0);
    answer.writeUInt16BE(type, 2);
    answer.writeUInt16BE(1, 4);
    answer.writeUInt32BE(30, 6);
    answer.writeUInt16BE(address.length, 10);
    socket.send(
      Buffer.concat([header, query.subarray(12, offset + 4), ...(missing ? [] : [answer, address])]),
      peer.port,
      peer.address,
    );
  });
  socket.bind(0, "127.0.0.1");
  await once(socket, "listening");
  return {
    servers: [`127.0.0.1:${socket.address().port}`],
    queries,
    async waitForQuery(hostname) {
      while (!queries.some((query) => query.hostname === hostname)) await once(notifications, "query");
    },
    close: () => new Promise<void>((resolve) => socket.close(() => resolve())),
  };
}
