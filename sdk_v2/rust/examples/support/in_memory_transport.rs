use foundry_local_sdk::{
    FoundryLocalError, HttpHeader, HttpRequest, HttpResponseHead, ModelHttpOperation,
    ModelHttpTransport, NativeErrorCode,
};
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

pub const CATALOG_URL: &str = "https://example.invalid/catalog";
const BODY: &[u8] = br#"{"value":[]}"#;

#[derive(Default)]
pub struct Counters {
    pub requests: AtomicUsize,
    pub reads: AtomicUsize,
    pub operations_released: AtomicUsize,
    pub transports_released: AtomicUsize,
}

pub struct InMemoryTransport(pub Arc<Counters>);

impl Drop for InMemoryTransport {
    fn drop(&mut self) {
        self.0.transports_released.fetch_add(1, Ordering::SeqCst);
    }
}

impl ModelHttpTransport for InMemoryTransport {
    fn create_operation(
        &self,
        request: HttpRequest,
    ) -> Result<Box<dyn ModelHttpOperation>, FoundryLocalError> {
        if request.method != "POST" || request.url != CATALOG_URL {
            return Err(FoundryLocalError::Validation {
                reason: "unsupported in-memory catalog request".into(),
            });
        }
        Ok(Box::new(InMemoryOperation {
            counters: Arc::clone(&self.0),
            cancelled: AtomicBool::new(false),
            timeout: request.timeout,
            state: Mutex::new((0, None)),
        }))
    }
}

struct InMemoryOperation {
    counters: Arc<Counters>,
    cancelled: AtomicBool,
    timeout: Option<Duration>,
    state: Mutex<(usize, Option<Instant>)>,
}

impl InMemoryOperation {
    fn check(&self, started: Option<Instant>) -> Result<(), FoundryLocalError> {
        if self.cancelled.load(Ordering::Acquire) {
            return Err(FoundryLocalError::Native {
                code: NativeErrorCode::OperationCancelled,
                message: "in-memory operation cancelled".into(),
            });
        }
        if let (Some(budget), Some(started)) = (self.timeout, started) {
            if started.elapsed() >= budget {
                return Err(FoundryLocalError::Native {
                    code: NativeErrorCode::Network,
                    message: "in-memory operation deadline expired".into(),
                });
            }
        }
        Ok(())
    }
}

impl ModelHttpOperation for InMemoryOperation {
    fn send(&self) -> Result<HttpResponseHead, FoundryLocalError> {
        let mut state = self.state.lock().unwrap();
        state.1 = Some(Instant::now());
        self.check(state.1)?;
        self.counters.requests.fetch_add(1, Ordering::SeqCst);
        Ok(HttpResponseHead {
            status_code: 200,
            headers: vec![
                HttpHeader {
                    name: "content-type".into(),
                    value: b"application/json".to_vec(),
                },
                HttpHeader {
                    name: "x-example".into(),
                    value: b"first".to_vec(),
                },
                HttpHeader {
                    name: "x-example".into(),
                    value: b"second".to_vec(),
                },
            ],
        })
    }

    fn read(&self, buffer: &mut [u8]) -> Result<usize, FoundryLocalError> {
        let mut state = self.state.lock().unwrap();
        self.check(state.1)?;
        // Short reads demonstrate that the native coordinator consumes a stream.
        let count = buffer.len().min(3).min(BODY.len() - state.0);
        buffer[..count].copy_from_slice(&BODY[state.0..state.0 + count]);
        state.0 += count;
        self.counters.reads.fetch_add(1, Ordering::SeqCst);
        Ok(count)
    }

    fn cancel(&self) {
        // There is no pending I/O. A network adapter must also interrupt its socket or future.
        self.cancelled.store(true, Ordering::Release);
    }
}

impl Drop for InMemoryOperation {
    fn drop(&mut self) {
        self.counters
            .operations_released
            .fetch_add(1, Ordering::SeqCst);
    }
}
