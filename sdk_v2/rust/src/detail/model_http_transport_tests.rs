use super::*;
use crate::detail::api::catalog_tests::Fixture;
use std::sync::atomic::AtomicUsize;
use std::sync::{mpsc, Condvar};

#[derive(Clone, Copy)]
enum Mode {
    Normal,
    BadStatus,
    BadHeader,
    BadRead,
    InspectReadBuffer,
    PanicCreate,
    PanicSend,
    PanicRead,
    PanicCancel,
    ErrorSend,
    BlockSend,
    BlockRead,
}

#[derive(Default)]
struct Shared {
    request: Mutex<Option<HttpRequest>>,
    position: Mutex<usize>,
    cancels: AtomicUsize,
    operation_drops: AtomicUsize,
    transport_drops: AtomicUsize,
    entered: Mutex<Option<mpsc::Sender<()>>>,
    stopped: Mutex<bool>,
    wake: Condvar,
}

struct Transport(Arc<Shared>, Mode);
struct Exchange(Arc<Shared>, Mode);

impl Drop for Transport {
    fn drop(&mut self) {
        self.0.transport_drops.fetch_add(1, Ordering::SeqCst);
    }
}

impl Drop for Exchange {
    fn drop(&mut self) {
        self.0.operation_drops.fetch_add(1, Ordering::SeqCst);
    }
}

impl ModelHttpTransport for Transport {
    fn create_operation(&self, request: HttpRequest) -> Result<Box<dyn ModelHttpOperation>> {
        if matches!(self.1, Mode::PanicCreate) {
            panic!("secret create diagnostic");
        }
        *self.0.request.lock().unwrap() = Some(request);
        Ok(Box::new(Exchange(Arc::clone(&self.0), self.1)))
    }
}

impl Exchange {
    fn wait_for_cancel(&self) {
        self.0
            .entered
            .lock()
            .unwrap()
            .take()
            .unwrap()
            .send(())
            .unwrap();
        let (_guard, timeout) = self
            .0
            .wake
            .wait_timeout_while(
                self.0.stopped.lock().unwrap(),
                Duration::from_secs(10),
                |stopped| !*stopped,
            )
            .unwrap();
        assert!(
            !timeout.timed_out(),
            "Cancel did not interrupt the callback"
        );
    }
}

impl ModelHttpOperation for Exchange {
    fn send(&self) -> Result<HttpResponseHead> {
        match self.1 {
            Mode::PanicSend => panic!("secret send diagnostic"),
            Mode::BlockSend => self.wait_for_cancel(),
            Mode::ErrorSend => {
                return Err(FoundryLocalError::Native {
                    code: NativeErrorCode::Network,
                    message: "https://secret:password@example.invalid/?token=secret".into(),
                })
            }
            _ => {}
        }
        Ok(HttpResponseHead {
            status_code: if matches!(self.1, Mode::BadStatus) {
                600
            } else {
                200
            },
            headers: vec![
                HttpHeader {
                    name: "Set-Cookie".into(),
                    value: if matches!(self.1, Mode::BadHeader) {
                        b"secret\r\nInjected: true".to_vec()
                    } else {
                        vec![b'a', b'=', 0xff]
                    },
                },
                HttpHeader {
                    name: "Set-Cookie".into(),
                    value: b"b=2".to_vec(),
                },
            ],
        })
    }

    fn read(&self, buffer: &mut [u8]) -> Result<usize> {
        match self.1 {
            Mode::PanicRead => panic!("secret read diagnostic"),
            Mode::BadRead => return Ok(buffer.len() + 1),
            Mode::BlockRead => self.wait_for_cancel(),
            Mode::InspectReadBuffer => {
                assert!(buffer.iter().all(|byte| *byte == 0));
            }
            _ => {}
        }
        let mut position = self.0.position.lock().unwrap();
        let body = b"abc";
        let count = (body.len() - *position).min(buffer.len()).min(1);
        buffer[..count].copy_from_slice(&body[*position..*position + count]);
        *position += count;
        Ok(count)
    }

    fn cancel(&self) {
        if matches!(self.1, Mode::PanicCancel) {
            panic!("cancel failed");
        }
        self.0.cancels.fetch_add(1, Ordering::SeqCst);
        *self.0.stopped.lock().unwrap() = true;
        self.0.wake.notify_all();
    }
}

