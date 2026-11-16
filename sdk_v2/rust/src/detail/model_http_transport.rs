//! Owning callback bridge for the version-5 model HTTP extension.

use std::ffi::{c_void, CStr, CString};
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::ptr;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use super::api::Api;
use super::ffi::*;
use crate::error::{FoundryLocalError, NativeErrorCode, Result};
use crate::{HttpHeader, HttpRequest, HttpResponseHead, ModelHttpOperation, ModelHttpTransport};

struct Context {
    transport: Arc<dyn ModelHttpTransport>,
    // Drop application code before the library. Api never owns this context.
    api: Arc<Api>,
}

struct ContextLease(*const Context);

impl Drop for ContextLease {
    fn drop(&mut self) {
        // Balance the initial into_raw reference, independent of native snapshots.
        unsafe { release(self.0.cast_mut().cast()) };
    }
}

struct Operation {
    inner: Box<dyn ModelHttpOperation>,
    state: Mutex<State>,
    cancelled: AtomicBool,
    context: Arc<Context>,
}

#[derive(Default)]
struct State {
    response: Option<OwnedResponse>,
    send_started: bool,
    eof: bool,
}

struct OwnedResponse {
    _strings: Vec<(CString, CString)>,
    headers: Vec<flHttpHeader>,
    status_code: u32,
}

// SAFETY: pointers reference immutable CString allocations owned by this value.
// The operation mutex serializes access; moving the vectors does not move their allocations.
unsafe impl Send for OwnedResponse {}

fn invalid(reason: &'static str) -> FoundryLocalError {
    FoundryLocalError::Validation {
        reason: reason.into(),
    }
}

fn cancelled() -> FoundryLocalError {
    FoundryLocalError::Native {
        code: NativeErrorCode::OperationCancelled,
        message: "model HTTP operation cancelled".into(),
    }
}

fn token(bytes: &[u8]) -> bool {
    !bytes.is_empty()
        && bytes
            .iter()
            .all(|b| b.is_ascii_alphanumeric() || b"!#$%&'*+-.^_`|~".contains(b))
}

fn validate_header(header: &HttpHeader) -> Result<()> {
    if !token(header.name.as_bytes())
        || !header
            .value
            .iter()
            .all(|&b| b == b'\t' || (b >= 32 && b != 127))
    {
        return Err(invalid("invalid model HTTP header encoding"));
    }
    Ok(())
}

fn validate_count<T>(count: usize) -> Result<()> {
    if count > isize::MAX as usize / std::mem::size_of::<T>() {
        return Err(invalid(
            "model HTTP array length exceeds the addressable size",
        ));
    }
    Ok(())
}

unsafe fn bytes<'a, T>(data: *const T, count: usize) -> Result<&'a [T]> {
    validate_count::<T>(count)?;
    if count == 0 {
        return Ok(&[]);
    }
    if data.is_null() {
        return Err(invalid("null model HTTP array"));
    }
    // SAFETY: native supplies an initialized allocation of the specified extent.
    Ok(std::slice::from_raw_parts(data, count))
}

unsafe fn string<'a>(data: *const std::os::raw::c_char) -> Result<&'a str> {
    if data.is_null() {
        return Err(invalid("null model HTTP string"));
    }
    CStr::from_ptr(data)
        .to_str()
        .map_err(|_| invalid("invalid model HTTP UTF-8 string"))
}

unsafe fn copy_request(request: *const flHttpRequestData) -> Result<HttpRequest> {
    let request = request
        .as_ref()
        .ok_or_else(|| invalid("null model HTTP request"))?;
    if request.version != FOUNDRY_LOCAL_HTTP_TRANSPORT_VERSION {
        return Err(invalid("unsupported model HTTP transport payload version"));
    }
    let method = string(request.method)?.to_owned();
    if !token(method.as_bytes()) {
        return Err(invalid("invalid model HTTP method"));
    }
    let url = string(request.url)?.to_owned();
    let mut headers = Vec::new();
    for header in bytes(request.headers, request.header_count)? {
        if header.value.is_null() {
            return Err(invalid("null model HTTP header value"));
        }
        let header = HttpHeader {
            name: string(header.name)?.to_owned(),
            value: CStr::from_ptr(header.value).to_bytes().to_vec(),
        };
        validate_header(&header)?;
        headers.push(header);
    }
    Ok(HttpRequest {
        method,
        url,
        headers,
        body: bytes(request.body, request.body_size)?.to_vec(),
        timeout: (request.timeout_ms != 0).then(|| Duration::from_millis(request.timeout_ms)),
    })
}

