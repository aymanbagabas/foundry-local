// --------------------------------------------------------------------------------------------------------------------
// <copyright company="Microsoft">
//   Copyright (c) Microsoft. All rights reserved.
// </copyright>
// --------------------------------------------------------------------------------------------------------------------

namespace Microsoft.AI.Foundry.Local;

using System.Net;
using System.Net.Http;
using System.Net.Sockets;
using System.Threading;

using Microsoft.AI.Foundry.Local.Detail;

/// <summary>A streamed HttpClient adapter with redirects, automatic decompression and cookies disabled.</summary>
/// <remarks>
/// Owns its client/handler. Dispose only after all managers using it have been disposed.
/// Uses one-shot HTTP/1.1 connections to prevent internal replay and protocol-downgrade retries.
/// Requires .NET 8 or later; the netstandard2.0 API cannot disable internal HttpClient replay.
/// Network policies can be configured on the SDK-owned handler before mandatory transport invariants are enforced.
/// </remarks>
public sealed class HttpClientModelHttpTransport : IModelHttpTransport, IDisposable
{
    private readonly HttpClient _client;
    private int _disposed;

    /// <summary>Create an adapter with the default platform network policies.</summary>
    public HttpClientModelHttpTransport()
    {
        _client = CreateClient();
    }

#if NET8_0_OR_GREATER
    /// <summary>Configure network policies on a fresh SDK-owned handler before transport invariants are enforced.</summary>
    /// <param name="configureHandler">
    /// Invoked once, synchronously. May configure proxy, TLS/trust/client certificates, connection timeout or a
    /// custom ConnectCallback (for example DNS routing). Do not retain, dispose, use or mutate the handler afterward.
    /// Referenced proxy/certificate objects and callback captures must remain usable until transport disposal.
    /// </param>
    /// <remarks>
    /// Redirects, decompression, cookies, pooled reuse and automatic challenge authentication are disabled afterward.
    /// Header encodings are fixed to Latin-1. Any custom ConnectCallback is wrapped with the one-attempt guard;
    /// it must return a plain connected stream, honor cancellation and never retry. HttpClient still performs TLS.
    /// </remarks>
    public HttpClientModelHttpTransport(Action<SocketsHttpHandler> configureHandler)
    {
        Throw.IfNull(configureHandler);
        _client = CreateClient(configureHandler);
    }
#endif

#if NETSTANDARD2_0
    private static HttpClient CreateClient()
    {
        // Never silently provide an adapter that violates the transport's no-replay contract.
        throw new PlatformNotSupportedException(
            "The strict HttpClient model transport requires .NET 8 or later. Use IModelHttpTransport on netstandard2.0.");
    }
#else
    private static HttpClient CreateClient(Action<SocketsHttpHandler>? configureHandler = null)
    {
        // One client per transport, never one per operation. The transport owns its handler/client;
        // making it static would cross application transport ownership and disposal boundaries.
#pragma warning disable IDISP001, IDISP014
        var handler = new SocketsHttpHandler();
        try
        {
            configureHandler?.Invoke(handler);
            if (handler.Proxy?.Credentials is not null)
            {
                // Do not mutate an application-owned proxy to remove its credentials.
                throw new ArgumentException("Automatic proxy challenge authentication can replay model HTTP requests.");
            }

            var connect = handler.ConnectCallback;
            handler.AllowAutoRedirect = false;
            handler.AutomaticDecompression = DecompressionMethods.None;
            handler.UseCookies = false;
            handler.Credentials = null;
            handler.PreAuthenticate = false;
            handler.DefaultProxyCredentials = null;
            handler.PooledConnectionLifetime = TimeSpan.Zero;
            handler.ConnectCallback = (context, token) => ConnectOnceAsync(context, connect, token);
            handler.RequestHeaderEncodingSelector = (_, _) => System.Text.Encoding.Latin1;
            handler.ResponseHeaderEncodingSelector = (_, _) => System.Text.Encoding.Latin1;
            return new HttpClient(handler)
            {
                Timeout = Timeout.InfiniteTimeSpan
            };
        }
        catch
        {
            handler.Dispose();
            throw;
        }
#pragma warning restore IDISP001, IDISP014
    }
#endif

#if NET8_0_OR_GREATER
    private static readonly HttpRequestOptionsKey<Operation> OperationKey = new("FoundryLocal.ModelHttpOperation");

    private static async ValueTask<Stream> ConnectOnceAsync(
        SocketsHttpConnectionContext context,
        Func<SocketsHttpConnectionContext, CancellationToken, ValueTask<Stream>>? connect,
        CancellationToken cancellationToken)
    {
        if (!context.InitialRequestMessage.Options.TryGetValue(OperationKey, out var operation) ||
            !operation.TryBeginConnection())
        {
            throw new HttpRequestException("Model HTTP request replay is disabled.");
        }

        cancellationToken.ThrowIfCancellationRequested();
        if (connect is not null)
        {
            return await connect(context, cancellationToken).ConfigureAwait(false);
        }

        // Ownership transfers to NetworkStream on success; connection failures dispose the socket here.
#pragma warning disable IDISP001
        var socket = new Socket(SocketType.Stream, ProtocolType.Tcp) { NoDelay = true };
        try
        {
            // HttpClient still owns HTTP framing, proxies and TLS validation above this plain TCP stream.
            await socket.ConnectAsync(context.DnsEndPoint, cancellationToken).ConfigureAwait(false);
            return new NetworkStream(socket, ownsSocket: true);
        }
        catch
        {
            socket.Dispose();
            throw;
        }
#pragma warning restore IDISP001
    }
#endif

