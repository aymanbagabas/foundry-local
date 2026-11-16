#[path = "../examples/support/in_memory_transport.rs"]
mod support;

use foundry_local_sdk::{FoundryLocalConfig, FoundryLocalManager};
use std::sync::atomic::Ordering;
use std::sync::Arc;
use support::{Counters, InMemoryTransport, CATALOG_URL};

// This separate test process owns the process-wide native manager.
#[tokio::test]
async fn native_catalog_executes_application_transport_and_releases_its_snapshot() {
    let root = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("target/native-model-http-transport")
        .join(std::process::id().to_string());
    let counters = Arc::new(Counters::default());
    let transport = Arc::new(InMemoryTransport(Arc::clone(&counters)));
    let weak = Arc::downgrade(&transport);
    let config = FoundryLocalConfig::new("rust-transport-integration")
        .app_data_dir(root.to_string_lossy())
        .catalog_url(CATALOG_URL)
        .additional_setting("DisableNonessentialTelemetry", "true")
        .model_http_transport(transport.clone());
    assert!(format!("{config:?}").contains("model_http_transport: Some(\"..\")"));
    let manager =
        FoundryLocalManager::create(config).expect("register transport and create manager");
    drop(transport);
    assert!(
        weak.upgrade().is_some(),
        "manager must retain the configuration snapshot"
    );
    assert!(manager
        .catalog()
        .get_models()
        .await
        .expect("read empty catalog")
        .is_empty());
    let requests = counters.requests.load(Ordering::SeqCst);
    assert!(
        requests > 0,
        "native catalog must execute the Rust callback"
    );
    assert!(
        counters.reads.load(Ordering::SeqCst) > requests,
        "native must consume short reads"
    );
    assert_eq!(
        counters.operations_released.load(Ordering::SeqCst),
        requests
    );
    drop(manager);
    assert!(
        weak.upgrade().is_none(),
        "transport must not form a reference cycle"
    );
    assert_eq!(counters.transports_released.load(Ordering::SeqCst), 1);
}