struct Bridge {
    context: Arc<Context>,
    _lease: ContextLease,
    table: flHttpTransport,
    operation: *mut c_void,
    shared: Arc<Shared>,
    _fixture: Fixture,
}

impl Bridge {
    fn new(mode: Mode) -> Self {
        let fixture = Fixture::for_transport();
        let shared = Arc::new(Shared::default());
        let context = Arc::new(Context {
            transport: Arc::new(Transport(Arc::clone(&shared), mode)),
            api: fixture.api(),
        });
        let lease = ContextLease(Arc::into_raw(Arc::clone(&context)));
        Self {
            table: table(&lease),
            operation: ptr::null_mut(),
            context,
            _lease: lease,
            shared,
            _fixture: fixture,
        }
    }

    fn check(&self, status: flStatusPtr) -> Result<()> {
        self.context.api.check(status)
    }

    fn create(&mut self) -> Result<()> {
        // All of this storage disappears before Send. The public request must own its copy.
        let method = CString::new("POST").unwrap();
        let url = CString::new("https://example.invalid/catalog?token=secret").unwrap();
        let name = CString::new("X-Example").unwrap();
        let value = CString::new(vec![b'a', 0xfe]).unwrap();
        let headers = [
            flHttpHeader {
                name: name.as_ptr(),
                value: value.as_ptr(),
            },
            flHttpHeader {
                name: name.as_ptr(),
                value: value.as_ptr(),
            },
        ];
        let body = [0, 1, 255];
        let request = flHttpRequestData {
            version: FOUNDRY_LOCAL_HTTP_TRANSPORT_VERSION,
            method: method.as_ptr(),
            url: url.as_ptr(),
            headers: headers.as_ptr(),
            header_count: headers.len(),
            body: body.as_ptr(),
            body_size: body.len(),
            timeout_ms: 1234,
        };
        let status = unsafe {
            (self.table.CreateOperation)(self.table.user_data, &request, &mut self.operation)
        };
        self.check(status)
    }

    fn start(mode: Mode) -> Self {
        let mut bridge = Self::new(mode);
        bridge.create().unwrap();
        bridge
    }

    fn send(&self) -> Result<flHttpResponseData> {
        let mut response = flHttpResponseData {
            version: FOUNDRY_LOCAL_HTTP_TRANSPORT_VERSION,
            status_code: 0,
            headers: ptr::null(),
            header_count: 0,
        };
        self.check(unsafe { (self.table.Send)(self.operation, &mut response) })?;
        Ok(response)
    }

    fn read(&self, buffer: &mut [u8]) -> Result<usize> {
        let mut count = usize::MAX;
        self.check(unsafe {
            (self.table.Read)(
                self.operation,
                buffer.as_mut_ptr(),
                buffer.len(),
                &mut count,
            )
        })?;
        Ok(count)
    }
}

impl Drop for Bridge {
    fn drop(&mut self) {
        unsafe { (self.table.ReleaseOperation)(self.operation) };
    }
}

#[test]
fn initializes_native_output_storage_before_exposing_it_to_safe_rust() {
    let bridge = Bridge::start(Mode::InspectReadBuffer);
    bridge.send().unwrap();
    let mut buffer = [0xa5; 8];
    for expected in b"abc" {
        assert_eq!(bridge.read(&mut buffer).unwrap(), 1);
        assert_eq!(buffer[0], *expected);
        buffer.fill(0xa5);
    }
    assert_eq!(bridge.read(&mut buffer).unwrap(), 0);
}

