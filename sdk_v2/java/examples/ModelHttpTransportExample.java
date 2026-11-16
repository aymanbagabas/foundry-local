// Copyright (c) Microsoft Corporation. Licensed under the MIT License.
import com.microsoft.foundry.local.ApacheModelHttpTransport;
import com.microsoft.foundry.local.CatalogType;
import com.microsoft.foundry.local.Configuration;
import com.microsoft.foundry.local.FoundryLocalException;
import com.microsoft.foundry.local.FoundryLocalManager;
import com.sun.net.httpserver.HttpServer;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.concurrent.atomic.AtomicInteger;

/** Run with the SDK, JNA, and optional Apache dependencies on the class path. No model download is needed. */
public class ModelHttpTransportExample {
    public static void main(String[] args) throws Exception {
        if (args.length != 2) throw new IllegalArgumentException("Expected native runtime and work directories");
        Path work = Path.of(args[1]).toAbsolutePath();
        Files.createDirectories(work);
        AtomicInteger requests = new AtomicInteger();
        HttpServer server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        byte[] catalog = """
                {"totalCount":1,"continuationToken":"","summaries":[{
                  "assetId":"azureml://registries/test/models/example-cpu/versions/1",
                  "name":"example-cpu","alias":"example","version":"1","foundryLocal":"",
                  "variantInformation":{
                    "parents":[{"assetId":"azureml://registries/test/models/example/versions/1"}],
                    "variantMetadata":{"modelType":"ONNX","device":"cpu","executionProvider":"CPUExecutionProvider"}
                  }
                }]}
                """.getBytes(StandardCharsets.UTF_8);
        server.createContext("/", exchange -> {
            requests.incrementAndGet();
            exchange.getRequestBody().readAllBytes();
            exchange.getResponseHeaders().add("Content-Type", "application/json");
            exchange.getResponseHeaders().add("X-Example", "first");
            exchange.getResponseHeaders().add("X-Example", "second");
            exchange.sendResponseHeaders(200, catalog.length);
            try (var body = exchange.getResponseBody()) {
                for (int offset = 0; offset < catalog.length; offset += 3) {
                    body.write(catalog, offset, Math.min(3, catalog.length - offset));
                    body.flush();
                }
            }
            exchange.close();
        });
        server.start();
        try (var transport = ApacheModelHttpTransport.builder().maxConnections(8, 4).build()) {
            var configuration = Configuration.builder("JavaModelHttpTransportExample")
                    .runtimeDirectory(Path.of(args[0]))
                    .appDataDirectory(work.resolve("app"))
                    .modelCacheDirectory(work.resolve("models"))
                    .addCatalogUrl("http://127.0.0.1:" + server.getAddress().getPort() + "/catalog")
                    .modelHttpTransport(transport).build();
            try (var manager = new FoundryLocalManager(configuration)) {
                var models = manager.catalog().models();
                if (models.size() != 1 || !"example".equals(models.get(0).alias()) || requests.get() == 0) {
                    throw new AssertionError("Native catalog did not use the registered transport");
                }
                int catalogRequests = requests.get();
                try {
                    manager.catalog(CatalogType.AZURE_REGISTRY);
                    throw new AssertionError("Azure Registry must reject a custom transport");
                } catch (FoundryLocalException expected) {
                    if (expected.code() != 1 || requests.get() != catalogRequests) throw expected;
                }
                System.out.printf("Native Apache HTTP transport: %d loopback catalog request(s), 1 model (example)%n",
                        catalogRequests);
                System.out.println("Azure Registry rejected the custom transport before network access");
            }
        } finally {
            server.stop(0);
        }
    }
}
