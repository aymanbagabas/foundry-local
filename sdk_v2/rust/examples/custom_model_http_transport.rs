//! An application-owned catalog, with no network or model weights.

#[path = "support/in_memory_transport.rs"]
mod support;

use foundry_local_sdk::{FoundryLocalConfig, FoundryLocalManager};
use std::sync::atomic::Ordering;
use std::sync::Arc;
use support::{Counters, InMemoryTransport, CATALOG_URL};

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let data = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("target/custom-model-http-transport")
        .join(std::process::id().to_string());
    let counters = Arc::new(Counters::default());
    let transport = Arc::new(InMemoryTransport(Arc::clone(&counters)));
    let config = FoundryLocalConfig::new("rust-custom-model-http-transport")
        .app_data_dir(data.to_string_lossy())
        .catalog_url(CATALOG_URL)
        .additional_setting("DisableNonessentialTelemetry", "true")
        .model_http_transport(transport.clone());
    let manager = FoundryLocalManager::create(config)?;
    drop(transport);
    let models = manager.catalog().get_models().await?;
    assert!(models.is_empty());
    drop(manager);
    assert!(counters.requests.load(Ordering::SeqCst) > 0);
    assert!(counters.reads.load(Ordering::SeqCst) > counters.requests.load(Ordering::SeqCst));
    assert_eq!(
        counters.operations_released.load(Ordering::SeqCst),
        counters.requests.load(Ordering::SeqCst),
    );
    assert_eq!(counters.transports_released.load(Ordering::SeqCst), 1);
    println!(
        "Native transport: {} catalog requests, {} partial reads, 0 models, balanced ownership",
        counters.requests.load(Ordering::SeqCst),
        counters.reads.load(Ordering::SeqCst),
    );
    Ok(())
}