#[test]
fn owns_request_and_response_storage_with_duplicate_byte_headers_and_partial_reads() {
    let bridge = Bridge::start(Mode::Normal);
    let request = bridge.shared.request.lock().unwrap().clone().unwrap();
    assert_eq!(request.method, "POST");
    assert_eq!(request.url, "https://example.invalid/catalog?token=secret");
    assert_eq!(request.body, [0, 1, 255]);
    assert_eq!(request.timeout, Some(Duration::from_millis(1234)));
    assert_eq!(request.headers.len(), 2);
    assert_eq!(request.headers[0].value, [b'a', 0xfe]);
    assert_eq!(request.headers[0], request.headers[1]);
    let response = bridge.send().unwrap();
    assert_eq!(response.status_code, 200);
    let mut buffer = [0; 8];
    for expected in b"abc" {
        assert_eq!(bridge.read(&mut buffer).unwrap(), 1);
        assert_eq!(buffer[0], *expected);
    }
    assert_eq!(bridge.read(&mut buffer).unwrap(), 0);
    // EOF remains permanent even if a buggy application resets its cursor.
    *bridge.shared.position.lock().unwrap() = 0;
    assert_eq!(bridge.read(&mut buffer).unwrap(), 0);
    // The FFI array and both CString allocations must survive Send and Read.
    unsafe {
        let headers = std::slice::from_raw_parts(response.headers, response.header_count);
        assert_eq!(headers.len(), 2);
        assert_eq!(CStr::from_ptr(headers[0].name).to_bytes(), b"Set-Cookie");
        assert_eq!(
            CStr::from_ptr(headers[0].value).to_bytes(),
            &[b'a', b'=', 0xff]
        );
        assert_eq!(CStr::from_ptr(headers[1].value).to_bytes(), b"b=2");
    }
}

#[test]
fn retain_release_and_operations_keep_context_and_api_alive_without_cycles() {
    let bridge = Bridge::start(Mode::Normal);
    let context = Arc::downgrade(&bridge.context);
    let api = Arc::downgrade(&bridge.context.api);
    let shared = Arc::clone(&bridge.shared);
    let data = bridge.table.user_data;
    let release_context = bridge.table.Release;
    unsafe { (bridge.table.Retain)(data) };
    // Rust owner, callback table, operation, and native snapshot.
    assert_eq!(Arc::strong_count(&bridge.context), 4);
    drop(bridge);
    assert_eq!(shared.operation_drops.load(Ordering::SeqCst), 1);
    assert!(api.upgrade().is_some());
    assert!(context.upgrade().is_some());
    assert_eq!(shared.transport_drops.load(Ordering::SeqCst), 0);
    unsafe { release_context(data) };
    assert!(context.upgrade().is_none());
    assert!(api.upgrade().is_none());
    assert_eq!(shared.transport_drops.load(Ordering::SeqCst), 1);
}

#[test]
fn cancel_before_send_is_sticky_and_idempotent() {
    let bridge = Bridge::start(Mode::Normal);
    unsafe {
        (bridge.table.Cancel)(bridge.operation);
        (bridge.table.Cancel)(bridge.operation);
    }
    assert_eq!(
        bridge.send().unwrap_err().native_code(),
        Some(NativeErrorCode::OperationCancelled)
    );
    assert_eq!(
        bridge.read(&mut [0; 4]).unwrap_err().native_code(),
        Some(NativeErrorCode::OperationCancelled)
    );
    assert_eq!(bridge.shared.cancels.load(Ordering::SeqCst), 1);
}

#[test]
fn cancel_interrupts_send_and_read_without_taking_the_io_lock() {
    for mode in [Mode::BlockSend, Mode::BlockRead] {
        let bridge = Bridge::start(mode);
        if matches!(mode, Mode::BlockRead) {
            bridge.send().unwrap();
        }
        let (entered, wait) = mpsc::channel();
        *bridge.shared.entered.lock().unwrap() = Some(entered);
        // Native owns the operation until both callbacks return.
        let pointer = bridge.operation as usize;
        let api = Arc::clone(&bridge.context.api);
        let worker = std::thread::spawn(move || {
            let operation = pointer as *mut c_void;
            let status = unsafe {
                if matches!(mode, Mode::BlockSend) {
                    let mut response = flHttpResponseData {
                        version: FOUNDRY_LOCAL_HTTP_TRANSPORT_VERSION,
                        status_code: 0,
                        headers: ptr::null(),
                        header_count: 0,
                    };
                    send(operation, &mut response)
                } else {
                    let mut count = 0;
                    read(operation, [0; 4].as_mut_ptr(), 4, &mut count)
                }
            };
            api.check(status).unwrap_err().native_code()
        });
        wait.recv_timeout(Duration::from_secs(10)).unwrap();
        unsafe { (bridge.table.Cancel)(bridge.operation) };
        assert_eq!(
            worker.join().unwrap(),
            Some(NativeErrorCode::OperationCancelled)
        );
        assert_eq!(
            bridge.read(&mut [0; 4]).unwrap_err().native_code(),
            Some(NativeErrorCode::OperationCancelled)
        );
    }
}

