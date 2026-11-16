// --------------------------------------------------------------------------------------------------------------------
// <copyright company="Microsoft">
//   Copyright (c) Microsoft. All rights reserved.
// </copyright>
// --------------------------------------------------------------------------------------------------------------------

namespace Microsoft.AI.Foundry.Local.Tests;

using System.Net;
using System.Net.Sockets;
using System.Runtime.InteropServices;
using System.Text;

using Microsoft.AI.Foundry.Local.Detail;
using Microsoft.AI.Foundry.Local.Detail.Interop;
using Microsoft.AI.Foundry.Local.Detail.Native;
using Microsoft.Extensions.Logging.Abstractions;

using Bridge = Microsoft.AI.Foundry.Local.Detail.ModelHttpTransportBridge;
using PublicConfiguration = Microsoft.AI.Foundry.Local.Configuration;

[NotInParallel]
internal sealed class ModelHttpTransportTests
{
    [Test]
    [Arguments(2u)]
    [Arguments(3u)]
    [Arguments(4u)]
    public async Task OlderTablesNeverReadTransportTail(uint version)
    {
        await Assert.That(Api.ReadModelHttpTransportSetting(IntPtr.Zero, version)).IsNull();
    }

    [Test]
    public async Task TransportSetterUsesSlot12Only()
    {
        var ptr = Marshal.AllocHGlobal(13 * IntPtr.Size);
        Bridge.SetTransportDelegate setter = (config, transport) => new IntPtr(123);
        try
        {
            Marshal.WriteIntPtr(ptr, 12 * IntPtr.Size, Marshal.GetFunctionPointerForDelegate(setter));
            var read = Api.ReadModelHttpTransportSetting(ptr, 5)!;
            await Assert.That(read(IntPtr.Zero, IntPtr.Zero)).IsEqualTo(new IntPtr(123));
            Marshal.WriteIntPtr(ptr, 12 * IntPtr.Size, IntPtr.Zero);
            await Assert.That(() => Api.ReadModelHttpTransportSetting(ptr, 5)).Throws<InvalidOperationException>();
        }
        finally
        {
            Marshal.FreeHGlobal(ptr);
            GC.KeepAlive(setter);
        }
    }

    [Test]
    public async Task OlderRuntimeRejectsOnlyConfiguredTransport()
    {
        DllLoader.Initialize();
        using var config = new Detail.Native.Configuration("cs-transport-compat");
        var setter = Api.SetModelHttpTransport;
        try
        {
            Api.SetModelHttpTransport = null;
            config.SetHttpTransport(null);
            try
            {
                config.SetHttpTransport(new TestTransport());
                Assert.Fail("Expected an explicit unsupported-runtime error.");
            }
            catch (FoundryLocalException error)
            {
                await Assert.That(error.ErrorCode).IsEqualTo(FoundryLocalErrorCode.NotImplemented);
            }
        }
        finally
        {
            Api.SetModelHttpTransport = setter;
        }
    }

    [Test]
    public async Task ImmutableMetadataAndValidation()
    {
        var bytes = new byte[] { 128, 255 };
        var header = new ModelHttpHeader("x-byte", bytes);
        bytes[0] = 0;
        var headers = new List<ModelHttpHeader> { header, header };
        var response = new ModelHttpResponseHead(403, headers);
        headers.Clear();
        await Assert.That(header.Value.ToArray().SequenceEqual(new byte[] { 128, 255 })).IsTrue();
        await Assert.That(response.Headers.Count).IsEqualTo(2);
        await Assert.That(response.StatusCode).IsEqualTo(403);
        await Assert.That(() => new ModelHttpHeader("bad name", bytes)).Throws<ArgumentException>();
        await Assert.That(() => new ModelHttpHeader("x", new byte[] { 10 })).Throws<ArgumentException>();
        await Assert.That(() => new ModelHttpResponseHead(99)).Throws<ArgumentOutOfRangeException>();
#if NET8_0_OR_GREATER
        using var adapter = new HttpClientModelHttpTransport();
        adapter.Dispose();
        await Assert.That(() => adapter.CreateOperation(null!)).Throws<ObjectDisposedException>();
#else
        await Assert.That(() => new HttpClientModelHttpTransport()).Throws<PlatformNotSupportedException>();
#endif
    }

