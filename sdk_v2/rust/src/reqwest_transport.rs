//! Optional async Reqwest adapter for the synchronous model HTTP interface.

use std::future::Future;
use std::sync::{mpsc, Arc, Mutex, MutexGuard};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

use bytes::Bytes;
use reqwest::header::{HeaderMap, HeaderName, HeaderValue};
use reqwest::{Client, ClientBuilder, Request, Response};
use tokio::runtime::{Builder, Handle};
use tokio::sync::oneshot;
use tokio_util::sync::CancellationToken;

use crate::error::Result;
use crate::{
    FoundryLocalError, HttpHeader, HttpRequest, HttpResponseHead, ModelHttpOperation,
    ModelHttpTransport, NativeErrorCode,
};

fn failure(code: NativeErrorCode, message: &'static str) -> FoundryLocalError {
    FoundryLocalError::Native {
        code,
        message: message.into(),
    }
}

fn cancelled() -> FoundryLocalError {
    failure(
        NativeErrorCode::OperationCancelled,
        "model HTTP operation cancelled",
    )
}

fn network() -> FoundryLocalError {
    failure(
        NativeErrorCode::Network,
        "model HTTP network operation failed",
    )
}

fn deadline_expired() -> FoundryLocalError {
    failure(
        NativeErrorCode::Network,
        "model HTTP total deadline expired",
    )
}

/// Reqwest HTTP for native model catalogs and downloads.
///
/// Available with the `model-http-reqwest` feature. The neutral [`ModelHttpTransport`]
/// interface does not depend on this adapter.
///
/// A dedicated Tokio thread executes async HTTP. Synchronous callbacks wait for that
/// independent runtime, so they also work inside an application's current-thread runtime.
/// The final transport or operation drop shuts down and joins the thread. No application
/// runtime handle is required, and no Tokio runtime is dropped inside an async context.
/// Runtime shutdown can wait for platform DNS lookups already in progress.
///
/// Automatic retries, redirects, and decompression are always disabled, including when
/// other dependencies enable Reqwest compression features. Native code owns retry policy.
/// Cancellation drops pending Send/Read futures. The request deadline spans Send and all Reads.
///
/// Reqwest must preserve the supplied URL exactly. URLs that its parser normalizes, and
/// URLs with fragments or embedded credentials, return an explicit validation error.
/// Already encoded signed paths and queries retain their bytes. Reqwest controls header
/// name case and wire order; duplicate values and response value bytes remain intact.
#[derive(Clone)]
pub struct ReqwestTransport {
    client: Client,
    executor: Arc<Executor>,
}

impl std::fmt::Debug for ReqwestTransport {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("ReqwestTransport")
            .finish_non_exhaustive()
    }
}

impl ReqwestTransport {
    /// Create a transport with Reqwest's default TLS, proxy, and connection settings.
    ///
    /// Automatic retries, redirects, and decompression remain disabled.
    pub fn new() -> Result<Self> {
        Self::with_client_builder(Client::builder())
    }

