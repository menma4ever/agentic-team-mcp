# Comprehensive Architectural Dossier: Evolution, Engineering & Improvements of Agentic Team MCP

**Document Version:** 3.0.0-PROD  
**Author:** Root Watchdog (Chief of Staff & Master Sentinel)  
**Human Owner:** Abdulaziz Komilov (@zwyci / Chat ID: `5644286697`)  
**Scope:** Chronological and Architectural Synthesis of all System Refactors, Bug Fixes, Subsystems, and Enhancements implemented across the Agentic Team Platform and MCP Ecosystem.

---

## 1. Executive Summary & Architectural Overview

The **Agentic Team MCP** ecosystem evolved from a basic single-process command launcher into an enterprise-grade, distributed, autonomous multi-agent operating system. It bridges local developer workflows, cloud GPU training infrastructure, web browser dashboards, and encrypted mobile Telegram communication.

### Core Architectural Pillars
```
                             +-----------------------------------+
                             |       HUMAN OWNER (@zwyci)        |
                             +-----------------+-----------------+
                                               |
                     +-------------------------+-------------------------+
                     | Telegram (@ufljarvisbot)                          | Antigravity Web Studio
                     | [Mobile Telegram Bridge]                          | [http://127.0.0.1:8765]
                     +-------------------------+-------------------------+
                                               |
                               +---------------+---------------+
                               |         ROOT WATCHDOG         |
                               | (Master Sentinel & Director)  |
                               +---------------+---------------+
                                               |
                               +---------------+---------------+
                               |           CEO ASTRA           |
                               | (Strategic Critic & Spawner)  |
                               +-------+---------------+-------+
                                       |               |
             +-------------------------+               +-------------------------+
             |                                                                   |
+------------+------------+                                         +------------+------------+
|     CAMPAIGN_MANAGER    |                                         |     MANAGER_BONSAI      |
| (Project: Job searching)|                                         | (Project: Qwen3.5-2B)   |
+------------+------------+                                         +------------+------------+
             |                                                                   |
     [Worker Fleet]                                                      [Worker Fleet]
 (Global, EMEA, Local)                                               (Prism, DeltaNet, RunPod)
```

1. **Autonomous Multi-Agent Hierarchy**: Strict five-tier command chain (`Workers -> Manager -> CEO -> Watchdog -> Human Owner`) ensuring zero token loops, protected executive quotas, and structured handoffs.
2. **Multi-Account Google Auth Pool**: Isolated profile containers with instant quota failover, Windows Credential Vault sandboxing, and sticky concurrency gating for Gemini 3.8 Flash High and Claude 4.6 Opus.
3. **Multi-Harness Execution Subsystem**: Modular adapters supporting `antigravity` (Google Pool), `codex` (OpenAI CLI), `claude_code` (Anthropic CLI), and high-throughput `direct_api` (DeepSeek, GLM, Groq, Experiential Labs).
4. **Autonomous Telegram Bridge**: Bi-directional mobile interface (`@ufljarvisbot`) featuring strict cryptographic sender whitelisting, live typing simulation, multimodal audio/vision ingestion, and automated document delivery (`[SEND_FILE]`).
5. **Interactive Web Studio**: Node-link topological visualization with real-time WebSocket state streaming, drag-and-drop canvas layout, live message pulsing, and granular token telemetry.
6. **Cloud Compute & Training Orchestration**: Headless RunPod GPU deployment, unquantized teacher-student distillation pipelines, fail-closed zero-burn teardowns, and physical Q2_0 quantization validation.
7. **Production Resiliency**: Starlette path-traversal middleware patches, Windows socket lifecycle management, and graceful recovery from external API quotas.

---

## 2. Chronological Engineering Timeline & Major Epochs