    [Test]
    public async Task CallbacksRootTransportCopyRequestPreserveHeadersAndPartialReads()
    {
        var transport = new TestTransport();
        using var fixture = new CallbackFixture(transport, body: new byte[] { 0, 255, 7 });
        await Assert.That(transport.Sends).IsEqualTo(0); // Create must not send.
        await Assert.That(transport.LastRequest!.Method).IsEqualTo("POST");
        await Assert.That(transport.LastRequest.Url).IsEqualTo(CallbackFixture.Url);
        await Assert.That(transport.LastRequest.Body.ToArray().SequenceEqual(new byte[] { 0, 255, 7 })).IsTrue();
        await Assert.That(transport.LastRequest.Headers[0].Value.ToArray().SequenceEqual(new byte[] { 128, 255 })).IsTrue();
        GC.Collect();
        GC.WaitForPendingFinalizers();
        var response = fixture.Send();
        await Assert.That(response.StatusCode).IsEqualTo(403u); // HTTP errors are not transport errors.
        await Assert.That(response.HeaderCount.ToUInt64()).IsEqualTo(2UL);
        var first = Marshal.PtrToStructure<Bridge.HeaderData>(response.Headers);
        var second = Marshal.PtrToStructure<Bridge.HeaderData>(IntPtr.Add(response.Headers, Marshal.SizeOf<Bridge.HeaderData>()));
        await Assert.That(Marshal.PtrToStringAnsi(first.Name)).IsEqualTo("x-byte");
        await Assert.That(Marshal.ReadByte(first.Value)).IsEqualTo((byte)128);
        await Assert.That(Marshal.ReadByte(second.Value)).IsEqualTo((byte)255);
        await Assert.That(fixture.Read()).IsEqualTo(3);
        await Assert.That(fixture.Read()).IsEqualTo(3);
        await Assert.That(fixture.Read()).IsEqualTo(3);
        await Assert.That(fixture.Read()).IsEqualTo(3);
        await Assert.That(fixture.Read()).IsEqualTo(0);
        await Assert.That(fixture.Read()).IsEqualTo(0);
        fixture.Dispose();
        await Assert.That(transport.Disposals).IsEqualTo(1);
    }

    [Test]
    public async Task CancelBeforeSendIsStickyAndUnusedOperationsAreReleased()
    {
        var transport = new TestTransport();
        using var fixture = new CallbackFixture(transport);
        fixture.Table.Cancel(fixture.Operation);
        fixture.Table.Cancel(fixture.Operation);
        await Assert.That(() => fixture.Send()).Throws<OperationCanceledException>();
        await Assert.That(transport.Sends).IsEqualTo(0);
        fixture.Dispose();
        await Assert.That(transport.Disposals).IsEqualTo(1);
    }

    [Test]
    [Arguments(false)]
    [Arguments(true)]
    public async Task CancelInterruptsBlockedSendOrRead(bool duringRead)
    {
        var transport = new TestTransport { BlockSend = !duringRead, BlockRead = duringRead };
        using var fixture = new CallbackFixture(transport);
        if (duringRead)
        {
            fixture.Send();
        }

        var pending = Task.Run(() =>
        {
            if (duringRead)
            {
                fixture.Read();
            }
            else
            {
                fixture.Send();
            }
        });
        await Within(transport.Entered.Task);
        fixture.Table.Cancel(fixture.Operation);
        await Assert.That(async () => await Within(pending)).Throws<OperationCanceledException>();
    }

    [Test]
    [Arguments(false)]
    [Arguments(true)]
    public async Task TotalDeadlineInterruptsBlockedIoAsNetworkFailure(bool duringRead)
    {
        var transport = new TestTransport { BlockSend = !duringRead, BlockRead = duringRead };
        using var fixture = new CallbackFixture(transport, timeout: 100);
        if (duringRead)
        {
            fixture.Send();
            await Task.Delay(60);
        }

        var task = Task.Run(() =>
        {
            try
            {
                if (duringRead)
                {
                    fixture.Read();
                }
                else
                {
                    fixture.Send();
                }

                Assert.Fail("Deadline must fail.");
            }
            catch (FoundryLocalException error)
            {
                return error.ErrorCode;
            }

            return FoundryLocalErrorCode.Ok;
        });
        await Within(task);
        await Assert.That(await task).IsEqualTo(FoundryLocalErrorCode.Network);
    }

