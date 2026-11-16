// --------------------------------------------------------------------------------------------------------------------
// <copyright company="Microsoft">
//   Copyright (c) Microsoft. All rights reserved.
// </copyright>
// --------------------------------------------------------------------------------------------------------------------

namespace Microsoft.AI.Foundry.Local.Detail;

using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Threading;

using Microsoft.AI.Foundry.Local.Detail.Interop;
using Microsoft.AI.Foundry.Local.Detail.Native;

/// <summary>API5/transport1 bridge. Payload API2 structures are deliberately unchanged.</summary>
internal sealed class ModelHttpTransportBridge : IDisposable
{
    [UnmanagedFunctionPointer(CallingConvention.Winapi)]
    internal delegate IntPtr SetTransportDelegate(IntPtr configuration, IntPtr transport);
    [UnmanagedFunctionPointer(CallingConvention.Winapi)]
    internal delegate void LifetimeDelegate(IntPtr context);
    [UnmanagedFunctionPointer(CallingConvention.Winapi)]
    internal delegate IntPtr CreateDelegate(IntPtr context, IntPtr request, out IntPtr operation);
    [UnmanagedFunctionPointer(CallingConvention.Winapi)]
    internal delegate IntPtr SendDelegate(IntPtr operation, ref ResponseData response);
    [UnmanagedFunctionPointer(CallingConvention.Winapi)]
    internal delegate IntPtr ReadDelegate(IntPtr operation, IntPtr buffer, UIntPtr capacity, out UIntPtr count);

    [StructLayout(LayoutKind.Sequential)]
    internal struct HeaderData
    {
        internal IntPtr Name;
        internal IntPtr Value;
    }

    [StructLayout(LayoutKind.Sequential)]
    internal struct RequestData
    {
        internal uint Version;
        internal IntPtr Method;
        internal IntPtr Url;
        internal IntPtr Headers;
        internal UIntPtr HeaderCount;
        internal IntPtr Body;
        internal UIntPtr BodySize;
        internal ulong TimeoutMilliseconds;
    }

    [StructLayout(LayoutKind.Sequential)]
    internal struct ResponseData
    {
        internal uint Version;
        internal uint StatusCode;
        internal IntPtr Headers;
        internal UIntPtr HeaderCount;
    }

    [StructLayout(LayoutKind.Sequential)]
    internal struct TransportData
    {
        internal uint Version;
        internal IntPtr Context;
        internal LifetimeDelegate Retain;
        internal LifetimeDelegate Release;
        internal CreateDelegate Create;
        internal SendDelegate Send;
        internal ReadDelegate Read;
        internal LifetimeDelegate Cancel;
        internal LifetimeDelegate ReleaseOperation;
    }

    // Static roots outlive every native registration, even when the caller drops all managed references.
    // DllLoader's libraries are process-lifetime loads (never freed); Api.Root is the SAME loaded runtime
    // used for registration and for status allocation, including callbacks after configuration disposal.
    private static readonly LifetimeDelegate RetainCallback = Retain;
    private static readonly LifetimeDelegate ReleaseCallback = Release;
    private static readonly CreateDelegate CreateCallback = Create;
    private static readonly SendDelegate SendCallback = Send;
    private static readonly ReadDelegate ReadCallback = Read;
    private static readonly LifetimeDelegate CancelCallback = Cancel;
    private static readonly LifetimeDelegate ReleaseOperationCallback = ReleaseOperation;

    private readonly IModelHttpTransport _transport;
    private GCHandle _handle;
    private int _references = 1;
    private int _disposed;

    internal ModelHttpTransportBridge(IModelHttpTransport transport)
    {
        _transport = transport;
        _handle = GCHandle.Alloc(this);
    }

    internal void Register(IntPtr configuration, SetTransportDelegate setter)
    {
        var data = new TransportData
        {
            Version = 1,
            Context = GCHandle.ToIntPtr(_handle),
            Retain = RetainCallback,
            Release = ReleaseCallback,
            Create = CreateCallback,
            Send = SendCallback,
            Read = ReadCallback,
            Cancel = CancelCallback,
            ReleaseOperation = ReleaseOperationCallback
        };
        var ptr = Marshal.AllocHGlobal(Marshal.SizeOf<TransportData>());
        try
        {
            Marshal.StructureToPtr(data, ptr, false);
            Api.CheckStatus(setter(configuration, ptr));
        }
        finally
        {
            Marshal.FreeHGlobal(ptr);
        }
    }

