# Custom model HTTP transports

Applications can select the HTTP client for model acquisition.
The core API does not require Apache or another HTTP client.
Foundry still controls retries, regions, ranges, checksums, and cache completion.

The transport covers public catalogs, registry resolution, blob enumeration, HEAD requests, and model downloads.
It does not cover telemetry, inference requests, or execution-provider downloads.
Native shared-host mode keeps these callbacks in the client process.
Azure Registry catalogs explicitly reject a custom transport before credential or network access.
This preview does not expose Azure Registry catalog options.

## Registration

```java
ModelHttpTransport transport = request -> new ApplicationOperation(request);
var configuration = Configuration.builder("my-app")
        .runtimeDirectory(runtime)
        .modelCacheDirectory(cache)
        .appDataDirectory(data)
        .modelHttpTransport(transport)
        .build();
try (var manager = new FoundryLocalManager(configuration)) {
    var models = manager.catalog().models();
}
```

`ApplicationOperation` implements `ModelHttpOperation`.
The [executable example](../examples/ModelHttpTransportExample.java) uses the Apache adapter and a loopback catalog.
It registers the adapter through the public configuration and queries a real native manager.
It needs no model download.

The binding negotiates API 5, 4, 3, then 2.
A configured transport requires API 5.
Older runtimes reject registration before the binding reads the new configuration slot.
An unset transport preserves the native HTTP client and API 2 compatibility.
HTTP structures use version 1; inference payloads remain version 2.

Configurations and request data are immutable.
The builder copies the catalog list.
Request constructors and accessors copy body arrays.
Header lists preserve order and duplicate fields.
Header values represent ISO-8859-1 bytes, not UTF-8.
URLs preserve their encoded path and query.

## Operation contract

- `createOperation` allocates local state only. It must not access the network.
- `send` sends one request and returns its status and headers.
- `read` copies at most the requested byte count. A short read is valid; zero means permanent EOF.
- `cancel` must interrupt blocked send and read calls. It must remain effective before and after those calls.
- `close` releases resources, even when the operation never reached `send`.

Native workers can execute distinct operations concurrently.
The bridge serializes send and read for each operation.
Cancellation does not take that lock.
The bridge copies the request and owns response header storage until operation release.
Native retains keep the transport, callback code, and loaded runtime reachable.

Implementations must not retry, follow redirects, decompress content, or replay authentication.
Return HTTP errors such as 403 as responses.
Preserve conditional, range, checksum, and duplicate headers.
The timeout covers send and all reads from entry into send.
Zero means no deadline.

Exceptions do not cross native callbacks.
The bridge returns owned statuses from the same loaded runtime.
It removes exception text from native statuses to protect credentials and signed URLs.
Cancellation returns code 5; I/O failures return code 6.
Invalid arguments return code 3; other callback failures return code 2.
Native lifecycle callbacks have no error return.
The bridge reports exceptions from cancel or close through `System.Logger`, without application exception text.
Custom implementations must keep these methods reliable.

Native buffers metadata up to 16 MiB.
Blob responses stream without this metadata limit.
The Java bridge uses bounded read buffers, not an entire response buffer.
It never declares a model complete or writes cache files.
A transport failure does not select the built-in HTTP client.
Native catalog policy can recover from a failed source with cached data or an empty list.

Do not call manager lifecycle methods or recursive model acquisition from a callback.
The Java manager serializes catalog and download calls with its lifecycle lock.
Manager close waits for these calls; it is not an acquisition cancellation method.
`Model.download` cancellation retains its existing progress-checkpoint behavior.
An application can close its Apache transport from another thread to interrupt active socket operations.

## Optional Apache adapter

Add this dependency to applications that use `ApacheModelHttpTransport`:

```xml
<dependency>
  <groupId>org.apache.httpcomponents.client5</groupId>
  <artifactId>httpclient5</artifactId>
  <version>5.5.1</version>
</dependency>
```

The SDK marks this dependency optional.
Core interfaces and registration work with only JNA.
Applications that do not use the adapter need no Apache classes.
The adapter owns its connection pool.
Close all dependent managers before normal transport disposal.