| Epoch | Dates | Primary Engineering Focus | Key Deliverables & Milestones |
|---|---|---|---|
| **Epoch 1** | Sept 15, 2026 | Initial Architecture & CLI Execution | Single worker launcher, DeepSeek CLI harness integration, basic token budgeting. |
| **Epoch 2** | Sept 20, 2026 | Google Multi-Account Auth Pool | Isolated `auth_dir` profiles, Windows Keyring isolation, 429 quota failover, 1-click terminal login. |
| **Epoch 3** | Sept 20–23, 2026 | Web Studio Dashboard & Visual Flow | Node graph canvas, drag-and-drop fix, live SVG communication links, token/pricing meters. |
| **Epoch 4** | Sept 22–23, 2026 | Multi-Harness & Multi-Provider Engine | Codex CLI, Claude Code CLI, Experiential Labs (`xpl`), GPT-6 Sol, Claude 5.5 Opus integration. |
| **Epoch 5** | Sept 23–24, 2026 | Root Watchdog & Telegram Bridge | `@ufljarvisbot` bridge, chat whitelisting, typing simulation, voice STT, vision ingestion, `[DISPATCH]`. |
| **Epoch 6** | Sept 24, 2026 | Strict Escalation Hierarchy & Manifesto | CEO quota protection rules, anti-spam quiescence, external model permission gate, `AGENTS.md`. |
| **Epoch 7** | Sept 24–27, 2026 | SDE Research & Quantization Pipeline | Gated DeltaNet audits, SwiGLU ternary STE, tied-embedding controls, SDE43 formal specification. |
| **Epoch 8** | Sept 27–28, 2026 | Cloud GPU Orchestration & Distillation | RunPod L40 deployment, cap removal, 5,000 steps distillation, zero-burn automated teardown. |
| **Epoch 9** | Sept 28, 2026 | HF Hub Storage Limits & CPU Validation | Step 5,000 quota tripwire, Step 4,500 model preservation, offline CPU eval (81.5% collapse recovery). |
| **Epoch 10** | Sept 28–29, 2026 | Studio Daemon Hardening & Port Audit | Starlette wildcard path crash fix, port 8765 collision analysis with UFL Job Scout. |

---

## 3. Deep Dive: Google Multi-Account Auth Pool (`core/auth_pool.py`)

### The Challenge
Agents running on the Google Account Pool (`antigravity` harness) heavily utilize `gemini-3.8-flash-high` and `claude-opus-4-6-thinking`. However:
1. **Google 429 & Quota Exhaustion**: Running 4–8 concurrent workers quickly triggered rate limits or daily/weekly token saturation.
2. **Keyring Profile Contamination**: On Windows, the Antigravity CLI relies on Windows Credential Manager (`WindowsKeyringHelper`) and `~/.gemini`. Attempting to log into a second account silently overwrote or redirected to the existing logged-in account.
3. **KV-Cache Thrashing**: Blindly switching accounts on every API call destroyed prompt cache reuse, causing token costs and latencies to skyrocket.

### The Architectural Solution
1. **Per-Account Directory Isolation**:
   - Each Google account receives a dedicated directory (`auth/google/account_XX/`) containing its own `.gemini/` configuration, sub-settings, and credential vaults.
   - When spawning an interactive login or CLI worker, `USERPROFILE`, `HOME`, and `ANTIGRAVITY_APP_DATA_DIR` are scoped exclusively to that folder.
2. **Keyring Swap & Vault Capture Engine**:
   - Implemented `prepare_login_environment()`: When initiating authentication for a new slot, the system automatically backs up existing vault credentials into `credential.dat`, wipes the active Windows Keyring entry to prevent silent auto-login, and launches an isolated PowerShell terminal for OAuth completion.
   - Implemented `capture_account_credential()`: Extracts the newly granted OAuth token from the active vault and permanently locks it into `auth/google/account_XX/credential.dat`.
3. **Sticky Affinity & Rate-Limit Concurrency Gates**:
   - Each agent is granted **slot affinity** (`forced_auth_slot_id` / sticky channel allocation) so long conversations continue hitting the same account to maximize KV-cache reuse.
   - Introduced concurrency limiters (`max_concurrent = 4` per account) to guarantee that worker bursts do not exceed provider socket thresholds.
4. **Automated Quota Failover**:
   - Detection of `429 Rate Limit` or quota saturation immediately transitions the slot to `quota-blocked` or `rate-limited` with exponential backoff.
   - The engine automatically routes pending tasks to the next healthiest slot without destroying the agent's context or conversation history.