    public void Dispose()
    {
        if (Interlocked.Exchange(ref _disposed, 1) == 0)
        {
            ReleaseReference();
        }
    }

    private void ReleaseReference()
    {
        if (Interlocked.Decrement(ref _references) == 0)
        {
            _handle.Free();
        }
    }

    private static T Target<T>(IntPtr ptr) where T : class =>
        GCHandle.FromIntPtr(ptr).Target as T ?? throw new InvalidOperationException("Invalid transport handle.");

    private static void Retain(IntPtr context)
    {
        try
        {
            Interlocked.Increment(ref Target<ModelHttpTransportBridge>(context)._references);
        }
        catch (Exception)
        {
            // Lifecycle callbacks cannot report failures across the ABI.
        }
    }

    private static void Release(IntPtr context)
    {
        try
        {
            Target<ModelHttpTransportBridge>(context).ReleaseReference();
        }
        catch (Exception)
        {
            // Lifecycle callbacks cannot report failures across the ABI.
        }
    }

    private static IntPtr Failure(bool cancelled = false)
    {
        try
        {
            // Never disclose exception messages, URLs, credentials or body data.
            var status = Api.Root.StatusCreate(
                cancelled ? FlErrorCode.OperationCancelled : FlErrorCode.Network,
                cancelled ? "Model HTTP operation cancelled." : "Model HTTP transport failed.");
            if (status != IntPtr.Zero)
            {
                return status; // Ownership transfers to this runtime.
            }
        }
        catch (Exception)
        {
            // Returning success on an allocation failure would violate the C contract.
        }

        Environment.FailFast("Unable to allocate a native model HTTP error status.");
        return IntPtr.Zero;
    }

    private static IntPtr Create(IntPtr context, IntPtr request, out IntPtr operation)
    {
        operation = IntPtr.Zero;
        Operation? state = null;
        try
        {
            var owner = Target<ModelHttpTransportBridge>(context);
            var data = Marshal.PtrToStructure<RequestData>(request);
            if (data.Version != 1)
            {
                throw new ArgumentException("Unsupported transport request.");
            }

            var headers = new List<ModelHttpHeader>();
            var count = checked((int)data.HeaderCount.ToUInt64());
            for (var i = 0; i < count; i++)
            {
                var header = Marshal.PtrToStructure<HeaderData>(
                    IntPtr.Add(data.Headers, checked(i * Marshal.SizeOf<HeaderData>())));
                headers.Add(new ModelHttpHeader(ReadAscii(header.Name), ReadFieldBytes(header.Value)));
            }

            var body = new byte[checked((int)data.BodySize.ToUInt64())];
            if (body.Length > 0)
            {
                Marshal.Copy(data.Body, body, 0, body.Length);
            }

            var snapshot = new ModelHttpRequest(ReadAscii(data.Method),
                Utf8.PtrToString(data.Url) ?? throw new ArgumentException("Missing URL."),
                headers, body, data.TimeoutMilliseconds);
            // Ownership moves into state, then into the native GCHandle, released in ReleaseOperation.
#pragma warning disable IDISP001, IDISP003
            state = new Operation(owner._transport.CreateOperation(snapshot)
                ?? throw new InvalidOperationException("Transport returned a null operation."), snapshot);
            operation = GCHandle.ToIntPtr(GCHandle.Alloc(state));
            state = null; // Native owns the operation and its GCHandle from here.
#pragma warning restore IDISP001, IDISP003
            return IntPtr.Zero;
        }
        catch (Exception error)
        {
            try
            {
                state?.Dispose();
            }
            catch (Exception)
            {
                // Cleanup must not escape the callback.
            }

            return Failure(error is OperationCanceledException);
        }
    }

