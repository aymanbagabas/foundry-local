// Copyright (c) Microsoft Corporation. Licensed under the MIT License.
package com.microsoft.foundry.local;

import static org.junit.jupiter.api.Assertions.*;
import com.sun.jna.Memory;
import com.sun.jna.Pointer;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import org.junit.jupiter.api.Test;

class ModelHttpTransportTest {
    @Test void ownsRequestResponseAndConfigurationValues() {
        byte[] body = {1, 2, 3};
        var fields = new ArrayList<>(List.of(new ModelHttpHeader("X-Test", "\u00ff")));
        var request = new ModelHttpRequest("POST", "https://example.com/a%2Fb?sig=a%2Bb&x=+", fields, body, 10);
        var response = new ModelHttpResponseHead(403, fields);
        fields.clear();
        body[0] = 9;
        request.body()[0] = 8;
        assertArrayEquals(new byte[] {1, 2, 3}, request.body());
        assertEquals(1, request.headers().size());
        assertEquals("\u00ff", response.headers().get(0).value());
        assertEquals("https://example.com/a%2Fb?sig=a%2Bb&x=+", request.url());
        assertThrows(UnsupportedOperationException.class, () -> request.headers().clear());
        assertThrows(UnsupportedOperationException.class, () -> response.headers().clear());
        ModelHttpTransport transport = value -> null;
        var builder = Configuration.builder("test").runtimeDirectory(Path.of("."))
                .modelCacheDirectory(Path.of(".")).appDataDirectory(Path.of("."))
                .modelHttpTransport(transport).addCatalogUrl("https://example.com/catalog");
        var configuration = builder.build();
        builder.modelHttpTransport(null).addCatalogUrl("https://example.com/other");
        assertSame(transport, configuration.modelHttpTransport());
        assertEquals(1, configuration.catalogUrls().size());
        assertThrows(UnsupportedOperationException.class, () -> configuration.catalogUrls().clear());
    }

    @Test void rejectsInvalidFieldsWithoutNativeCode() {
        for (String name : List.of("", "x:y", "bad name", "bad\rname")) {
            assertThrows(IllegalArgumentException.class, () -> new ModelHttpHeader(name, ""));
        }
        for (String value : List.of("a\0b", "a\rb", "a\nb", "\u0100")) {
            assertThrows(IllegalArgumentException.class, () -> new ModelHttpHeader("X-Test", value));
        }
        assertThrows(IllegalArgumentException.class, () -> new ModelHttpResponseHead(99, List.of()));
        for (String url : List.of("/a", "file:///a", "http://user:pass@example.com/", "http://example.com/#a")) {
            assertThrows(IllegalArgumentException.class, () ->
                    new ModelHttpRequest("GET", url, List.of(), new byte[0], 0));
        }
    }

    @Test void negotiatesNewestSupportedApiAndRejectsTransportBeforeTableTailAccess() {
        for (int supported = 2; supported <= 5; supported++) {
            int current = supported;
            List<Integer> attempts = new ArrayList<>();
            // Deliberately allocate only the old prefix. A rejected transport must never read slot 12.
            try (Memory oldTable = new Memory(8)) {
                var negotiated = NativeApi.negotiate(version -> {
                    attempts.add(version);
                    return version == current ? oldTable : null;
                });
                assertEquals(current, negotiated.version());
                assertEquals(oldTable, negotiated.pointer());
                assertEquals(5, attempts.get(0));
                assertEquals(current, attempts.get(attempts.size() - 1));
                if (current < 5) {
                    var table = new NativeApi.Table(oldTable);
                    assertThrows(UnsupportedOperationException.class, () -> NativeApi.registerModelHttpTransport(
                            negotiated.version(), table, null, new Pointer(1)));
                    assertNull(NativeApi.registerModelHttpTransport(negotiated.version(), table, null, null));
                } else {
                    assertDoesNotThrow(() -> NativeApi.requireTransportApi(negotiated.version()));
                }
            }
        }
        assertEquals(2, new NativeApi.BytesData().version);
        assertEquals(2, new NativeApi.AudioData().version);
    }

    @Test void matchesNativeHttpStructLayouts() {
        assertEquals(72, new NativeModelHttpTransport.TransportData().size());
        assertEquals(64, new NativeModelHttpTransport.RequestData().size());
    }
}
