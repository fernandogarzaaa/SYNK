use serde::{Deserialize, Serialize};
use tauri::command;
use reqwest::Client;
use std::sync::Arc;
use tokio::sync::Mutex;

#[derive(Debug, Clone)]
pub struct HarnessState {
    pub base_url: String,
    pub client: Arc<Client>,
}

#[derive(Serialize, Deserialize, Debug)]
pub struct WorldSnapshot {
    pub world: serde_json::Value,
    pub ownership: serde_json::Value,
    pub events: serde_json::Value,
}

#[derive(Serialize, Deserialize, Debug)]
pub struct VerificationResult {
    pub result: String,
    pub reason: String,
    pub confidence: f32,
}

#[command]
async fn get_world_state(state: tauri::State<'_, Arc<Mutex<HarnessState>>>) -> Result<WorldSnapshot, String> {
    let s = state.lock().await;
    let url = format!("{}/world", s.base_url);
    
    let res = s.client.get(&url).send().await
        .map_err(|e| e.to_string())?
        .json::<serde_json::Value>().await
        .map_err(|e| e.to_string())?;

    let world = res["world"].clone();
    let ownership = res["ownership"].clone();
    let events = res["events"].clone();

    Ok(WorldSnapshot { world, ownership, events })
}

#[command]
async fn submit_action(
    state: tauri::State<'_, Arc<Mutex<HarnessState>>>,
    body: serde_json::Value,
) -> Result<serde_json::Value, String> {
    let s = state.lock().await;
    let url = format!("{}/act", s.base_url);

    let res = s.client.post(&url).json(&body).send().await
        .map_err(|e| e.to_string())?
        .json::<serde_json::Value>().await
        .map_err(|e| e.to_string())?;

    Ok(res)
}

#[command]
async fn verify_claim(
    state: tauri::State<'_, Arc<Mutex<HarnessState>>>,
    claim_id: String,
) -> Result<VerificationResult, String> {
    let s = state.lock().await;
    let url = format!("{}/verification/verify", s.base_url);

    let payload = serde_json::json!({ "claim_id": claim_id });
    let res = s.client.post(&url).json(&payload).send().await
        .map_err(|e| e.to_string())?
        .json::<VerificationResult>().await
        .map_err(|e| e.to_string())?;

    Ok(res)
}

#[command]
async fn get_recent_events(state: tauri::State<'_, Arc<Mutex<HarnessState>>>) -> Result<serde_json::Value, String> {
    let s = state.lock().await;
    let url = format!("{}/world", s.base_url);
    
    let res = s.client.get(&url).send().await
        .map_err(|e| e.to_string())?
        .json::<serde_json::Value>().await
        .map_err(|e| e.to_string())?;

    Ok(res["events"].clone())
}

#[command]
async fn request_consent(
    _state: tauri::State<'_, Arc<Mutex<HarnessState>>>,
    action: serde_json::Value
) -> Result<bool, String> {
    // Consent is collected in the frontend (confirm dialog) which then
    // resubmits with user_consented=true. This endpoint exists for
    // harness-initiated signals; default-deny is the safe posture.
    println!("CONSENT REQUEST (default deny) for action: {:?}", action);
    Ok(false)
}

fn main() {
    let harness_state = Arc::new(Mutex::new(HarnessState {
        base_url: "http://127.0.0.1:18080".to_string(),
        client: Arc::new(Client::new()),
    }));

    tauri::Builder::default()
        .manage(harness_state)
        .invoke_handler(tauri::generate_handler![
            get_world_state,
            submit_action,
            verify_claim,
            get_recent_events,
            request_consent
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}