    private static byte[] ReadFieldBytes(IntPtr ptr)
    {
        if (ptr == IntPtr.Zero)
        {
            throw new ArgumentException("Missing HTTP field.");
        }

        var length = 0;
        while (Marshal.ReadByte(ptr, length) != 0)
        {
            length = checked(length + 1);
        }

        var bytes = new byte[length];
        Marshal.Copy(ptr, bytes, 0, length);
        return bytes;
    }

    private static string ReadAscii(IntPtr ptr)
    {
        var bytes = ReadFieldBytes(ptr);
        if (bytes.Any(b => b > 127))
        {
            throw new ArgumentException("Invalid ASCII field.");
        }

        return System.Text.Encoding.ASCII.GetString(bytes);
    }

    private static IntPtr Send(IntPtr operation, ref ResponseData response)
    {
        try
        {
            var state = Target<Operation>(operation);
            state.Send(ref response);
            return IntPtr.Zero;
        }
        catch (Exception error)
        {
            return OperationFailure(operation, error);
        }
    }

    private static IntPtr Read(IntPtr operation, IntPtr buffer, UIntPtr capacity, out UIntPtr count)
    {
        count = UIntPtr.Zero;
        try
        {
            var state = Target<Operation>(operation);
            count = new UIntPtr(checked((uint)state.Read(buffer, capacity)));
            return IntPtr.Zero;
        }
        catch (Exception error)
        {
            return OperationFailure(operation, error);
        }
    }

    private static IntPtr OperationFailure(IntPtr operation, Exception error)
    {
        try
        {
            var state = Target<Operation>(operation);
            // Local expiry wins over cancellation. The native coordinator attributes its own deadline monitor.
            return Failure(!state.IsTimedOut && (state.IsCancelled || error is OperationCanceledException));
        }
        catch (Exception)
        {
            return Failure();
        }
    }

    private static void Cancel(IntPtr operation)
    {
        try
        {
            Target<Operation>(operation).Cancel();
        }
        catch (Exception)
        {
            // Includes misbehaving user cancellation registrations.
        }
    }

    private static void ReleaseOperation(IntPtr operation)
    {
        if (operation == IntPtr.Zero)
        {
            return;
        }

        try
        {
            var handle = GCHandle.FromIntPtr(operation);
            try
            {
                (handle.Target as Operation)?.Dispose();
            }
            finally
            {
                handle.Free();
            }
        }
        catch (Exception)
        {
            // A user Dispose exception cannot cross the ABI.
        }
    }

    // Capture the immutable request as well as the implementation until native releases this operation.
    private sealed class Operation(IModelHttpOperation implementation, ModelHttpRequest request) : IDisposable
    {
        private readonly CancellationTokenSource _cancellation = new();
        private readonly object _ioLock = new();
        private readonly List<IntPtr> _allocations = new();
        private readonly Stopwatch _elapsed = new();
        private byte[] _readBuffer = Array.Empty<byte>();
        private Timer? _timer;
        private int _cancelled;
        private int _timedOut;
        private bool _sent;
        private bool _sendCompleted;
        private bool _eof;

        internal bool IsCancelled => Volatile.Read(ref _cancelled) != 0;
        internal bool IsTimedOut => Volatile.Read(ref _timedOut) != 0;

        internal void Send(ref ResponseData response)
        {
            lock (_ioLock)
            {
                CheckCancellation();
                if (_sent || response.Version != 1)
                {
                    throw new InvalidOperationException("Invalid send sequence.");
                }

                _sent = true;
                _elapsed.Start();
                if (request.TimeoutMilliseconds != 0)
                {
                    // Send is single-shot, so this is assigned once and drained in Dispose.
#pragma warning disable IDISP003
                    _timer = new Timer(CheckDeadline, null, NextDelay(), Timeout.Infinite);
#pragma warning restore IDISP003
                }

                var head = implementation.SendAsync(_cancellation.Token).GetAwaiter().GetResult();
                CheckCancellation();
                response.StatusCode = checked((uint)head.StatusCode);
                response.HeaderCount = new UIntPtr(checked((uint)head.Headers.Count));
                response.Headers = IntPtr.Zero;
                if (head.Headers.Count > 0)
                {
                    response.Headers = Allocate(checked(head.Headers.Count * Marshal.SizeOf<HeaderData>()));
                    for (var i = 0; i < head.Headers.Count; i++)
                    {
                        var header = head.Headers[i];
                        var data = new HeaderData
                        {
                            Name = Field(System.Text.Encoding.ASCII.GetBytes(header.Name)),
                            Value = Field(header.Value.ToArray())
                        };
                        Marshal.StructureToPtr(data, IntPtr.Add(response.Headers,
                            checked(i * Marshal.SizeOf<HeaderData>())), false);
                    }
                }

                _sendCompleted = true;
            }
        }