    [Test]
    [Arguments("create")]
    [Arguments("send")]
    [Arguments("read")]
    [Arguments("count")]
    [Arguments("dispose")]
    public async Task CallbackFailuresNeverEscapeAndErrorsAreSecretSafe(string phase)
    {
        var transport = new TestTransport { FailurePhase = phase };
        using var fixture = new CallbackFixture(transport, create: false);
        try
        {
            fixture.Create();
            fixture.Send();
            fixture.Read();
            fixture.Dispose();
            await Assert.That(phase).IsEqualTo("dispose");
        }
        catch (FoundryLocalException error)
        {
            await Assert.That(error.ErrorCode).IsEqualTo(FoundryLocalErrorCode.Network);
            await Assert.That(error.Message).IsEqualTo("Model HTTP transport failed.");
            if (phase == "create")
            {
                await Assert.That(fixture.Operation).IsEqualTo(IntPtr.Zero);
            }
        }
    }

#if NET8_0_OR_GREATER
    [Test]
    public async Task HttpClientReturnsRedirectAndRawBodyWithoutRetryOrDecompression()
    {
        using var server = new TcpListener(IPAddress.Loopback, 0);
        server.Start();
        var port = ((IPEndPoint)server.LocalEndpoint).Port;
        var rawBody = new byte[] { 31, 139, 0, 255 };
        var served = Task.Run(async () =>
        {
            using var socket = await server.AcceptTcpClientAsync();
            using var stream = socket.GetStream();
            var received = new List<byte>();
            var one = new byte[1];
            while (await stream.ReadAsync(one) == 1)
            {
                received.Add(one[0]);
                if (received.Count >= 4 && Encoding.ASCII.GetString(received.ToArray()).EndsWith("\r\n\r\n", StringComparison.Ordinal))
                {
                    break;
                }
            }

            var body = new byte[3];
            await stream.ReadExactlyAsync(body);
            var head = Encoding.ASCII.GetBytes("HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1:1/forbidden\r\n" +
                "Content-Encoding: gzip\r\nContent-Length: 4\r\nX-Repeat: one\r\nX-Repeat: two\r\nConnection: close\r\n\r\n");
            await stream.WriteAsync(head);
            await stream.WriteAsync(rawBody);
            return (Encoding.ASCII.GetString(received.ToArray()), body);
        });
        using var adapter = new HttpClientModelHttpTransport();
        var url = $"http://127.0.0.1:{port}/a%2fb/../raw?sig=A%2Bb%2fC&x=1";
        var request = new ModelHttpRequest("POST", url, Array.Empty<ModelHttpHeader>(), new byte[] { 0, 255, 7 }, 0);
        using var operation = adapter.CreateOperation(request);
        var response = await operation.SendAsync(CancellationToken.None).WaitAsync(TimeSpan.FromSeconds(5));
        var bytes = new byte[10];
        var read = await operation.ReadAsync(bytes, CancellationToken.None);
        var received = await served.WaitAsync(TimeSpan.FromSeconds(5));
        await Assert.That(response.StatusCode).IsEqualTo(302);
        await Assert.That(response.Headers.Count(h => h.Name.Equals("X-Repeat", StringComparison.OrdinalIgnoreCase))).IsEqualTo(2);
        await Assert.That(bytes.Take(read).SequenceEqual(rawBody)).IsTrue();
        await Assert.That(received.Item1.StartsWith("POST /a%2fb/../raw?sig=A%2Bb%2fC&x=1 HTTP/1.1", StringComparison.Ordinal)).IsTrue();
        await Assert.That(received.body.SequenceEqual(new byte[] { 0, 255, 7 })).IsTrue();
        await Assert.That(server.Pending()).IsFalse();
    }