```python
# Conceptual Architecture in core/auth_pool.py
class GoogleAuthPool:
    def acquire_slot(self, agent_id: str, required_model: str) -> GoogleAccountProfile:
        # 1. Check sticky assignment
        if agent_id in self.sticky_assignments:
            slot = self.accounts[self.sticky_assignments[agent_id]]
            if slot.is_healthy() and slot.active_agents < slot.max_concurrent:
                slot.active_agents += 1
                return slot
        # 2. Seamless Failover to least-loaded healthy slot
        healthy_slots = [s for s in self.accounts.values() if s.is_healthy()]
        selected = min(healthy_slots, key=lambda s: s.active_agents)
        self.sticky_assignments[agent_id] = selected.account_id
        selected.active_agents += 1
        return selected
```

---

## 4. Deep Dive: Web Studio Dashboard (`web/`)

The Web Studio (`http://127.0.0.1:8765`) serves as the operational cockpit for the human owner.

### Key Enhancements Implemented
1. **Topological Canvas & Drag-and-Drop Fix**:
   - Agents are rendered as interactive nodes linked by SVG vectors representing team hierarchy.
   - **Invisible Wall Bugfix**: Resolved a viewport transformation issue where canvas bounding boxes clamped worker cards on the left margin, preventing manual layout organization. Added dynamic viewBox scaling and unconstrained pan/zoom coordinates.
2. **Real-Time Communication Flow**:
   - Connected via `/ws` WebSocket endpoint.
   - When an agent emits `send_team_message` or `report_result`, the UI dynamically animates a traveling pulse along the SVG vector connecting the sender and recipient nodes.
3. **Telemetry & Pricing Metrics (`telemetry.js`)**:
   - Tracks granular execution statistics across all projects: `input_tokens`, `output_tokens`, `cache_read_tokens`, and turnaround latencies.
   - Real-time cost modeling based on model card definitions (supporting free pool quotas alongside paid direct API token burn).
4. **Auth Pool Management UI**:
   - Visual metrics bar: `TOTAL ACCOUNTS`, `HEALTHY`, `QUOTA-BLOCKED`, `ACTIVE AGENTS`.
   - Action controls for each account slot: `Login`, `Capture`, `Test`, `Enable/Disable`, `Delete`.

---

## 5. Deep Dive: Multi-Harness Architecture (`harness/` & `core/providers.py`)

To eliminate vendor lock-in and enable diverse model architectures to collaborate, the engine decouples high-level agent logic from physical model execution.

```
                  +-----------------------------------+
                  |      ORCHESTRATOR / AGENT         |
                  +-----------------+-----------------+
                                    |
          +-------------------------+-------------------------+
          |                                                   |
+---------+----------+                               +--------+---------+
|   CLI_RUNNER       |                               |   DIRECT_API     |
| (Subprocess Pipe)  |                               | (HTTP/SSE Client)|
+----+----+----+-----+                               +----+----+----+---+
     |    |    |                                          |    |    |
   agy  codex claude                                     xpl  glm  deepseek
```

### Supported Harness Engines
1. **`antigravity` (Antigravity CLI Runner)**:
   - Primary driver for Google Account Pool (`gemini-3.8-flash-high`, `claude-opus-4-6-thinking`).
   - Uses headless CLI execution with isolated environment variables.
2. **`codex` (OpenAI Codex CLI Runner)**:
   - Integrates OpenAI's developer CLI for frontier models (`gpt-6-sol`, `gpt-6-luna`, `gpt-4o`).
3. **`claude_code` (Claude Code CLI Runner)**:
   - Executes Anthropic's native terminal agent with full tool harness capabilities.
4. **`direct_api` (High-Throughput HTTP Client)**:
   - Direct SSE streaming client for third-party API providers with custom tool-calling adapters:
     - **Experiential Labs (`xpl`)**: Special integration supporting `gpt-6-sol` and `claude-opus-5.5` using dedicated API keys.
     - **DeepSeek**: DeepSeek Chat / Reasoner endpoints.
     - **Zhipu AI (GLM)**: GLM-4 / GLM-5.3 integration.
     - **Groq & Together**: Low-latency open-weight inference.

