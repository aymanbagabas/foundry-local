//! Real HTTP through Reqwest, registered with the native model catalog.

use foundry_local_sdk::{FoundryLocalConfig, FoundryLocalManager, ReqwestTransport};
use std::io::{Read, Write};
use std::net::TcpListener;
use std::sync::Arc;
use std::time::{Duration, Instant};

#[tokio::main(flavor = "current_thread")]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let listener = TcpListener::bind("127.0.0.1:0")?;
    let address = listener.local_addr()?;
    listener.set_nonblocking(true)?;
    let server = std::thread::spawn(move || -> std::io::Result<String> {
        let started = Instant::now();
        let mut stream = loop {
            match listener.accept() {
                Ok((stream, _)) => break stream,
                Err(error)
                    if error.kind() == std::io::ErrorKind::WouldBlock
                        && started.elapsed() < Duration::from_secs(10) =>
                {
                    std::thread::sleep(Duration::from_millis(5));
                }
                Err(error) => return Err(error),
            }
        };
        stream.set_read_timeout(Some(Duration::from_secs(5)))?;
        stream.set_write_timeout(Some(Duration::from_secs(5)))?;
        let mut request = Vec::new();
        while !request.ends_with(b"\r\n\r\n") {
            let mut byte = [0];
            stream.read_exact(&mut byte)?;
            request.push(byte[0]);
        }
        let head = String::from_utf8_lossy(&request);
        let size = head
            .lines()
            .find_map(|line| {
                let (name, value) = line.split_once(':')?;
                name.eq_ignore_ascii_case("content-length")
                    .then(|| value.trim().parse::<usize>().unwrap())
            })
            .unwrap_or(0);
        let mut body = vec![0; size];
        stream.read_exact(&mut body)?;
        let line = head.lines().next().unwrap_or_default().to_owned();
        stream.write_all(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n")?;
        for chunk in br#"{"value":[]}"#.chunks(3) {
            write!(stream, "{:x}\r\n", chunk.len())?;
            stream.write_all(chunk)?;
            stream.write_all(b"\r\n")?;
            stream.flush()?;
        }
        stream.write_all(b"0\r\n\r\n")?;
        Ok(line)
    });

    let adapter = Arc::new(ReqwestTransport::with_client_builder(
        reqwest::Client::builder()
            .no_proxy()
            .connect_timeout(Duration::from_secs(2)),
    )?);
    let data = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("target/reqwest-model-http-transport")
        .join(std::process::id().to_string());
    let manager = FoundryLocalManager::create(
        FoundryLocalConfig::new("rust-reqwest-model-http-transport")
            .app_data_dir(data.to_string_lossy())
            .catalog_url(format!("http://{address}/catalog?sig=a%2Bb%2f+"))
            .additional_setting("DisableNonessentialTelemetry", "true")
            .model_http_transport(adapter),
    )?;
    let models = manager.catalog().get_models().await?;
    assert!(models.is_empty());
    drop(manager);
    let request = server.join().map_err(|_| "loopback server failed")??;
    assert_eq!(request, "POST /catalog?sig=a%2Bb%2f+ HTTP/1.1");
    println!("Reqwest native catalog: {request}; chunked response; 0 models; clean shutdown");
    Ok(())
}