    [Test]
    [Arguments(false)]
    [Arguments(true)]
    public async Task HttpClientCancellationInterruptsRealSocketIo(bool duringRead)
    {
        using var server = new TcpListener(IPAddress.Loopback, 0);
        server.Start();
        var port = ((IPEndPoint)server.LocalEndpoint).Port;
        var entered = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
        var finish = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
        var served = Task.Run(async () =>
        {
            using var socket = await server.AcceptTcpClientAsync();
            using var stream = socket.GetStream();
            var received = new List<byte>();
            var one = new byte[1];
            while (await stream.ReadAsync(one) == 1)
            {
                received.Add(one[0]);
                if (Encoding.ASCII.GetString(received.ToArray()).EndsWith("\r\n\r\n", StringComparison.Ordinal))
                {
                    break;
                }
            }

            if (duringRead)
            {
                await stream.WriteAsync(Encoding.ASCII.GetBytes("HTTP/1.1 200 OK\r\nContent-Length: 1\r\n\r\n"));
            }

            entered.TrySetResult(true);
            await finish.Task;
        });
        using var transport = new HttpClientModelHttpTransport();
        using var operation = transport.CreateOperation(new ModelHttpRequest("GET", $"http://127.0.0.1:{port}/",
            Array.Empty<ModelHttpHeader>(), Array.Empty<byte>(), 0));
        using var cancellation = new CancellationTokenSource();
        try
        {
            Task pending = operation.SendAsync(cancellation.Token);
            await Within(entered.Task);
            if (duringRead)
            {
                await Within(pending);
                pending = operation.ReadAsync(new byte[1], cancellation.Token);
            }

            cancellation.Cancel();
            await Assert.That(async () => await Within(pending)).Throws<OperationCanceledException>();
        }
        finally
        {
            finish.TrySetResult(true);
            await Within(served);
        }
    }

    [Test]
    public async Task HttpClientDoesNotReplayFailedRequests()
    {
        using var server = new TcpListener(IPAddress.Loopback, 0);
        server.Start();
        var port = ((IPEndPoint)server.LocalEndpoint).Port;
        var served = Task.Run(async () =>
        {
            using var socket = await server.AcceptTcpClientAsync();
            using var stream = socket.GetStream();
            var received = new List<byte>();
            var one = new byte[1];
            while (await stream.ReadAsync(one) == 1)
            {
                received.Add(one[0]);
                if (Encoding.ASCII.GetString(received.ToArray()).EndsWith("\r\n\r\n", StringComparison.Ordinal))
                {
                    break;
                }
            }

            // Close after receiving the request, without sending ANY response bytes.
            return Encoding.ASCII.GetString(received.ToArray());
        });
        using var transport = new HttpClientModelHttpTransport();
        using var operation = transport.CreateOperation(new ModelHttpRequest("GET", $"http://127.0.0.1:{port}/",
            Array.Empty<ModelHttpHeader>(), Array.Empty<byte>(), 0));
        await Assert.That(async () => await Within(operation.SendAsync(CancellationToken.None)))
            .Throws<System.Net.Http.HttpRequestException>();
        await Within(served);
        await Assert.That((await served).Contains("Connection: close", StringComparison.OrdinalIgnoreCase)).IsTrue();
        await Assert.That(server.Pending()).IsFalse();
    }
#endif

    [Test]
    public async Task NativeRetainRootsTransportUntilFinalRelease()
    {
        var weak = CreateWeakFixture(out var fixture);
        Collect();
        await Assert.That(weak.IsAlive).IsTrue();
        fixture.Dispose();
        Collect();
        await Assert.That(weak.IsAlive).IsFalse();
    }

    [System.Runtime.CompilerServices.MethodImpl(System.Runtime.CompilerServices.MethodImplOptions.NoInlining)]
    private static WeakReference CreateWeakFixture(out CallbackFixture fixture)
    {
        var transport = new TestTransport();
        fixture = new CallbackFixture(transport, create: false);
        return new WeakReference(transport);
    }

    private static void Collect()
    {
        GC.Collect();
        GC.WaitForPendingFinalizers();
        GC.Collect();
    }

    private static async Task Within(Task task)
    {
        if (await Task.WhenAny(task, Task.Delay(5000)) != task)
        {
            throw new TimeoutException("Transport test timed out.");
        }

        await task;
    }

