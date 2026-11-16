// Copyright (c) Microsoft Corporation. All rights reserved.
// Licensed under the MIT License.

import { ADDRCONFIG, type LookupAddress, Resolver, getDefaultResultOrder } from "node:dns";
import type { LookupFunction } from "node:net";
import { networkInterfaces } from "node:os";

/** Per-operation c-ares channel: getaddrinfo would compete with native waiters for the same libuv pool. */
export class ModelHttpLookup {
  readonly #resolver = new Resolver({ tries: 1 });
  #cancelled = false;

  constructor(servers?: readonly string[]) {
    if (servers !== undefined) this.#resolver.setServers([...servers]);
  }

  readonly lookup: LookupFunction = (hostname, options, callback): void => {
    if (this.#cancelled) return;
    const family = options.family === "IPv4" ? 4 : options.family === "IPv6" ? 6 : (options.family ?? 0);
    const hints = options.hints ?? 0;
    if (![0, 4, 6].includes(family) || (hints & ~ADDRCONFIG) !== 0) {
      queueMicrotask(() => {
        if (!this.#cancelled)
          callback(Object.assign(new Error("Unsupported DNS lookup options"), { code: "EINVAL" }), "");
      });
      return;
    }
    const configured = new Set(
      Object.values(networkInterfaces())
        .flatMap((entries) => entries ?? [])
        .filter((entry) => !entry.internal)
        .map((entry) => (entry.family === "IPv4" ? 4 : 6)),
    );
    const families = ([4, 6] as const).filter(
      (candidate) => (family === 0 || family === candidate) && (!(hints & ADDRCONFIG) || configured.has(candidate)),
    );
    const defaultOrder =
      options.verbatim === undefined ? getDefaultResultOrder() : options.verbatim ? "verbatim" : "ipv4first";
    const order = options.order ?? defaultOrder;
    const queries = families.map(
      (candidate) =>
        new Promise<LookupAddress[]>((resolve) => {
          const receive = (error: NodeJS.ErrnoException | null, addresses: string[]): void => {
            resolve(error ? [] : addresses.map((address) => ({ address, family: candidate })));
          };
          try {
            if (candidate === 4) this.#resolver.resolve4(hostname, receive);
            else this.#resolver.resolve6(hostname, receive);
          } catch {
            resolve([]);
          }
        }),
    );
    void Promise.all(queries).then((groups) => {
      if (this.#cancelled) return;
      const addresses = groups.flat();
      if (order === "ipv6first") addresses.sort((a, b) => b.family - a.family);
      if (addresses.length === 0) {
        callback(Object.assign(new Error("Model HTTP DNS resolution failed"), { code: "ENOTFOUND" }), "");
      } else if (options.all) {
        callback(null, addresses);
      } else {
        const first = addresses[0];
        if (first !== undefined) callback(null, first.address, first.family);
      }
    });
  };

  cancel(): void {
    this.#cancelled = true;
    this.#resolver.cancel();
  }
}