### Context Preservation & Session Handoffs
When an agent is reconfigured from one harness or model to another (e.g. migrating `Manager_Bonsai` from `gemini-3.8-flash-high` to `claude-opus-4-6-thinking`):
- The engine serializes the conversation transcript into `manager/.handoffs/<hash>.json`.
- The new model process reads the snapshot upon initialization, preventing cognitive amnesia during architectural transitions.

---

## 6. Deep Dive: Root Watchdog & Autonomous Telegram Bridge (`core/telegram_*`)

Root Watchdog functions as the autonomous Chief of Staff connecting the Human Owner to the platform via Telegram (`@ufljarvisbot`).

### Architectural Components
1. **Cryptographic Whitelisting & Owner Security**:
   - Restricts all control to **Abdulaziz Komilov (@zwyci / Chat ID: `5644286697`)**.
   - Messages from unauthorized Telegram IDs are silently dropped, preventing unauthorized access to local host tools.
2. **Human-like UX Enhancements**:
   - **Typing Indicator Simulation**: During multi-step tool calls or deep reasoning, the bridge runs an asynchronous loop calling Telegram's `send_chat_action(action='typing')` every 4.5 seconds so the user knows work is progressing.
   - **Mobile-First Formatting**: Standardizes responses to fit 35–40 character smartphone screens, utilizing 2–3 column compact tables and bulleted status cards.
   - **No `file:///` URLs**: Strips and converts all local file URLs into standard markdown code blocks (`📄 **filename.md**`), avoiding broken Telegram markdown rendering.
3. **Autonomous Tool Actions**:
   - **`[DISPATCH: <AGENT>] <instruction>`**: Watchdog autonomously wakes up sleeping managers or injects tasks into the engine message queue.
   - **`[SEND_FILE: <path>]`**: Detects when reports, artifacts, or logs are created and immediately uploads them as physical files directly to Telegram.
4. **Multimodal Ingestion Pipeline (`core/multimodal.py`)**:
   - **Voice Notes & Audio**: Telegram voice messages are downloaded, pre-processed, transcribed via local/provider speech-to-text, and injected into the agent prompt context.
   - **Images, Videos & Documents**: Media files are automatically saved to `.user_uploaded/` with unique collision-resistant timestamps and passed as native vision tokens to the model.

---

## 7. Deep Dive: Hierarchy Governance & Anti-Spam Safeguards (`AGENTS.md`)

As the number of autonomous agents grew, unconstrained autonomy led to two critical failure modes:
1. **Executive Token Waste**: High-tier models (CEO Astra) were spending rare quota performing low-level tasks like manual web scraping or writing helper scripts.
2. **Infinite Message Ping-Pong**: When a manager reported task completion, the watchdog acknowledged, which caused the manager to reply with another status report, resulting in an infinite mailbox spam loop.

### Policy Rules Enforced in System Prompts (`core/prompts.py` & `AGENTS.md`)
1. **Strict 5-Tier Escalation Chain**:
   $$\text{Workers} \longrightarrow \text{Manager} \longrightarrow \text{CEO} \longrightarrow \text{Watchdog} \longrightarrow \text{Human Owner}$$
   - Workers cannot invent data when stuck; they must escalate immediately to their Manager.
   - Managers solve operational blockers or escalate architectural questions to the CEO.
   - The CEO escalates unresolvable external needs directly to the Human Owner via Root Watchdog.
2. **CEO Role Invariance**:
   - CEO Astra is strictly reserved for high-level architectural strategy, critique, and worker spawning.
   - The CEO is forbidden from manual low-level coding or scraping.
   - **Direct Spawner Authority**: The CEO can call `spawn_worker` directly for rapid verifications without burdening the Manager.
3. **External Model Permission Gate**:
   - Hard system rule: Agents are strictly prohibited from switching to paid third-party API keys (GLM, DeepSeek, OpenAI) without explicit Human Owner permission.
   - Default models are permanently anchored to the Google Account Pool (`gemini-3.8-flash-high` and `claude-opus-4-6-thinking`).
4. **Quiescence Detection & Anti-Spam Damping**:
   - Added message idempotency checks and terminal event states (`completed`, `paused`, `idle`).
   - Acknowledgments of completed milestones do not trigger new autonomous turns unless accompanied by a concrete directive.

---

## 8. Deep Dive: Cloud GPU Distillation & Physical Quantization (SDE43)