    [Test]
    public async Task RealConfigurationRegistersTransportAndNativeCatalogConsumesStream()
    {
        var transport = new TestTransport { StatusCode = 200 };
        var settings = new Dictionary<string, string> { ["DisableNonessentialTelemetry"] = "true" };
        var data = Path.Combine(AppContext.BaseDirectory, "TestResults", "transport-" + Guid.NewGuid().ToString("N"));
        await FoundryLocalManager.CreateAsync(new PublicConfiguration
        {
            AppName = "cs-model-transport",
            AppDataDir = data,
            ModelHttpTransport = transport,
            CatalogUrls = new List<(string, string?)> { (CallbackFixture.Url, null) },
            AdditionalSettings = settings
        }, NullLogger.Instance);
        using (var manager = FoundryLocalManager.Instance)
        {
            var catalog = await manager.GetCatalogAsync();
            await Assert.That((await catalog.ListModelsAsync()).Count).IsEqualTo(0);
            var sent = transport.Sends;
            try
            {
                await manager.GetCatalogAsync(CatalogType.AzureRegistry,
                    new Dictionary<string, string> { ["name"] = "unused" });
                Assert.Fail("Azure Registry must reject custom transport.");
            }
            catch (FoundryLocalException error)
            {
                await Assert.That(error.ErrorCode).IsEqualTo(FoundryLocalErrorCode.NotImplemented);
            }

            await Assert.That(transport.Sends).IsEqualTo(sent);
        }

        await Assert.That(transport.Sends).IsGreaterThan(0);
        await Assert.That(transport.Reads).IsGreaterThan(transport.Sends);
        await Assert.That(transport.Disposals).IsEqualTo(transport.Creates);
        await Assert.That(settings.Count).IsEqualTo(1);
        await Assert.That(settings["DisableNonessentialTelemetry"]).IsEqualTo("true");
    }

    [Test]
    public async Task NativeManagerKeepsOriginalTransportAfterConfigurationReplacementAndDisposal()
    {
        DllLoader.Initialize();
        var original = new TestTransport { StatusCode = 200 };
        var replacement = new TestTransport { StatusCode = 200 };
        using var config = new Detail.Native.Configuration("cs-transport-replacement");
        config.SetAppDataDir(Path.Combine(AppContext.BaseDirectory, "TestResults", Guid.NewGuid().ToString("N")));
        config.AddCatalogUrl(CallbackFixture.Url);
        config.SetHttpTransport(original);
        using var manager = new Detail.Native.Manager(config);
        config.SetHttpTransport(replacement);
        config.SetHttpTransport(null);
        config.Dispose(); // The manager's native retain must keep its original context alive.
        var catalog = manager.GetCatalog(); // Non-owning catalog view; the manager owns its lifetime.
        using var models = catalog.GetModels();
        await Assert.That(models.Models.Count).IsEqualTo(0);
        await Assert.That(original.Sends).IsGreaterThan(0);
        await Assert.That(replacement.Creates).IsEqualTo(0);
        await Assert.That(original.Disposals).IsEqualTo(original.Creates);
        await Assert.That(() => config.SetHttpTransport(original)).Throws<ObjectDisposedException>();
    }

    private sealed class TestTransport : IModelHttpTransport
    {
        internal int Creates;
        internal int Sends;
        internal int Reads;
        internal int Disposals;
        internal int StatusCode = 403;
        internal bool BlockSend;
        internal bool BlockRead;
        internal string? FailurePhase;
        internal ModelHttpRequest? LastRequest;
        internal readonly TaskCompletionSource<bool> Entered = new(TaskCreationOptions.RunContinuationsAsynchronously);

        public IModelHttpOperation CreateOperation(ModelHttpRequest request)
        {
            LastRequest = request;
            if (FailurePhase == "create")
            {
                throw new InvalidOperationException("secret-token");
            }

            Interlocked.Increment(ref Creates);
            return new TestOperation(this);
        }

        private sealed class TestOperation(TestTransport owner) : IModelHttpOperation
        {
            private readonly byte[] _body = Encoding.UTF8.GetBytes("{\"value\":[]}");
            private int _position;

            public async Task<ModelHttpResponseHead> SendAsync(CancellationToken cancellationToken)
            {
                Interlocked.Increment(ref owner.Sends);
                if (owner.FailurePhase == "send")
                {
                    throw new InvalidOperationException("secret-token");
                }

                if (owner.BlockSend)
                {
                    owner.Entered.TrySetResult(true);
                    await Task.Delay(Timeout.Infinite, cancellationToken);
                }

                return new ModelHttpResponseHead(owner.StatusCode, new[]
                {
                    new ModelHttpHeader("x-byte", new byte[] { 128 }),
                    new ModelHttpHeader("x-byte", new byte[] { 255 })
                });
            }