        internal int Read(IntPtr destination, UIntPtr capacity)
        {
            lock (_ioLock)
            {
                CheckCancellation();
                if (!_sendCompleted || capacity == UIntPtr.Zero)
                {
                    throw new InvalidOperationException("Invalid read sequence.");
                }

                if (_eof)
                {
                    return 0;
                }

                // Bounded scratch storage, never an entire model. It also keeps managed async operations
                // independent of borrowed native buffers. Short reads are explicitly permitted.
                var length = (int)Math.Min(capacity.ToUInt64(), 64 * 1024UL);
                if (_readBuffer.Length < length)
                {
                    _readBuffer = new byte[length];
                }

                var read = implementation.ReadAsync(_readBuffer.AsMemory(0, length), _cancellation.Token)
                    .GetAwaiter().GetResult();
                CheckCancellation();
                if (read < 0 || read > length)
                {
                    throw new InvalidOperationException("Invalid response read count.");
                }

                Marshal.Copy(_readBuffer, 0, destination, read);
                _eof = read == 0;
                return read;
            }
        }

        private int NextDelay() =>
            (int)Math.Max(1, Math.Min(int.MaxValue,
                (double)request.TimeoutMilliseconds - _elapsed.Elapsed.TotalMilliseconds));

        private void CheckCancellation()
        {
            if (request.TimeoutMilliseconds != 0 && _elapsed.IsRunning &&
                _elapsed.Elapsed.TotalMilliseconds >= request.TimeoutMilliseconds)
            {
                Interlocked.Exchange(ref _timedOut, 1);
                _cancellation.Cancel();
            }

            _cancellation.Token.ThrowIfCancellationRequested();
        }

        private void CheckDeadline(object? _)
        {
            try
            {
                if (_elapsed.Elapsed.TotalMilliseconds >= request.TimeoutMilliseconds)
                {
                    Interlocked.Exchange(ref _timedOut, 1);
                    _cancellation.Cancel();
                }
                else
                {
                    _timer?.Change(NextDelay(), Timeout.Infinite);
                }
            }
            catch (Exception)
            {
                // Timer disposal and user cancellation callbacks must not escape on a thread-pool thread.
            }
        }

        internal void Cancel()
        {
            Interlocked.Exchange(ref _cancelled, 1);
            _cancellation.Cancel(); // Never take _ioLock: Send/Read may be blocked.
        }

        private IntPtr Allocate(int size)
        {
            var ptr = Marshal.AllocHGlobal(size);
            _allocations.Add(ptr);
            return ptr;
        }

        private IntPtr Field(byte[] bytes)
        {
            var ptr = Allocate(checked(bytes.Length + 1));
            Marshal.Copy(bytes, 0, ptr, bytes.Length);
            Marshal.WriteByte(ptr, bytes.Length, 0);
            return ptr;
        }

        public void Dispose()
        {
            // Native guarantees Send/Read/Cancel drained. Drain the independent deadline timer too.
            if (_timer is not null)
            {
                using var drained = new ManualResetEvent(false);
                // This is Timer.Dispose(WaitHandle), not this Operation's Dispose overload.
#pragma warning disable IDISP021
                if (_timer.Dispose(drained))
                {
                    drained.WaitOne();
                }
#pragma warning restore IDISP021
            }

            try
            {
                // Ownership was transferred by IModelHttpTransport.CreateOperation.
#pragma warning disable IDISP007
                implementation.Dispose();
#pragma warning restore IDISP007
            }
            finally
            {
                _cancellation.Dispose();
                foreach (var ptr in _allocations)
                {
                    Marshal.FreeHGlobal(ptr);
                }
            }
        }
    }
}
