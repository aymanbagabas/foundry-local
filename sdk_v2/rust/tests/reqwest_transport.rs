#![cfg(feature = "model-http-reqwest")]

use foundry_local_sdk::{
    HttpHeader, HttpRequest, ModelHttpOperation, ModelHttpTransport, NativeErrorCode,
    ReqwestTransport,
};
use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{mpsc, Arc};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

struct Server {
    url: String,
    requests: mpsc::Receiver<Vec<u8>>,
    stop: Arc<AtomicBool>,
    thread: Option<JoinHandle<()>>,
}

fn request_bytes(stream: &mut TcpStream) -> Vec<u8> {
    let mut request = Vec::new();
    while !request.ends_with(b"\r\n\r\n") {
        let mut byte = [0];
        stream.read_exact(&mut byte).expect("read request head");
        request.push(byte[0]);
    }
    let count = String::from_utf8_lossy(&request)
        .lines()
        .find_map(|line| {
            let (name, value) = line.split_once(':')?;
            name.eq_ignore_ascii_case("content-length")
                .then(|| value.trim().parse::<usize>().unwrap())
        })
        .unwrap_or(0);
    let start = request.len();
    request.resize(start + count, 0);
    stream
        .read_exact(&mut request[start..])
        .expect("read request body");
    request
}

impl Server {
    fn new(handler: impl Fn(&mut TcpStream) + Send + 'static) -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let url = format!("http://{}/catalog", listener.local_addr().unwrap());
        listener.set_nonblocking(true).unwrap();
        let stop = Arc::new(AtomicBool::new(false));
        let finished = Arc::clone(&stop);
        let (send, requests) = mpsc::channel();
        let thread = thread::spawn(move || {
            while !finished.load(Ordering::Acquire) {
                match listener.accept() {
                    Ok((mut stream, _)) => {
                        stream
                            .set_read_timeout(Some(Duration::from_secs(3)))
                            .unwrap();
                        stream
                            .set_write_timeout(Some(Duration::from_secs(3)))
                            .unwrap();
                        let request = request_bytes(&mut stream);
                        if send.send(request).is_err() {
                            break;
                        }
                        handler(&mut stream);
                    }
                    Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                        thread::sleep(Duration::from_millis(2));
                    }
                    Err(error) => panic!("accept loopback connection: {error}"),
                }
            }
        });
        Self {
            url,
            requests,
            stop,
            thread: Some(thread),
        }
    }

    fn received(&self) -> Vec<u8> {
        self.requests
            .recv_timeout(Duration::from_secs(3))
            .expect("receive HTTP request")
    }
}

impl Drop for Server {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Release);
        let result = self.thread.take().unwrap().join();
        if !thread::panicking() {
            result.unwrap();
        }
    }
}

fn transport() -> ReqwestTransport {
    ReqwestTransport::with_client_builder(reqwest::Client::builder().no_proxy()).unwrap()
}

fn request(url: &str) -> HttpRequest {
    HttpRequest {
        method: "POST".into(),
        url: url.into(),
        headers: Vec::new(),
        body: Vec::new(),
        timeout: Some(Duration::from_secs(2)),
    }
}

fn read_all(operation: &dyn ModelHttpOperation) -> Vec<u8> {
    let mut body = Vec::new();
    let mut buffer = [0; 2];
    loop {
        let count = operation.read(&mut buffer).unwrap();
        if count == 0 {
            return body;
        }
        body.extend_from_slice(&buffer[..count]);
    }
}