impl OwnedResponse {
    fn new(response: HttpResponseHead) -> Result<Self> {
        if !(100..=599).contains(&response.status_code) {
            return Err(invalid("invalid model HTTP response status"));
        }
        validate_count::<flHttpHeader>(response.headers.len())?;
        let strings = response
            .headers
            .into_iter()
            .map(|header| {
                validate_header(&header)?;
                // Header validation excludes NUL; keep the conversions fallible.
                Ok((
                    CString::new(header.name)
                        .map_err(|_| invalid("invalid model HTTP header name"))?,
                    CString::new(header.value)
                        .map_err(|_| invalid("invalid model HTTP header value"))?,
                ))
            })
            .collect::<Result<Vec<_>>>()?;
        let headers = strings
            .iter()
            .map(|(name, value)| flHttpHeader {
                name: name.as_ptr(),
                value: value.as_ptr(),
            })
            .collect();
        Ok(Self {
            _strings: strings,
            headers,
            status_code: response.status_code,
        })
    }
}

/// Register a copied table. Native ownership begins with its synchronous Retain.
pub(crate) fn register(
    api: &Arc<Api>,
    config: *mut flConfiguration,
    transport: Arc<dyn ModelHttpTransport>,
) -> Result<()> {
    let setter = api.model_http_transport_setter()?;
    let context = ContextLease(Arc::into_raw(Arc::new(Context {
        transport,
        api: Arc::clone(api),
    })));
    let table = table(&context);
    // SAFETY: native copies the table and retains the context before this returns.
    api.check(unsafe { setter(config, &table) })
}

fn table(context: &ContextLease) -> flHttpTransport {
    flHttpTransport {
        version: FOUNDRY_LOCAL_HTTP_TRANSPORT_VERSION,
        user_data: context.0.cast_mut().cast(),
        Retain: retain,
        Release: release,
        CreateOperation: create,
        Send: send,
        Read: read,
        Cancel: cancel,
        ReleaseOperation: release_operation,
    }
}

fn error_status(api: &Api, error: FoundryLocalError) -> flStatusPtr {
    let code = match error.native_code() {
        Some(NativeErrorCode::OperationCancelled) => FOUNDRY_LOCAL_ERROR_OPERATION_CANCELLED,
        Some(NativeErrorCode::Internal) => FOUNDRY_LOCAL_ERROR_INTERNAL,
        Some(NativeErrorCode::NotImplemented) => FOUNDRY_LOCAL_ERROR_NOT_IMPLEMENTED,
        Some(NativeErrorCode::InvalidUsage) => FOUNDRY_LOCAL_ERROR_INVALID_USAGE,
        Some(NativeErrorCode::InvalidArgument) => FOUNDRY_LOCAL_ERROR_INVALID_ARGUMENT,
        Some(NativeErrorCode::Unknown(code)) if code != FOUNDRY_LOCAL_OK => code,
        Some(NativeErrorCode::Ok | NativeErrorCode::Unknown(FOUNDRY_LOCAL_OK)) => {
            FOUNDRY_LOCAL_ERROR_INTERNAL
        }
        _ if matches!(error, FoundryLocalError::Validation { .. }) => {
            FOUNDRY_LOCAL_ERROR_INVALID_ARGUMENT
        }
        _ if matches!(error, FoundryLocalError::Internal { .. }) => FOUNDRY_LOCAL_ERROR_INTERNAL,
        _ => FOUNDRY_LOCAL_ERROR_NETWORK,
    };
    // Never expose application errors, URLs, credentials, headers, or panic payloads.
    unsafe { (api.root().Status_Create)(code, b"model HTTP callback failed\0".as_ptr().cast()) }
}

fn fallible(api: &Api, callback: impl FnOnce() -> Result<()>) -> flStatusPtr {
    match catch_unwind(AssertUnwindSafe(callback)) {
        Ok(Ok(())) => ptr::null_mut(),
        Ok(Err(error)) => error_status(api, error),
        Err(payload) => {
            // A custom panic payload can itself panic in Drop.
            std::mem::forget(payload);
            error_status(
                api,
                FoundryLocalError::Internal {
                    reason: "model HTTP callback panicked".into(),
                },
            )
        }
    }
}

fn infallible(callback: impl FnOnce()) {
    if let Err(_payload) = catch_unwind(AssertUnwindSafe(callback)) {
        // A lifecycle or cancellation failure has no C error channel.
        std::process::abort();
    }
}