```java
try (var transport = ApacheModelHttpTransport.builder()
        .sslContext(applicationTlsContext)
        .proxy(new org.apache.hc.core5.http.HttpHost("http", "proxy.example.com", 8080))
        .connectionConfig(org.apache.hc.client5.http.config.ConnectionConfig.custom()
                .setConnectTimeout(org.apache.hc.core5.util.Timeout.ofSeconds(15))
                .build())
        .maxConnections(32, 8)
        .build()) {
    var configuration = Configuration.builder("my-app")
            .runtimeDirectory(runtime).modelCacheDirectory(cache).appDataDirectory(data)
            .modelHttpTransport(transport).build();
    try (var manager = new FoundryLocalManager(configuration)) {
        var models = manager.catalog().models();
    }
}
```

Use an `SSLContext` with application trust and key material.
The adapter retains certificate and hostname checks.
Do not disable these checks.
The builder also accepts a `DnsResolver`, socket settings, and a connection-pool wait timeout.
These settings do not mutate global JVM properties.
Application network limits can end a request before its native deadline.

The builder does not accept an arbitrary client or an unrestricted client builder.
Such clients can contain interceptors or execution chains that bypass transport policy.
The adapter always enforces these settings:

- `disableAutomaticRetries`
- `disableRedirectHandling`
- `disableContentCompression`
- `disableCookieManagement`
- `disableAuthCaching`
- `setAuthenticationEnabled(false)`, including proxy challenges
- No automatic protocol upgrade or Expect/Continue handshake

Explicit request authentication headers remain available.
Apache emits request framing from the owned body.
A supplied `Content-Length` must contain the matching canonical decimal length.
A supplied `Transfer-Encoding` must be `chunked`; conflicting or duplicate framing fields are rejected.
The adapter returns 3xx, 401, 403, 407, and other HTTP responses without automatic replay.
It streams raw payload bytes after HTTP framing removal.
Each operation owns one total-deadline task.
Expiry closes its active endpoint through Apache request cancellation.
Cancellation does not wait for the send/read monitor.
Operation close aborts the endpoint rather than drains an unfinished response.

**If a DNS resolver blocks, socket cancellation cannot interrupt that lookup.**
The default resolver uses JVM name resolution.
The native callback can remain blocked until that resolver returns, even after cancellation or deadline expiry.
Use prompt, bounded application resolution when this distinction matters.
The adapter does not hide this limitation with global JVM settings or detached resolver threads.
Cancellation remains permanent and prevents further request processing after the lookup returns.

### Why not `java.net.http.HttpClient`?

The JDK 17 implementation retries internally.
`MultiExchange.retryOnFailure` retries `ConnectionExpiredException` and, by default, `ConnectException`.
`getExceptionalCF` also retries exchanges that the peer did not process.
The controls include static JVM properties such as `jdk.httpclient.disableRetryConnect`.
The public client builder has no complete per-client retry switch.
The adapter therefore cannot enforce this contract with the JDK client alone.
It does not change those properties or require hidden JVM flags.
A loopback probe on JDK 17.0.21+9 confirmed the retry.
One `send()` produced two wire GET requests after the first connection closed without a response.
The Apache test observes exactly one request for that failure.

Apache 5.5.1 removes `HttpRequestRetryExec` when automatic retries are disabled.
Its `ProtocolExec` and `ConnectExec` consult the per-request authentication flag before a challenge replay.
`InternalExecRuntime.cancel` discards and closes the active endpoint.
This dependency provides explicit, local policy controls.
The tests verify wire behavior rather than infer guarantees from the absence of an adapter retry loop.

## Run the example and focused tests

Run these commands from `sdk_v2/java` with JDK 17 and Maven:

```sh
mvn -q -DskipTests package dependency:build-classpath -Dmdep.outputFile=target/classpath.txt
java -cp "target/classes:$(cat target/classpath.txt)" \
  examples/ModelHttpTransportExample.java /path/to/native-runtime target/model-http-example
mvn -q '-Dtest=ModelHttpTransportTest,ApacheModelHttpTransportTest,NativeModelHttpTransportTest' \
  -Dfoundry.test.native.required=true -Dfoundry.test.runtime=/path/to/native-runtime verify
```

Use `;` instead of `:` for the class path on Windows.
The tests cover raw compressed bytes, duplicate fields, signed queries, proxies, TLS trust, hostname checks, and HTTP errors.
They observe blocked socket reads before cancellation.
They also check deadlines, no automatic retries, native registration, native retain roots, and optional-dependency isolation.