#[test]
fn streams_raw_bytes_and_preserves_signed_targets_and_duplicate_headers() {
    let encoded = b"\x1f\x8bopaque encoded bytes\x00\xff";
    for encoding in ["gzip", "br", "deflate", "zstd"] {
        let server = Server::new(move |stream| {
            write!(stream, "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nContent-Encoding: {encoding}\r\nX-Duplicate: first\r\nX-Duplicate: second\r\nX-Bytes: ", encoded.len()).unwrap();
            stream
                .write_all(b"\xff\r\nConnection: close\r\n\r\n")
                .unwrap();
            stream.write_all(encoded).unwrap();
        });
        let mut input = request(&format!("{}?sig=a%2Bb%2f+&x=1&x=2", server.url));
        input.body = vec![0, 1, 255];
        input.headers = vec![
            HttpHeader {
                name: "x-duplicate".into(),
                value: b"one".to_vec(),
            },
            HttpHeader {
                name: "x-duplicate".into(),
                value: b"two".to_vec(),
            },
        ];
        let adapter = ReqwestTransport::with_client_builder(
            reqwest::Client::builder()
                .no_proxy()
                .user_agent("application-policy"),
        )
        .unwrap();
        let operation = adapter.create_operation(input.clone()).unwrap();
        let head = operation.send().unwrap();
        assert_eq!(head.status_code, 200);
        assert_eq!(
            head.headers
                .iter()
                .find(|h| h.name == "content-encoding")
                .unwrap()
                .value,
            encoding.as_bytes(),
        );
        assert_eq!(
            head.headers
                .iter()
                .filter(|h| h.name == "x-duplicate")
                .map(|h| h.value.as_slice())
                .collect::<Vec<_>>(),
            [b"first".as_slice(), b"second"],
        );
        assert_eq!(
            head.headers
                .iter()
                .find(|h| h.name == "x-bytes")
                .unwrap()
                .value,
            [255]
        );
        assert_eq!(read_all(operation.as_ref()), encoded);
        let received = server.received();
        assert!(received.starts_with(b"POST /catalog?sig=a%2Bb%2f+&x=1&x=2 HTTP/1.1\r\n"));
        assert!(received.ends_with(&input.body));
        let text = String::from_utf8_lossy(&received);
        assert!(text.contains("x-duplicate: one\r\nx-duplicate: two\r\n"));
        assert!(text.contains("user-agent: application-policy\r\n"));
    }
}

#[test]
fn factory_overrides_redirect_and_retry_policies_but_keeps_http_error_responses() {
    for status in [302, 503, 404] {
        let server = Server::new(move |stream| {
            write!(stream, "HTTP/1.1 {status} Test\r\nLocation: /redirected\r\nContent-Length: 3\r\nConnection: close\r\n\r\nraw").unwrap();
        });
        let adapter = ReqwestTransport::with_client_builder(
            reqwest::Client::builder()
                .no_proxy()
                .redirect(reqwest::redirect::Policy::limited(5))
                .retry(
                    reqwest::retry::for_host("127.0.0.1")
                        .no_budget()
                        .max_retries_per_request(2)
                        .classify_fn(|response| response.retryable()),
                ),
        )
        .unwrap();
        let operation = adapter.create_operation(request(&server.url)).unwrap();
        assert_eq!(operation.send().unwrap().status_code, status);
        assert_eq!(read_all(operation.as_ref()), b"raw");
        server.received();
        assert!(
            server.requests.try_recv().is_err(),
            "adapter must perform exactly one request"
        );
    }
}

#[test]
fn creation_is_local_and_cancel_before_send_never_connects() {
    let server = Server::new(|_| panic!("cancelled operation must not connect"));
    let operation = transport().create_operation(request(&server.url)).unwrap();
    assert!(server
        .requests
        .recv_timeout(Duration::from_millis(30))
        .is_err());
    operation.cancel();
    operation.cancel();
    assert_eq!(
        operation.send().unwrap_err().native_code(),
        Some(NativeErrorCode::OperationCancelled)
    );
    assert_eq!(
        operation.read(&mut [0; 1]).unwrap_err().native_code(),
        Some(NativeErrorCode::OperationCancelled)
    );
    assert!(server.requests.try_recv().is_err());
}

#[test]
fn cancellation_interrupts_blocked_send_and_read_and_closes_the_socket() {
    for during_read in [false, true] {
        let (closed, observe_close) = mpsc::channel();
        let server = Server::new(move |stream| {
            if during_read {
                stream
                    .write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 10\r\n\r\n")
                    .unwrap();
                stream.flush().unwrap();
            }
            let result = stream.read(&mut [0; 1]);
            closed.send(matches!(result, Ok(0))).unwrap();
        });
        let operation: Arc<dyn ModelHttpOperation> =
            Arc::from(transport().create_operation(request(&server.url)).unwrap());
        if during_read {
            operation.send().unwrap();
        }
        let worker_operation = Arc::clone(&operation);
        let (done, completion) = mpsc::channel();
        let worker = thread::spawn(move || {
            let result = if during_read {
                worker_operation.read(&mut [0; 1]).map(|_| ())
            } else {
                worker_operation.send().map(|_| ())
            };
            done.send(result).unwrap();
        });
        server.received();
        assert!(
            completion.recv_timeout(Duration::from_millis(50)).is_err(),
            "I/O must still be pending before cancellation"
        );
        operation.cancel();
        let error = completion
            .recv_timeout(Duration::from_secs(1))
            .expect("cancel pending I/O")
            .unwrap_err();
        assert_eq!(
            error.native_code(),
            Some(NativeErrorCode::OperationCancelled)
        );
        worker.join().unwrap();
        assert!(observe_close
            .recv_timeout(Duration::from_secs(1))
            .expect("peer must observe socket close"));
        assert_eq!(
            operation.read(&mut [0; 1]).unwrap_err().native_code(),
            Some(NativeErrorCode::OperationCancelled)
        );
    }
}