    /// <inheritdoc />
    public IModelHttpOperation CreateOperation(ModelHttpRequest request)
    {
        Throw.IfDisposed(Volatile.Read(ref _disposed) != 0, this);
        Throw.IfNull(request);

#if NET8_0_OR_GREATER
        return new Operation(_client, request);
#else
        throw new PlatformNotSupportedException("The HttpClient model transport requires .NET 8 or later.");
#endif
    }

    /// <summary>Release the connection pool after all transport-dependent managers have been disposed.</summary>
    public void Dispose()
    {
        if (Interlocked.Exchange(ref _disposed, 1) == 0)
        {
            _client.Dispose();
        }
    }

#if NET8_0_OR_GREATER
    private sealed class Operation(HttpClient client, ModelHttpRequest request) : IModelHttpOperation
    {
        private HttpRequestMessage? _message;
        private HttpResponseMessage? _response;
        private Stream? _stream;
        private int _connectionAttempted;

        internal bool TryBeginConnection() => Interlocked.Exchange(ref _connectionAttempted, 1) == 0;

        public async Task<ModelHttpResponseHead> SendAsync(CancellationToken cancellationToken)
        {
            cancellationToken.ThrowIfCancellationRequested();
            if (_message is not null)
            {
                throw new InvalidOperationException("Send can only be called once.");
            }

            // No URI query reconstruction: preserve signed path/query bytes.
            var uri = new Uri(request.Url, new UriCreationOptions { DangerousDisablePathAndQueryCanonicalization = true });
            if ((uri.Scheme != Uri.UriSchemeHttp && uri.Scheme != Uri.UriSchemeHttps) || uri.Fragment.Length != 0)
            {
                throw new NotSupportedException("Only absolute HTTP(S) URLs without fragments are supported.");
            }

            // Single-send operation; fields are assigned once and disposed by the native operation owner.
#pragma warning disable IDISP003
            _message = new HttpRequestMessage(new HttpMethod(request.Method), uri);
            _message.Version = HttpVersion.Version11;
            _message.VersionPolicy = HttpVersionPolicy.RequestVersionExact;
            _message.Options.Set(OperationKey, this);
            // The native coordinator owns retries. Do not reuse pooled connections, which would
            // bypass our one-shot ConnectCallback and could allow HttpClient to replay internally.
            _message.Headers.ConnectionClose = true;
            if (request.Method == "POST")
            {
                _message.Content = new ByteArrayContent(request.Body.ToArray());
            }

            foreach (var header in request.Headers)
            {
                var value = DecodeField(header.Value.Span);
                if (!_message.Headers.TryAddWithoutValidation(header.Name, value))
                {
                    _message.Content ??= new ByteArrayContent(Array.Empty<byte>());
                    if (!_message.Content.Headers.TryAddWithoutValidation(header.Name, value))
                    {
                        throw new InvalidOperationException("Unsupported HTTP field.");
                    }
                }
            }

            _response = await client.SendAsync(_message, HttpCompletionOption.ResponseHeadersRead,
                cancellationToken).ConfigureAwait(false);
            _stream = await _response.Content.ReadAsStreamAsync(cancellationToken).ConfigureAwait(false);
#pragma warning restore IDISP003
            cancellationToken.ThrowIfCancellationRequested();
            var headers = new List<ModelHttpHeader>();
            AddHeaders(_response.Headers, headers);
            AddHeaders(_response.Content.Headers, headers);
            return new ModelHttpResponseHead((int)_response.StatusCode, headers);
        }

        public async Task<int> ReadAsync(Memory<byte> buffer, CancellationToken cancellationToken)
        {
            var stream = _stream ?? throw new InvalidOperationException("Send has not completed.");
            return await stream.ReadAsync(buffer, cancellationToken).ConfigureAwait(false);
        }

        public void Dispose()
        {
            _stream?.Dispose();
            _response?.Dispose();
            _message?.Dispose();
        }

        private static string DecodeField(ReadOnlySpan<byte> bytes)
        {
            // Latin-1 maps HTTP field bytes 1:1.
            var chars = new char[bytes.Length];
            for (var i = 0; i < bytes.Length; i++)
            {
                chars[i] = (char)bytes[i];
            }

            return new string(chars);
        }

        private static void AddHeaders(System.Net.Http.Headers.HttpHeaders source, List<ModelHttpHeader> headers)
        {
            foreach (var field in source.NonValidated)
            {
                foreach (var value in field.Value)
                {
                    var bytes = new byte[value.Length];
                    for (var i = 0; i < value.Length; i++)
                    {
                        if (value[i] > 255)
                        {
                            throw new InvalidOperationException("Unrepresentable HTTP field byte.");
                        }

                        bytes[i] = (byte)value[i];
                    }

                    headers.Add(new ModelHttpHeader(field.Key, bytes));
                }
            }
        }
    }
#endif
}
