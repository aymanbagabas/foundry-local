// --------------------------------------------------------------------------------------------------------------------
// <copyright company="Microsoft">
//   Copyright (c) Microsoft. All rights reserved.
// </copyright>
// --------------------------------------------------------------------------------------------------------------------

namespace Microsoft.AI.Foundry.Local;

/// <summary>Creates client-local model acquisition operations. Implementations must support concurrent operations.</summary>
/// <remarks>Never retry, follow redirects, or decompress responses. Does not cover inference, telemetry, or EP downloads.</remarks>
public interface IModelHttpTransport
{
    /// <summary>Allocate an operation only: do not perform network I/O or wait for network activity here.</summary>
    IModelHttpOperation CreateOperation(ModelHttpRequest request);
}

/// <summary>A single streamed request, owned and disposed by the SDK.</summary>
/// <remarks>
/// Send is called once, then reads are serialized. Distinct operations may execute concurrently.
/// Honor cancellation promptly, including during blocked I/O; cancellation is permanent.
/// The token includes the total send-and-read deadline. Timeouts are cooperative: implementations must honor it.
/// Do not register blocking cancellation callbacks or invoke manager disposal/model acquisition from an operation.
/// </remarks>
public interface IModelHttpOperation : IDisposable
{
    /// <summary>Send and return headers without buffering the body. HTTP errors are successful responses.</summary>
    Task<ModelHttpResponseHead> SendAsync(CancellationToken cancellationToken);

    /// <summary>Read at most buffer.Length bytes. Short reads are valid; zero means permanent EOF.</summary>
    /// <remarks>The buffer is valid only until the returned task completes. The SDK supplies a positive length.</remarks>
    Task<int> ReadAsync(Memory<byte> buffer, CancellationToken cancellationToken);
}

/// <summary>An immutable, duplicate-preserving HTTP field. Values are field bytes, not necessarily UTF-8.</summary>
public sealed class ModelHttpHeader
{
    private readonly byte[] _value;

    /// <summary>Create a field. Names must be ASCII tokens; NUL, CR and LF are forbidden in values.</summary>
    public ModelHttpHeader(string name, ReadOnlyMemory<byte> value)
    {
        if (string.IsNullOrEmpty(name) || name.Any(c => c > 127 ||
            !(char.IsLetterOrDigit(c) || "!#$%&'*+-.^_`|~".Contains(c))))
        {
            throw new ArgumentException("HTTP header names must be nonempty ASCII tokens.", nameof(name));
        }

        _value = value.ToArray();
        if (_value.Any(b => b is 0 or 10 or 13))
        {
            throw new ArgumentException("HTTP header values cannot contain NUL, CR or LF.", nameof(value));
        }

        Name = name;
    }

    /// <summary>The ASCII field name.</summary>
    public string Name { get; }

    /// <summary>A copy of the field bytes.</summary>
    public ReadOnlyMemory<byte> Value => _value.ToArray();
}

/// <summary>Owned immutable request data, valid independently of native request storage.</summary>
public sealed class ModelHttpRequest
{
    private readonly byte[] _body;

    internal ModelHttpRequest(string method, string url, IEnumerable<ModelHttpHeader> headers, byte[] body, ulong timeout)
    {
        Method = method;
        Url = url;
        Headers = Array.AsReadOnly(headers.ToArray());
        _body = body.ToArray();
        TimeoutMilliseconds = timeout;
    }

    /// <summary>GET, HEAD or POST.</summary>
    public string Method { get; }

    /// <summary>The original URL, including path and signed query parameters. Do not log it.</summary>
    public string Url { get; }

    /// <summary>Request fields in original order, including duplicates.</summary>
    public IReadOnlyList<ModelHttpHeader> Headers { get; }

    /// <summary>A copy of the exact request body.</summary>
    public ReadOnlyMemory<byte> Body => _body.ToArray();

    /// <summary>Total send-and-read budget beginning at Send, or zero for no deadline.</summary>
    public ulong TimeoutMilliseconds { get; }
}

/// <summary>Owned response metadata. HTTP errors such as 403 are normal responses.</summary>
public sealed class ModelHttpResponseHead
{
    /// <summary>Create response metadata, preserving repeated fields without comma splitting.</summary>
    public ModelHttpResponseHead(int statusCode, IEnumerable<ModelHttpHeader>? headers = null)
    {
        if (statusCode < 100 || statusCode > 599)
        {
            throw new ArgumentOutOfRangeException(nameof(statusCode));
        }

        StatusCode = statusCode;
        var snapshot = headers?.ToArray() ?? Array.Empty<ModelHttpHeader>();
        if (snapshot.Any(h => h is null))
        {
            throw new ArgumentException("Response headers cannot contain null entries.", nameof(headers));
        }

        Headers = Array.AsReadOnly(snapshot);
    }

    /// <summary>The HTTP status code.</summary>
    public int StatusCode { get; }

    /// <summary>Response fields, including duplicate fields.</summary>
    public IReadOnlyList<ModelHttpHeader> Headers { get; }
}