#[test]
fn total_deadline_interrupts_pending_send_and_read() {
    for during_read in [false, true] {
        let server = Server::new(move |stream| {
            if during_read {
                stream
                    .write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 10\r\n\r\n")
                    .unwrap();
            }
            // The client must close pending I/O, not wait for this read timeout.
            assert_eq!(stream.read(&mut [0; 1]).unwrap(), 0);
        });
        let mut input = request(&server.url);
        input.timeout = Some(Duration::from_millis(150));
        let operation = transport().create_operation(input).unwrap();
        let start = Instant::now();
        let error = if during_read {
            operation.send().unwrap();
            operation.read(&mut [0; 1]).unwrap_err()
        } else {
            operation.send().unwrap_err()
        };
        assert_eq!(error.native_code(), Some(NativeErrorCode::Network));
        assert!(start.elapsed() < Duration::from_secs(1));
        server.received();
    }
}

#[test]
fn deadline_includes_application_delays_between_reads() {
    let server = Server::new(|stream| {
        stream
            .write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 10\r\n\r\nab")
            .unwrap();
        let _ = stream.read(&mut [0; 1]);
    });
    let mut input = request(&server.url);
    input.timeout = Some(Duration::from_millis(150));
    let operation = transport().create_operation(input).unwrap();
    operation.send().unwrap();
    assert_eq!(operation.read(&mut [0; 1]).unwrap(), 1);
    thread::sleep(Duration::from_millis(180));
    assert_eq!(
        operation.read(&mut [0; 1]).unwrap_err().native_code(),
        Some(NativeErrorCode::Network)
    );
}

#[test]
fn rejects_urls_that_reqwest_would_change_without_exposing_secrets() {
    let adapter = transport();
    for url in [
        "https://EXAMPLE.invalid/catalog?secret=value",
        "https://example.invalid:443/catalog?secret=value",
        "https://example.invalid/a/../catalog?secret=value",
        "https://example.invalid/catalog#secret",
        "https://user:secret@example.invalid/catalog",
    ] {
        let error = match adapter.create_operation(request(url)) {
            Ok(_) => panic!("adapter must reject a changed URL"),
            Err(error) => error,
        };
        assert_eq!(error.native_code(), Some(NativeErrorCode::InvalidArgument));
        assert!(!error.to_string().contains("secret"));
    }
}

#[tokio::test(flavor = "current_thread")]
async fn independent_runtime_survives_transport_drop_and_shuts_down_inside_async_context() {
    let server = Server::new(|stream| {
        stream
            .write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
            .unwrap();
    });
    let adapter = transport();
    let operation = adapter.create_operation(request(&server.url)).unwrap();
    drop(adapter);
    assert_eq!(operation.send().unwrap().status_code, 200);
    assert_eq!(read_all(operation.as_ref()), b"ok");
    drop(operation);
    drop(ReqwestTransport::new().unwrap());
}

#[test]
fn distinct_operations_share_the_runtime_without_blocking_each_other() {
    let stalled = Server::new(|stream| {
        assert_eq!(stream.read(&mut [0; 1]).unwrap(), 0);
    });
    let responsive = Server::new(|stream| {
        stream
            .write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
            .unwrap();
    });
    let adapter = transport();
    let first: Arc<dyn ModelHttpOperation> =
        Arc::from(adapter.create_operation(request(&stalled.url)).unwrap());
    let background = Arc::clone(&first);
    let worker = thread::spawn(move || background.send().unwrap_err().native_code());
    stalled.received();
    let second = adapter.create_operation(request(&responsive.url)).unwrap();
    assert_eq!(second.send().unwrap().status_code, 200);
    assert_eq!(read_all(second.as_ref()), b"ok");
    first.cancel();
    assert_eq!(
        worker.join().unwrap(),
        Some(NativeErrorCode::OperationCancelled)
    );
}

#[test]
fn network_errors_do_not_expose_signed_url_secrets() {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let url = format!(
        "http://{}/catalog?secret=signed-value",
        listener.local_addr().unwrap()
    );
    drop(listener);
    let operation = transport().create_operation(request(&url)).unwrap();
    let error = operation.send().unwrap_err();
    assert_eq!(error.native_code(), Some(NativeErrorCode::Network));
    assert_eq!(
        error.native_message(),
        Some("model HTTP network operation failed")
    );
    assert!(!error.to_string().contains("signed-value"));
}