    /// Build a new client with application network policy.
    ///
    /// Configure proxies, TLS roots, DNS, authentication headers, connection limits, or
    /// shorter timeouts through the builder. This consumes the builder without changing
    /// caller-owned settings. This factory then disables automatic retries, redirects,
    /// and all four supported decompression modes before it builds the client.
    /// It never accepts an opaque client with unverifiable policy.
    /// Application-supplied connector or resolver code remains the application's responsibility.
    pub fn with_client_builder(builder: ClientBuilder) -> Result<Self> {
        let (ready, receive) = mpsc::sync_channel(1);
        let (shutdown, stopped) = oneshot::channel();
        let thread = thread::Builder::new()
            .name("foundry-model-http".into())
            .spawn(move || {
                let runtime = match Builder::new_current_thread().enable_all().build() {
                    Ok(runtime) => runtime,
                    Err(_) => {
                        let _ = ready.send(Err(failure(
                            NativeErrorCode::Internal,
                            "could not create model HTTP runtime",
                        )));
                        return;
                    }
                };
                let client = {
                    let _entered = runtime.enter();
                    builder
                        .retry(reqwest::retry::never())
                        .redirect(reqwest::redirect::Policy::none())
                        .no_gzip()
                        .no_brotli()
                        .no_deflate()
                        .no_zstd()
                        .build()
                };
                let client = match client {
                    Ok(client) => client,
                    Err(_) => {
                        let _ = ready.send(Err(failure(
                            NativeErrorCode::InvalidArgument,
                            "could not build model HTTP client",
                        )));
                        return;
                    }
                };
                if ready.send(Ok((client, runtime.handle().clone()))).is_err() {
                    return;
                }
                runtime.block_on(async {
                    let _ = stopped.await;
                });
            })
            .map_err(|_| {
                failure(
                    NativeErrorCode::Internal,
                    "could not start model HTTP thread",
                )
            })?;
        match receive.recv() {
            Ok(Ok((client, handle))) => Ok(Self {
                client,
                executor: Arc::new(Executor {
                    handle,
                    shutdown: Some(shutdown),
                    thread: Some(thread),
                }),
            }),
            Ok(Err(error)) => {
                let _ = thread.join();
                Err(error)
            }
            Err(_) => {
                let _ = thread.join();
                Err(failure(
                    NativeErrorCode::Internal,
                    "model HTTP runtime startup failed",
                ))
            }
        }
    }
}

struct Executor {
    handle: Handle,
    shutdown: Option<oneshot::Sender<()>>,
    thread: Option<JoinHandle<()>>,
}

impl Executor {
    fn run<T: Send + 'static>(
        &self,
        cancellation: CancellationToken,
        deadline: Option<Instant>,
        future: impl Future<Output = Result<T>> + Send + 'static,
    ) -> Result<T> {
        let (send, receive) = mpsc::sync_channel(1);
        self.handle.spawn(async move {
            let timed = async {
                match deadline {
                    Some(deadline) => {
                        tokio::time::timeout_at(tokio::time::Instant::from_std(deadline), future)
                            .await
                            .map_err(|_| deadline_expired())?
                    }
                    None => future.await,
                }
            };
            let result = tokio::select! {
                biased;
                _ = cancellation.cancelled() => Err(cancelled()),
                result = timed => result,
            };
            let _ = send.send(result);
        });
        receive.recv().map_err(|_| {
            failure(
                NativeErrorCode::Internal,
                "model HTTP worker stopped before completion",
            )
        })?
    }
}

impl Drop for Executor {
    fn drop(&mut self) {
        if let Some(shutdown) = self.shutdown.take() {
            let _ = shutdown.send(());
        }
        if let Some(thread) = self.thread.take() {
            // Futures do not own Executor. Its final owner cannot be the runtime thread.
            let _ = thread.join();
        }
    }
}

impl ModelHttpTransport for ReqwestTransport {
    fn create_operation(&self, request: HttpRequest) -> Result<Box<dyn ModelHttpOperation>> {
        let invalid = || {
            failure(
                NativeErrorCode::InvalidArgument,
                "model HTTP request cannot be represented without changes",
            )
        };
        let url = reqwest::Url::parse(&request.url).map_err(|_| invalid())?;
        if url.as_str() != request.url
            || url.fragment().is_some()
            || !url.username().is_empty()
            || url.password().is_some()
            || !matches!(url.scheme(), "http" | "https")
        {
            return Err(invalid());
        }
        let method =
            reqwest::Method::from_bytes(request.method.as_bytes()).map_err(|_| invalid())?;
        let mut headers = HeaderMap::new();
        for header in request.headers {
            headers.append(
                HeaderName::from_bytes(header.name.as_bytes()).map_err(|_| invalid())?,
                HeaderValue::from_bytes(&header.value).map_err(|_| invalid())?,
            );
        }
        let native = self
            .client
            .request(method, url)
            .headers(headers)
            .body(request.body)
            .build()
            .map_err(|_| invalid())?;
        Ok(Box::new(ReqwestOperation {
            client: self.client.clone(),
            state: Mutex::new(State {
                request: Some(native),
                timeout: request.timeout,
                deadline: None,
                response: None,
                pending: Bytes::new(),
                eof: false,
            }),
            cancellation: CancellationToken::new(),
            executor: Arc::clone(&self.executor),
        }))
    }
}

