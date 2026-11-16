//! Application-owned HTTP for native model catalogs and downloads.

use std::time::Duration;

use crate::error::Result;

/// One HTTP header. The vector of headers preserves order and duplicate names.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct HttpHeader {
    /// An ASCII HTTP field name.
    pub name: String,
    /// A field value. Non-UTF-8 bytes and HTAB are supported; other control bytes are invalid.
    pub value: Vec<u8>,
}

/// An independent copy of a native model HTTP request.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct HttpRequest {
    /// The HTTP method.
    pub method: String,
    /// The UTF-8 URL.
    pub url: String,
    /// Ordered headers, with duplicate names intact.
    pub headers: Vec<HttpHeader>,
    /// The request body.
    pub body: Vec<u8>,
    /// The total budget for Send and all Reads, measured from Send. `None` means no deadline.
    pub timeout: Option<Duration>,
}

/// Response status and headers. The operation supplies the body through `read`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct HttpResponseHead {
    /// An HTTP status code from 100 through 599. HTTP error codes are successful transport results.
    pub status_code: u32,
    /// Ordered headers. The bridge validates and retains a copy until it releases the operation.
    pub headers: Vec<HttpHeader>,
}

/// Creates operations for native model HTTP requests.
///
/// The application supplies HTTP; the native coordinator retains its existing policy.
/// Each operation performs one request, without retries, redirects, or decompression.
/// Distinct operations can run concurrently on SDK worker threads.
/// Implementations must not panic, including in destructors. Fallible callback panics become
/// internal errors. A panic in cancellation or destruction aborts the process.
/// Rust panic hooks still run; do not put secrets in panic messages.
pub trait ModelHttpTransport: Send + Sync + 'static {
    /// Create local state without network I/O or waits for network activity.
    ///
    /// The request owns all its data and can outlive this call.
    fn create_operation(&self, request: HttpRequest) -> Result<Box<dyn ModelHttpOperation>>;
}

/// One synchronous, streaming HTTP exchange.
///
/// The bridge serializes `send` and `read`. It can call `cancel` concurrently.
/// Enforce the request's total timeout across calls, not separately for each read.
/// Return errors with `NativeErrorCode::OperationCancelled` for cancellation.
/// Other transport failures normally use `NativeErrorCode::Network`.
/// The bridge forwards error codes but replaces diagnostics to protect request secrets.
/// Methods and destructors must not panic; see [`ModelHttpTransport`].
pub trait ModelHttpOperation: Send + Sync + 'static {
    /// Send once and return when the response head is available, without buffering the body.
    fn send(&self) -> Result<HttpResponseHead>;

    /// Read at most `buffer.len()` bytes. A short read is valid; zero means permanent EOF.
    fn read(&self, buffer: &mut [u8]) -> Result<usize>;

    /// Interrupt pending I/O without blocking. This must be thread-safe, idempotent and sticky.
    ///
    /// Cancellation can precede `send`. The bridge rejects subsequent calls after cancellation.
    /// A panic aborts the process; cancellation has no error return.
    fn cancel(&self);
}