#[test]
fn rejects_invalid_status_headers_and_read_lengths() {
    for mode in [Mode::BadStatus, Mode::BadHeader] {
        let bridge = Bridge::start(mode);
        assert_eq!(
            bridge.send().unwrap_err().native_code(),
            Some(NativeErrorCode::InvalidArgument)
        );
    }
    let bridge = Bridge::start(Mode::BadRead);
    bridge.send().unwrap();
    let mut count = 99;
    let status =
        unsafe { (bridge.table.Read)(bridge.operation, [0; 4].as_mut_ptr(), 4, &mut count) };
    assert_eq!(
        bridge.check(status).unwrap_err().native_code(),
        Some(NativeErrorCode::InvalidArgument)
    );
    assert_eq!(count, 0);
}

#[test]
fn rejects_invalid_header_encodings_and_counts_without_lossy_defaults() {
    for (name, value) in [
        ("bad name", b"x".as_slice()),
        ("Valid", b"\0"),
        ("Valid", b"x\n"),
        ("Valid", b"x\r"),
        ("Valid", b"\x7f"),
    ] {
        assert!(OwnedResponse::new(HttpResponseHead {
            status_code: 200,
            headers: vec![HttpHeader {
                name: name.into(),
                value: value.into()
            }],
        })
        .is_err());
    }
    unsafe {
        assert!(bytes::<flHttpHeader>(ptr::null(), usize::MAX).is_err());
        assert!(bytes::<u8>(ptr::null(), 1).is_err());
        assert!(bytes::<u8>(ptr::null(), 0).unwrap().is_empty());
        assert!(string(b"\xff\0".as_ptr().cast()).is_err());
    }
}

#[test]
fn panics_and_application_errors_return_owned_secret_safe_statuses() {
    let mut bridge = Bridge::new(Mode::PanicCreate);
    let error = bridge.create().unwrap_err();
    assert_eq!(error.native_code(), Some(NativeErrorCode::Internal));
    assert!(bridge.operation.is_null());
    assert!(!error.to_string().contains("secret"));
    for mode in [Mode::PanicSend, Mode::ErrorSend, Mode::PanicRead] {
        let bridge = Bridge::start(mode);
        let error = if matches!(mode, Mode::PanicRead) {
            bridge.send().unwrap();
            bridge.read(&mut [0; 4]).unwrap_err()
        } else {
            bridge.send().unwrap_err()
        };
        assert_eq!(
            error.native_code(),
            Some(if matches!(mode, Mode::ErrorSend) {
                NativeErrorCode::Network
            } else {
                NativeErrorCode::Internal
            })
        );
        assert_eq!(error.native_message(), Some("model HTTP callback failed"));
        unsafe { (bridge.table.Cancel)(bridge.operation) };
        assert_eq!(
            bridge.read(&mut [0; 4]).unwrap_err().native_code(),
            Some(NativeErrorCode::OperationCancelled),
        );
    }
}

#[test]
fn cancel_panic_aborts_instead_of_reporting_success() {
    const CHILD: &str = "FOUNDRY_RUST_CANCEL_PANIC_CHILD";
    if std::env::var_os(CHILD).is_some() {
        let bridge = Bridge::start(Mode::PanicCancel);
        unsafe { (bridge.table.Cancel)(bridge.operation) };
        panic!("cancel unexpectedly returned");
    }
    // Disable core files for the deliberate abort.
    #[cfg(unix)]
    let mut command = {
        let mut command = std::process::Command::new("sh");
        command.args(["-c", "ulimit -c 0; exec \"$@\"", "sh"]);
        command.arg(std::env::current_exe().unwrap());
        command
    };
    #[cfg(not(unix))]
    let mut command = std::process::Command::new(std::env::current_exe().unwrap());
    let output = command
        .args([
            "--exact",
            "detail::model_http_transport::tests::cancel_panic_aborts_instead_of_reporting_success",
            "--nocapture",
        ])
        .env(CHILD, "1")
        .output()
        .unwrap();
    assert!(!output.status.success());
    #[cfg(unix)]
    {
        use std::os::unix::process::ExitStatusExt;
        assert_eq!(output.status.signal(), Some(6));
    }
    assert!(String::from_utf8_lossy(&output.stderr).contains("cancel failed"));
}
