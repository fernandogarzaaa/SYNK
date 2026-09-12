# Local SLM weights (proven: Qwen2.5-1.5B Q4_K_M via llama.cpp Vulkan)

The harness talks to any OpenAI-compatible local server. No Python
dependencies beyond stdlib (`harness/local/model.py: EndpointLocalModel`).

## Quick start (Windows, RTX 2060, ~1.2 GB disk)

1. Server binary (30 MB, Vulkan GPU build):
   `llama-b10919-bin-win-vulkan-x64.zip` from
   https://github.com/ggerganov/llama.cpp/releases (b10919+) →
   extract to `E:\workspace\models\llama`.
2. Weights (1.1 GB):
   `Qwen/Qwen2.5-1.5B-Instruct-GGUF`, file
   `qwen2.5-1.5b-instruct-q4_k_m.gguf` → `E:\workspace\models\`.
3. Run (note: port 8080 may be taken — example uses 8090):
   `llama-server.exe -m E:\workspace\models\qwen2.5-1.5b-q4km.gguf --port 8090 -c 2048 --jinja`
4. Point the harness at it and restart:
   `SYNK_LOCAL_MODEL=endpoint`
   `SYNK_LOCAL_MODEL_URL=http://127.0.0.1:8090/v1/chat/completions`
   `SYNK_LOCAL_MODEL_NAME=qwen2.5-1.5b`

## Behavior

- First inference is slow (~14 s cold on this box); steady-state is faster.
- The model must emit `{"decision","ref","text","confidence"}` JSON
  (enforced by system prompt). Malformed output or a down server
  escalates to cloud/mock — never crashes the loop
  (`harness/local/runtime.py`, covered by `tests/test_local_model.py`).
- `_normalize_local_decision` (`harness/server.py`) rejects anything
  outside the fixed tool allowlist, so a creative SLM can never invent tools.

## Measured (2026-09-12, Qwen2.5-1.5B Q4_K_M, Vulkan/RTX 2060)

- Valid JSON decision, confidence 0.9, routed `local_slm`, normalized cleanly.
- Cold latency ~14 s for a ~40-token decision. Warmed-up runs are faster;
  quantize smaller (Q4_0) or shorten `max_tokens` if this gates interaction.
