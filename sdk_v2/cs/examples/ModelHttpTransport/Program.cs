// --------------------------------------------------------------------------------------------------------------------
// <copyright company="Microsoft">
//   Copyright (c) Microsoft. All rights reserved.
// </copyright>
// --------------------------------------------------------------------------------------------------------------------

using System.Net;
using System.Text;

using Microsoft.AI.Foundry.Local;
using Microsoft.Extensions.Logging.Abstractions;

// Default: deterministic IN-MEMORY example, no network or model weights.
// Optional: --http <catalog-url> [--proxy <proxy-url>] registers a configured HttpClient adapter.
// Put generated cache/logs under ignored build output.
if (args.Length != 0 && ((args.Length != 2 && args.Length != 4) || args[0] != "--http" ||
    (args.Length == 4 && args[2] != "--proxy")))
{
    throw new ArgumentException("Usage: ModelHttpTransport [--http <catalog-url> [--proxy <proxy-url>]]");
}

var configured = 0;
using var httpTransport = args.Length >= 2 ? new HttpClientModelHttpTransport(handler =>
{
    configured++;
    handler.ConnectTimeout = TimeSpan.FromSeconds(10);
    if (args.Length == 4)
    {
        handler.UseProxy = true;
        handler.Proxy = new WebProxy(new Uri(args[3], UriKind.Absolute));
    }
}) : null;
var memoryTransport = new InMemoryTransport();
IModelHttpTransport transport = httpTransport is null ? memoryTransport : httpTransport;
var modelCount = 0;
await FoundryLocalManager.CreateAsync(new Configuration
{
    AppName = "cs-model-http-transport",
    AppDataDir = Path.Combine(AppContext.BaseDirectory, "transport-data", Guid.NewGuid().ToString("N")),
    CatalogUrls = new List<(string, string?)> { (httpTransport is null ? InMemoryTransport.CatalogUrl : args[1], null) },
    ModelHttpTransport = transport,
    DisableNonessentialTelemetry = true
}, NullLogger.Instance);
using (var manager = FoundryLocalManager.Instance)
{
    var catalog = await manager.GetCatalogAsync();
    var models = await catalog.ListModelsAsync();
    modelCount = models.Count;
    if (httpTransport is null && models.Count != 0)
    {
        throw new InvalidOperationException("The in-memory catalog must contain exactly zero models.");
    }
}

if (httpTransport is not null)
{
    if (configured != 1)
    {
        throw new InvalidOperationException("The handler configuration must run exactly once.");
    }

    Console.WriteLine($"Native registered configured HttpClient transport: {modelCount} models, " +
        $"{configured} configuration callback, explicit proxy: {args.Length == 4}.");
    return;
}

if (memoryTransport.Sends == 0 || memoryTransport.Reads <= memoryTransport.Sends ||
    memoryTransport.Released != memoryTransport.Created)
{
    throw new InvalidOperationException("Expected native callbacks, partial reads and balanced operation ownership.");
}

Console.WriteLine($"Native registered in-memory transport: {memoryTransport.Sends} catalog request(s), " +
    $"{memoryTransport.Reads} reads, 0 models, {memoryTransport.Released}/{memoryTransport.Created} operations released.");

internal sealed class InMemoryTransport : IModelHttpTransport
{
    internal const string CatalogUrl = "https://example.invalid/catalog";
    internal int Created;
    internal int Sends;
    internal int Reads;
    internal int Released;

    public IModelHttpOperation CreateOperation(ModelHttpRequest request)
    {
        if (request.Method != "POST" || request.Url != CatalogUrl)
        {
            throw new InvalidOperationException("Unsupported in-memory request.");
        }

        Interlocked.Increment(ref Created);
        return new Operation(this); // No I/O here.
    }

    private sealed class Operation(InMemoryTransport owner) : IModelHttpOperation
    {
        private readonly byte[] _body = Encoding.UTF8.GetBytes("{\"value\":[]}");
        private int _position;

        public Task<ModelHttpResponseHead> SendAsync(CancellationToken cancellationToken)
        {
            cancellationToken.ThrowIfCancellationRequested();
            Interlocked.Increment(ref owner.Sends);
            return Task.FromResult(new ModelHttpResponseHead(200, new[]
            {
                new ModelHttpHeader("content-type", Encoding.ASCII.GetBytes("application/json")),
                new ModelHttpHeader("x-example", Encoding.ASCII.GetBytes("first")),
                new ModelHttpHeader("x-example", Encoding.ASCII.GetBytes("second"))
            }));
        }

        public Task<int> ReadAsync(Memory<byte> buffer, CancellationToken cancellationToken)
        {
            cancellationToken.ThrowIfCancellationRequested();
            var count = Math.Min(3, Math.Min(buffer.Length, _body.Length - _position));
            _body.AsMemory(_position, count).CopyTo(buffer);
            _position += count;
            Interlocked.Increment(ref owner.Reads);
            return Task.FromResult(count);
        }

        public void Dispose() => Interlocked.Increment(ref owner.Released);
    }
}