In project `Bonsai_Sauce_Qwen3.5-2B`, the platform conducted SDE43 Phase 1 Strategy B: quantizing Qwen3.5-2B into a strict ternary representation with physical Q2_0 serialization.

### Cloud Orchestration Highlights
1. **Uncapped Autonomous Run**:
   - Operating under explicit Owner directive (`"i allow to run... REMOVE all fcking caps"`), artificial $2.35 tripwires, $2.65 spend ceilings, and 6-hour runtime abort gates were completely stripped.
2. **RunPod Headless Deployment**:
   - Deployed Pod `51g9hl8u5ycurw` (NVIDIA L40 48GB VRAM @ $0.7180/hr).
   - Executed continuous distillation across **5,000 steps** (87.2 minutes), streaming BF16 activations from an unquantized teacher into a Straight-Through Estimator (STE) ternary student.
3. **Zero-Billing Teardown Verification**:
   - Upon step completion/upload failure, the watchdog verified via RunPod GraphQL API that `myself.pods` returned empty (`[]`), guaranteeing zero ongoing compute or storage billing leakage.
   - Financial ledger: Started at **$5.7949 USD**, ended at **$4.5280 USD**; total realized spend was only **$1.2669 USD** (78.1% under initial budget).
4. **Hugging Face Hub Storage Quota Handling**:
   - Saving frequent ~1.38 GB checkpoints alongside a 19.53 GB optimizer snapshot (`full_training_state.pt`) hit the 30–50 GB private LFS repository limit at Step 5,000.
   - The system executed a fail-closed response, preserving the definitive **Step 4,500 model artifact (`67cb0b24`)** and the Step 2,500 training state while terminating cloud compute.
5. **Offline CPU Verification ($0.00 Cloud Spend)**:
   - Evaluated the physical Q2_0 deserialized model on CPU against 1,024 WikiText-2 tokens:
     - **Unquantized Teacher Baseline**: `3.4092 NLL` (PPL 30.24)
     - **Step 0 Analytical Rounding**: `14.1617 NLL` (PPL 1,413,673)
     - **Step 4,500 Physical Artifact**: **`5.3997 NLL`** (PPL **`221.34`**)
     - **Result**: Recovered **`81.49%`** of the quantization collapse, closing the teacher gap to `+1.9905 nats`.

---

## 9. Deep Dive: Web Studio Hardening & Port Life-Cycle Management

### 1. Starlette Wildcard Route Crash Fix
- **Symptom**: Daemon `task-17532` (`main.py --serve --port 8765`) crashed with `OSError: [WinError 123]` ("The filename, directory name, or volume label syntax is incorrect").
- **Root Cause**: External requests containing wildcards (e.g. `healthz**`) bypassed route matching and triggered Starlette's `StaticFiles.lookup_path`, which attempted to resolve invalid characters on the Windows filesystem.
- **Remedy**:
  - Wrapped `await call_next(request)` in custom boundary middleware inside `web/app.py` to catch `OSError` and immediately return HTTP 404.
  - Added `@app.exception_handler(OSError)` at the FastAPI application level to prevent unhandled OS exceptions from crashing the process.

### 2. Port 8765 Socket Conflict Analysis
- **Symptom**: Daemon failed to start with `WinError 10048` ("Only one usage of each socket address is normally permitted").
- **Root Cause**: The companion tool `ufl-hh-bridge` (UFL Job Scout prototype) was launched by the user on port 8765. When `Agentic Team MCP` attempted to claim 8765, it collided with the active socket.
- **Solution & Status**: Identified PID `22580` (`ufl-hh-bridge\server.py`). The architectural design supports offsetting either service to port `8766`, allowing both local companions to run harmoniously.

---

## 10. Master File & Component Inventory