unsafe extern "system" fn retain(data: *mut c_void) {
    infallible(|| Arc::increment_strong_count(data.cast::<Context>()));
}

unsafe extern "system" fn release(data: *mut c_void) {
    infallible(|| drop(Arc::from_raw(data.cast::<Context>())));
}

unsafe extern "system" fn create(
    data: *mut c_void,
    request: *const flHttpRequestData,
    out: *mut *mut c_void,
) -> flStatusPtr {
    let context = &*data.cast::<Context>();
    fallible(&context.api, || {
        if out.is_null() {
            return Err(invalid("null model HTTP operation output"));
        }
        *out = ptr::null_mut();
        let request = copy_request(request)?;
        let inner = context.transport.create_operation(request)?;
        Arc::increment_strong_count(data.cast::<Context>());
        let operation = Box::new(Operation {
            inner,
            state: Mutex::new(State::default()),
            cancelled: AtomicBool::new(false),
            context: Arc::from_raw(data.cast::<Context>()),
        });
        *out = Box::into_raw(operation).cast();
        Ok(())
    })
}

unsafe extern "system" fn send(
    operation: *mut c_void,
    response: *mut flHttpResponseData,
) -> flStatusPtr {
    let operation = &*operation.cast::<Operation>();
    fallible(&operation.context.api, || {
        let response = response
            .as_mut()
            .ok_or_else(|| invalid("null model HTTP response"))?;
        if response.version != FOUNDRY_LOCAL_HTTP_TRANSPORT_VERSION {
            return Err(invalid("unsupported model HTTP response version"));
        }
        response.status_code = 0;
        response.headers = ptr::null();
        response.header_count = 0;
        if operation.cancelled.load(Ordering::Acquire) {
            return Err(cancelled());
        }
        let mut state = operation
            .state
            .lock()
            .map_err(|_| FoundryLocalError::Internal {
                reason: "model HTTP state poisoned".into(),
            })?;
        if operation.cancelled.load(Ordering::Acquire) {
            return Err(cancelled());
        }
        if state.send_started {
            return Err(invalid("model HTTP Send called more than once"));
        }
        state.send_started = true;
        let result = operation.inner.send();
        if operation.cancelled.load(Ordering::Acquire) {
            return Err(cancelled());
        }
        let owned = OwnedResponse::new(result?)?;
        state.response = Some(owned);
        let owned = state.response.as_ref().expect("response just stored");
        response.status_code = owned.status_code;
        response.headers = owned.headers.as_ptr();
        response.header_count = owned.headers.len();
        Ok(())
    })
}

unsafe extern "system" fn read(
    operation: *mut c_void,
    buffer: *mut u8,
    size: usize,
    out: *mut usize,
) -> flStatusPtr {
    let operation = &*operation.cast::<Operation>();
    fallible(&operation.context.api, || {
        if out.is_null() {
            return Err(invalid("null model HTTP read output"));
        }
        *out = 0;
        if buffer.is_null() || size == 0 {
            return Err(invalid("invalid model HTTP read buffer"));
        }
        validate_count::<u8>(size)?;
        if operation.cancelled.load(Ordering::Acquire) {
            return Err(cancelled());
        }
        let mut state = operation
            .state
            .lock()
            .map_err(|_| FoundryLocalError::Internal {
                reason: "model HTTP state poisoned".into(),
            })?;
        if operation.cancelled.load(Ordering::Acquire) {
            return Err(cancelled());
        }
        if state.response.is_none() {
            return Err(invalid("model HTTP Read requires successful Send"));
        }
        if state.eof {
            return Ok(());
        }
        let result = operation
            .inner
            .read(std::slice::from_raw_parts_mut(buffer, size));
        if operation.cancelled.load(Ordering::Acquire) {
            return Err(cancelled());
        }
        let count = result?;
        if count > size {
            return Err(invalid("model HTTP Read exceeded buffer length"));
        }
        state.eof = count == 0;
        *out = count;
        Ok(())
    })
}

unsafe extern "system" fn cancel(operation: *mut c_void) {
    infallible(|| {
        let operation = &*operation.cast::<Operation>();
        if !operation.cancelled.swap(true, Ordering::AcqRel) {
            operation.inner.cancel();
        }
    });
}

unsafe extern "system" fn release_operation(operation: *mut c_void) {
    infallible(|| {
        if !operation.is_null() {
            drop(Box::from_raw(operation.cast::<Operation>()));
        }
    });
}

#[cfg(test)]
#[path = "model_http_transport_tests.rs"]
mod tests;