            public async Task<int> ReadAsync(Memory<byte> buffer, CancellationToken cancellationToken)
            {
                Interlocked.Increment(ref owner.Reads);
                if (owner.FailurePhase == "read")
                {
                    throw new InvalidOperationException("secret-token");
                }

                if (owner.FailurePhase == "count")
                {
                    return buffer.Length + 1;
                }

                if (owner.BlockRead)
                {
                    owner.Entered.TrySetResult(true);
                    await Task.Delay(Timeout.Infinite, cancellationToken);
                }

                var count = Math.Min(3, Math.Min(buffer.Length, _body.Length - _position));
                _body.AsMemory(_position, count).CopyTo(buffer);
                _position += count;
                return count;
            }

            public void Dispose()
            {
                Interlocked.Increment(ref owner.Disposals);
                if (owner.FailurePhase == "dispose")
                {
                    throw new InvalidOperationException("secret-token");
                }
            }
        }
    }

    private sealed class CallbackFixture : IDisposable
    {
        internal const string Url = "https://example.invalid/catalog?sig=Keep%2BExactly";
        internal Bridge.TransportData Table;
        internal IntPtr Operation;
        private readonly List<IntPtr> _allocations = new();
        private readonly IntPtr _request;
        private bool _disposed;

        internal CallbackFixture(IModelHttpTransport transport, ulong timeout = 0, byte[]? body = null, bool create = true)
        {
            DllLoader.Initialize();
            Api.EnsureInitialized();
            using (var bridge = new Bridge(transport))
            {
                bridge.Register(IntPtr.Zero, (_, ptr) =>
                {
                    Table = Marshal.PtrToStructure<Bridge.TransportData>(ptr);
                    Table.Retain(Table.Context);
                    return IntPtr.Zero;
                });
            }

            var header = Allocate(Marshal.SizeOf<Bridge.HeaderData>());
            Marshal.StructureToPtr(new Bridge.HeaderData { Name = Bytes(Encoding.ASCII.GetBytes("x-byte")),
                Value = Bytes(new byte[] { 128, 255 }) }, header, false);
            body ??= Array.Empty<byte>();
            var data = new Bridge.RequestData
            {
                Version = 1,
                Method = Bytes(Encoding.ASCII.GetBytes("POST")),
                Url = Bytes(Encoding.UTF8.GetBytes(Url)),
                Headers = header,
                HeaderCount = new UIntPtr(1),
                Body = Bytes(body),
                BodySize = new UIntPtr((uint)body.Length),
                TimeoutMilliseconds = timeout
            };
            _request = Allocate(Marshal.SizeOf<Bridge.RequestData>());
            Marshal.StructureToPtr(data, _request, false);
            if (create)
            {
                Create();
            }
        }

        internal void Create() => Api.CheckStatus(Table.Create(Table.Context, _request, out Operation));

        internal Bridge.ResponseData Send()
        {
            var response = new Bridge.ResponseData { Version = 1 };
            Api.CheckStatus(Table.Send(Operation, ref response));
            return response;
        }

        internal int Read()
        {
            var buffer = Marshal.AllocHGlobal(7);
            try
            {
                Api.CheckStatus(Table.Read(Operation, buffer, new UIntPtr(7), out var count));
                return checked((int)count.ToUInt64());
            }
            finally
            {
                Marshal.FreeHGlobal(buffer);
            }
        }

        private IntPtr Allocate(int count)
        {
            var ptr = Marshal.AllocHGlobal(count);
            _allocations.Add(ptr);
            return ptr;
        }

        private IntPtr Bytes(byte[] bytes)
        {
            var ptr = Allocate(bytes.Length + 1);
            Marshal.Copy(bytes, 0, ptr, bytes.Length);
            Marshal.WriteByte(ptr, bytes.Length, 0);
            return ptr;
        }

        public void Dispose()
        {
            if (_disposed)
            {
                return;
            }

            _disposed = true;
            if (Operation != IntPtr.Zero)
            {
                Table.ReleaseOperation(Operation);
            }

            Table.Release(Table.Context);
            foreach (var ptr in _allocations)
            {
                Marshal.FreeHGlobal(ptr);
            }
        }
    }
}