| File Path | Primary Function & Architectural Role | Major Changes & Upgrades Implemented |
|---|---|---|
| `core/auth_pool.py` | Google Multi-Account Auth Manager | Implemented isolated account directories, Keyring vault swap/capture, sticky agent affinity, cooldown tracking, and max-concurrent gates. |
| `core/config.py` | System Settings & CLI Path Resolver | Added provider settings, XPL keys, Antigravity/Codex executable auto-detection on Windows, and API key redaction. |
| `core/credential_store.py` | Keyring / Windows Vault Interface | Low-level credential read/write/delete methods for isolated terminal logins. |
| `core/multimodal.py` | Telegram Multimodal Handler | Audio speech-to-text processing, image/video attachment downloading, and media token passing. |
| `core/prompts.py` | Global System Prompts & Hard Rules | Injected 5-tier escalation chain, Google pool mandate, CEO role protection, and external model permission gates. |
| `core/providers.py` | Provider Client Adapters | Added Experiential Labs adapter (`xpl`), OpenAI/Codex model routing, GLM streaming, and token usage normalizers. |
| `core/service.py` | Daemon Lifecycle & Lockfiles | Added `ensure_engine()`, process startup file locking (`msvcrt`), descriptor validation, and engine health checks. |
| `core/telegram_bridge.py` | Telegram Bot Supervisor | Strict chat ID whitelisting (`5644286697`), live typing simulation, `[DISPATCH]` and `[SEND_FILE]` directive execution. |
| `core/watchdog_brain.py` | Watchdog Decision Engine | ReAct audit loop, team tree inspection, and autonomous report generation. |
| `engine/actions.py` | Team Action Dispatcher | Handlers for `spawn_worker`, `reconfigure_agent`, `send_team_message`, `escalate_to_ceo`, and `terminate_worker`. |
| `engine/loop_monitor.py` | Anti-Spinning Monitor | Inactivity timeout detection, runaway token loops, and stall mitigations. |
| `engine/message_router.py` | Pub/Sub Event Bus | Asynchronous event broadcasting, WebSocket subscriptions, and cross-project routing. |
| `engine/orchestrator.py` | Central Autonomous Runtime | Multi-agent execution loop, heartbeat management, harness subprocess supervision, and SQLite state persistence. |
| `engine/store.py` | SQLite State Storage | `team.sqlite3` persistence, event streaming ledger, and transaction rollback guards. |
| `harness/cli_runner.py` | Subprocess CLI Interface | Spawns `agy`, `codex`, and `claude` with environment isolation and stdio streaming. |
| `harness/direct_api.py` | Direct Provider API Client | High-performance HTTP client for direct API keys with streaming support. |
| `mcp_server/server.py` | FastMCP Stdio Protocol Bridge | Exposes team tools (`list_projects`, `get_team_tree`, `spawn_worker`, etc.) over standard MCP protocol. |
| `web/app.py` | Web Studio API & WebSocket Server | Added REST endpoints for Auth Pool, static file serving, Starlette wildcard crash patch, and `/ws` telemetry. |
| `web/static/studio.js` | Web Studio Frontend Logic | Topological canvas, SVG link drawing, node drag-and-drop fix, live message animations, and Auth Pool UI. |
| `web/static/telemetry.js` | Metrics & Financial Visualizer | Token metering, cache ratio calculations, and provider burn tracking. |
| `main.py` | Application Entrypoint | CLI parsing (`--serve`, `--mcp`, `--console`), Windows socket reuse handling, and descriptor management. |
| `AGENTS.md` | Master Sentinel Manifesto | Core operating doctrine, Telegram formatting rules, escalation hierarchy, and autonomous directives spec. |

---

## 11. Conclusion & Next Operational Trajectory

Through these systematic upgrades, **Agentic Team MCP** has achieved:
1. **Uninterrupted Autonomy**: True continuous multi-agent execution powered by multi-account quota rotation.
2. **Complete Observability**: Real-time visibility through the Web Studio canvas and direct mobile Telegram interaction.
3. **Rigorous Operational Discipline**: Strict hierarchical delegation that prevents token waste and eliminates manual execution burdens.
4. **Verified AI Research Capabilities**: Cloud-scale deep learning distillation with automated financial and resource governance.

The platform is fully primed for **Phase 2 Execution** (bare-metal GGUF/llama.cpp inference compilation, extended Step 2,500 training resumption, or standardized MMLU/GSM8K benchmarking).

---
*Report sealed and delivered by Root Watchdog.*  
`[SEND_FILE: docs/MCP_SYSTEM_IMPROVEMENTS_COMPREHENSIVE_SUMMARY.md]`