struct State {
    request: Option<Request>,
    timeout: Option<Duration>,
    deadline: Option<Instant>,
    response: Option<Response>,
    pending: Bytes,
    eof: bool,
}

struct ReqwestOperation {
    client: Client,
    state: Mutex<State>,
    cancellation: CancellationToken,
    executor: Arc<Executor>,
}

impl ReqwestOperation {
    fn state(&self) -> Result<MutexGuard<'_, State>> {
        if self.cancellation.is_cancelled() {
            return Err(cancelled());
        }
        self.state
            .lock()
            .map_err(|_| failure(NativeErrorCode::Internal, "model HTTP state poisoned"))
    }

    fn check(&self, state: &State) -> Result<()> {
        if self.cancellation.is_cancelled() {
            return Err(cancelled());
        }
        if state
            .deadline
            .is_some_and(|deadline| Instant::now() >= deadline)
        {
            return Err(deadline_expired());
        }
        Ok(())
    }
}

impl ModelHttpOperation for ReqwestOperation {
    fn send(&self) -> Result<HttpResponseHead> {
        let mut state = self.state()?;
        self.check(&state)?;
        let request = state.request.take().ok_or_else(|| {
            failure(
                NativeErrorCode::InvalidUsage,
                "model HTTP Send requires a new operation",
            )
        })?;
        state.deadline = state
            .timeout
            .map(|timeout| {
                Instant::now().checked_add(timeout).ok_or_else(|| {
                    failure(
                        NativeErrorCode::InvalidArgument,
                        "model HTTP timeout exceeds the clock range",
                    )
                })
            })
            .transpose()?;
        self.check(&state)?;
        let client = self.client.clone();
        let response =
            self.executor
                .run(self.cancellation.clone(), state.deadline, async move {
                    client.execute(request).await.map_err(|_| network())
                })?;
        self.check(&state)?;
        let head = HttpResponseHead {
            status_code: response.status().as_u16().into(),
            headers: response
                .headers()
                .iter()
                .map(|(name, value)| HttpHeader {
                    name: name.as_str().into(),
                    value: value.as_bytes().to_vec(),
                })
                .collect(),
        };
        state.response = Some(response);
        Ok(head)
    }

    fn read(&self, buffer: &mut [u8]) -> Result<usize> {
        let mut state = self.state()?;
        self.check(&state)?;
        if buffer.is_empty() {
            return Err(failure(
                NativeErrorCode::InvalidArgument,
                "empty model HTTP read buffer",
            ));
        }
        if state.eof {
            return Ok(0);
        }
        if state.pending.is_empty() {
            let mut response = state.response.take().ok_or_else(|| {
                failure(
                    NativeErrorCode::InvalidUsage,
                    "model HTTP Read requires successful Send",
                )
            })?;
            let (response, chunk) =
                self.executor
                    .run(self.cancellation.clone(), state.deadline, async move {
                        loop {
                            let chunk = response.chunk().await.map_err(|_| network())?;
                            if chunk.as_ref().is_some_and(Bytes::is_empty) {
                                continue;
                            }
                            return Ok((response, chunk));
                        }
                    })?;
            self.check(&state)?;
            match chunk {
                Some(chunk) => {
                    state.response = Some(response);
                    state.pending = chunk;
                }
                None => {
                    state.eof = true;
                    return Ok(0);
                }
            }
        }
        let count = buffer.len().min(state.pending.len());
        buffer[..count].copy_from_slice(&state.pending.split_to(count));
        Ok(count)
    }

    fn cancel(&self) {
        self.cancellation.cancel();
    }
}
