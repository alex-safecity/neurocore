# Changelog

All notable changes to NeuroCore will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.5.0] — 2026-10-14

Correct resume for graph flows and a dependency-free core. Requires
`flowengine>=0.7.0`.

### Fixed

- **`resume_blueprint` re-enters at the suspended node.** For flows routed
  through flowengine's `GraphExecutor` (ports, conditions, cycles) the resume
  passed a context with `suspended_at_node` cleared, so the executor restarted
  from the roots (cyclic flows re-executed every upstream node) or skipped the
  suspended node when it sat behind a port edge and reported the run complete
  (acyclic flows). The suspended node is now handed to the executor
  (`execute(..., resume_node=...)`), completed nodes are skipped and propagate
  the ports they set, and nothing upstream re-runs. The other execution paths
  are unchanged.

### Added

- **The guarded invocation contract** (`neurocore.runtime.invocation`). Every
  skill instance the registry hands out is wrapped (`Guarded` / `GuardedAsync`)
  so that each call, on every execution path, goes through one function,
  `invoke_skill`: it opens a per-step unit of work (`InvocationPolicy.unit_of_work`,
  carried in the context — `current_unit_of_work(context)`), asks the guard
  *before* the call (BLOCK → the step does not run and the implicit `error`
  port is taken; REVIEW → the run suspends at the node and the resume
  re-evaluates; WARN → recorded) and *after* it (BLOCK → the step's writes are
  discarded in namespaced mode), commits on success and rolls back on any
  exception. Verdicts other than ALLOW are recorded in
  `metadata.guard_decisions`. The policy — guard, unit of work, observer —
  is installed on `SkillRegistry(invocation=...)`; the default allows
  everything with a no-op unit of work. `registry.wrap()` is the wrap point
  (`registry.wrapped` counts it) and `_create_skill_instances` uses it, so a
  path that bypassed the contract would have to bypass the registry.
- **`RunStore` resume protocol, row lease, envelope log, unit of work.**
  `RunRecord` gains `wait_step_id`, `resume_seq`, `resume_action_id`,
  `resume_payload`, `lease_owner`, `lease_until`; `RunStatus.QUEUED` is the
  state between an accepted resume and a worker's claim. New store methods:
  `request_resume` (a conditional update that flips suspended → queued exactly
  once; the same `action_id` replays, another one conflicts, a different
  waiting step is refused), `acquire_lease` / `renew_lease` / `release_lease` /
  `expired_leases` (one worker executes a run at a time; a stale
  `expected_resume_seq` is refused so the worker drops its message),
  `save_envelope` / `latest_envelope` / `load_envelopes` (what the platform last
  sent, ordered by `seq`), and `unit_of_work` (a transaction every store write
  joins until `commit` or `rollback`). SQLite files from 0.4.0 get the new
  columns on open. A conformance suite runs every behaviour against both
  stores, including fifty parallel resumes accepting exactly one.
- `FlowEventType.ENVELOPE`, `GUARD_DECISION`, `TIMER_FIRED` for the runtime's
  own events on a run's stream.
- **Data bindings pass through.** `FlowGraph` gains `bind` (inputs:
  `"<node_id>.<key>"` reference, `{value: ...}` literal or `{expr: ...}`
  expression; outputs: rename within the node's namespace), `provenance`
  (per-input provenance class, carried for policy) and `max_visits` (previously
  accepted and silently dropped); `_build_flow_config` forwards all three, so a
  blueprint with any `bind` runs in flowengine's namespaced mode — two
  instances of one skill no longer collide on keys.
- **Extended `SkillMeta`**: `ports`, `risk_level`, `effects`,
  `requires_approval`, `cost`, `data_classes`, typed `inputs`/`outputs`
  (`consumes`/`provides` are kept and merged). `skillmeta_to_componentmeta`
  maps every field onto flowengine's `ComponentMeta`.
- **`SkillRegistry(strict_meta=True)`** refuses skills whose `risk_level` is
  undeclared, for governed catalogues; the default registry stays lenient and
  maps an undeclared risk to `low` as before.

### Changed

- **One executor path for graph flows.** Every `flow.type: graph` blueprint runs
  on flowengine's `GraphExecutor`; the concurrent layer executor
  (`_execute_dag_concurrent`) and the routing predicate are removed. Independent
  nodes now run one after the other in topological order (deterministic, so
  the last writer of a shared key is deterministic too — bindings give each
  node its own namespace). `flow.settings.executor` may name `"graph"`; any
  other value is a `BlueprintError`.
- **`anthropic` is an optional dependency** (`neurocore-ai[anthropic]`), like
  every other provider SDK. The core — blueprints, skills, graph execution,
  persistence — installs without any LLM SDK; `all-providers` and `dev` include
  it. `AnthropicProvider` imports the SDK lazily as before.

## [0.4.0] — 2026-06-15

Realistic graph execution: NeuroCore graph flows now honor edge **ports**,
edge **conditions**, and **cycles/`max_iterations`** by routing through
flowengine's `GraphExecutor`. Requires `flowengine>=0.6.0`.

### Added

- **Edge conditions** — `FlowEdge` gains an optional `condition` (a safe Python
  expression over `context`, e.g. `context.data.score > 0.5`). An edge activates
  when its `port` matches AND its `condition` is True.
- **Hybrid graph routing** — `execute_blueprint` now routes a graph flow through
  flowengine's `GraphExecutor` whenever it uses edge ports/conditions or contains
  a cycle (`_graph_needs_executor`); plain DAGs keep the existing concurrent
  layer executor. The engine path runs async skills via
  `GraphExecutor.execute_async` and records run history (steps + `completed_nodes`)
  like the other tracked paths. This makes port-driven early-exits, conditional
  branching, and Lean-style repair loops (e.g. the `neurocore-skill-math` proof
  workers) actually execute as drawn.

### Changed

- Version bumped to 0.4.0; minimum `flowengine` raised to `>=0.6.0` (edge
  conditions + async graph execution live there).

## [0.3.0] — 2026-06-14

Turns NeuroCore from a framework into a production agent **runtime**: local-LLM
providers, durable run history with resume/replay, human-approval gates, MCP
tool-calling, and template scaffolding. All changes are backward-compatible.

### Added

#### OpenAI-compatible, Ollama, vLLM & LiteLLM providers
- `OpenAIProvider` now accepts `base_url` and a configurable `provider_name`
- `build_provider()` aliases `openai-compatible`, `ollama`, `vllm` (sensible
  default base URLs for the latter two) — all speak the OpenAI wire format
- `LiteLLMProvider` for routing to 100+ model APIs
- `LLMConfig` gains `base_url` and `api_key_env` (resolved from the environment);
  `get_skill_config()` injects `llm_base_url`
- New extras: `litellm`, `local`, `all-providers`

#### Durable run history & persistence (`neurocore.persistence`)
- `RunStore` ABC with `SQLiteRunStore` (stdlib `sqlite3`, default) and
  `InMemoryRunStore` backends; `RunRecord` / `StepRecord` Pydantic models
- `build_run_store()` factory and `PersistenceConfig` (`enabled`, `backend`,
  `path`, `persist_step_snapshots`); `NeuroCoreConfig.runs_db_path`
- Adapter exposing a `RunStore` as a flowengine `CheckpointStore`

#### Executor tracking, resume & replay
- `execute_blueprint_tracked()` records a run + per-step history across the
  sequential, DAG, and FlowEngine paths; `metadata.completed_nodes` now populated
- `resume_blueprint()` resumes suspended or failed runs, skipping completed steps
- `load_and_run(..., track=True)` persists by default

#### Human-in-the-loop approval gate
- Built-in `ApprovalSkill` (always discoverable) that suspends a run for sign-off
- `approval:` blueprint step sugar desugaring to the built-in skill

#### `neurocore runs` and `neurocore mcp` CLI
- `runs list / inspect / replay / resume / approve` (run-id prefix matching)
- `neurocore run --stream` renders human-readable progress (JSONL with `--json`)
- `neurocore mcp list-tools / call` (via the optional `neurocore-skill-mcp`)

#### Template scaffolding
- `neurocore new <template> <name>` with templates: `rag-agent`,
  `research-agent`, `ollama-agent`, `multi-agent-debate`, `tool-agent`;
  `neurocore new --list`

#### MCP tool-calling
- `neurocore-skill-mcp` package — invoke MCP server tools (stdio + streamable
  HTTP) from a blueprint via `type: mcp-tool`

#### Examples & deployment
- `examples/local-ollama-agent` (with `docker-compose.yaml`) and
  `examples/human-approval-agent`; root `Dockerfile`

#### Docs
- New guides: concepts, providers, persistence-and-runs, human-in-the-loop,
  skill-authoring, and "NeuroCore vs LangGraph"; README rewritten around a
  5-minute demo

### Changed
- Version bumped to 0.3.0
- `LLMProvider.stream` Protocol signature fixed so concrete async-generator
  providers type-check cleanly
- Scaffold templates excluded from the library's ruff/mypy surface

## [0.2.1] — 2026-04-03

### Added

#### GeminiProvider (NC-FIX-001)
- `GeminiProvider` class implementing `LLMProvider` protocol using `google-genai` SDK
- Automatic message format conversion (system → `system_instruction`, assistant → model role)
- Async `complete()` and `stream()` via `client.aio.models.generate_content()`
- `"gemini"` handled by `build_provider()` factory
- `google-genai>=1.0` optional dependency group (`pip install neurocore-ai[gemini]`)
- `neurocore-skill-gemini-auditor` — adversarial reproducibility auditor using Gemini
- Updated `ac1-research.flow.yaml` blueprint: reviewer-auditor now uses Gemini provider

#### Retry & Exponential Backoff (NC-FIX-002)
- `max_retries`, `retry_delay_base`, `retry_delay_max`, `retry_on` fields on `SkillMeta`
- Truncated exponential backoff with full jitter in `_run_skill_async()`
- Warning-level structured logging on each retry attempt
- `neurocore-skill-base` shared package with `RateLimitError`, `ServiceUnavailableError`, `check_response()`
- All 13 research skill packages updated with retry configuration (v0.1.1)

### Changed
- Version bumped to 0.2.1
- `build_provider()` error message now includes `gemini` in expected providers
- `GeminiProvider` exported from `neurocore.llm` package

## [0.2.0] — 2026-04-03

### Added

#### Async Skill Execution (NC-001)
- `AsyncSkill` base class for skills whose `process()` is an async coroutine
- `is_async_skill()` helper to detect async skills at runtime
- Automatic async/sync detection — blueprints with any async skill execute in an asyncio event loop
- Sync skills wrapped automatically via `run_in_executor` when mixed with async skills

#### Concurrent DAG Execution (NC-002)
- Topological-layer DAG execution using `asyncio.gather()` for concurrent node processing
- Kahn's algorithm for computing execution layers from blueprint edges
- Cycle detection with clear error messages
- Context merging across concurrent nodes (last-write-wins per key)

#### Streaming Execution (NC-003)
- `execute_blueprint_stream()` async generator yielding `FlowEvent` objects in real-time
- `FlowEvent` and `FlowEventType` dataclasses for structured event data
- Event types: `FLOW_STARTED`, `STEP_STARTED`, `STEP_COMPLETED`, `STEP_FAILED`, `FLOW_COMPLETED`, `FLOW_FAILED`
- `--stream` flag on `neurocore run` for JSONL event output
- Duration tracking per step and per flow

#### LLM Provider Protocol (NC-004)
- `LLMProvider` runtime-checkable protocol for pluggable LLM backends
- `AnthropicProvider` — Claude integration via anthropic SDK
- `OpenAIProvider` — GPT integration via openai SDK
- `MockProvider` — deterministic testing provider with response queuing
- `build_provider()` factory from config dict
- `LLMMessage` and `LLMResponse` dataclasses
- `requires_llm` flag on `SkillMeta` — automatic provider injection during skill init
- `llm` attribute on `Skill` base class

#### AC1 Research Skills (NC-005) — separate pip-installable packages
- `neurocore-skill-arxiv` — arXiv preprint search and PDF download
- `neurocore-skill-openalex` — OpenAlex academic paper search with citation data
- `neurocore-skill-semantic-scholar` — Semantic Scholar search, recommendations, citations
- `neurocore-skill-tavily` — Tavily AI-optimized web search (search/extract/research modes)
- `neurocore-skill-exa` — Exa.ai neural semantic search for papers
- `neurocore-skill-core-api` — CORE full-text open access paper retrieval
- `neurocore-skill-unpaywall` — Unpaywall DOI-to-PDF resolver with concurrent lookups
- `neurocore-skill-qdrant` — Qdrant vector store (search/upsert modes)
- `neurocore-skill-grobid` — GROBID PDF-to-TEI XML parser
- `neurocore-skill-lean4` — Lean4 formal proof verification with certificate generation
- `neurocore-skill-sagemath` — SageMath computation (eval/script modes)
- `neurocore-skill-sympy` — SymPy symbolic math with sandboxed evaluation
- `neurocore-skill-oeis` — OEIS integer sequence lookup

#### AC1 Blueprint (NC-006)
- Reference `ac1-research.flow.yaml` blueprint wiring all AC1 skills into a DAG pipeline
- Concurrent literature discovery (arXiv + OpenAlex + Semantic Scholar + Tavily)
- Concurrent adversarial review (skeptic + alt-theory + auditor)
- Full pipeline: frontier scan → literature → full text → PDF parsing → knowledge integration → reasoning → review → verification → storage

#### Config Schema Extensions (NC-007)
- `LLMConfig` model for project-level LLM configuration
- Project-level LLM config auto-injected into skill configs when `llm_provider` not set at skill level
- Skill-level config overrides project-level LLM config
- Updated `neurocore init` scaffold template with LLM config section

### Changed
- Version bumped to 0.2.0
- `anthropic>=0.42` added as core dependency
- `pytest-asyncio>=0.24` added to dev dependencies
- `openai>=1.0` available as optional dependency (`pip install neurocore-ai[openai]`)
- Graph flow validation now requires both `nodes` and `edges` to be defined
- Project URLs updated to point to correct GitHub repository

## [0.1.0] — 2026-02-28

Initial release of NeuroCore — a pluggable, YAML-driven framework for building
agentic AI applications.

### Added

#### Core Framework
- `Skill` base class extending FlowEngine's `BaseComponent` with declarative metadata
- `SkillMeta` frozen dataclass for skill identity, versioning, dependencies, and data contracts
- Config validation against JSON Schema with type checking
- Health check interface for runtime monitoring

#### Configuration System
- YAML-based project configuration via `neurocore.yaml`
- `.env` file support with environment variable overlay
- Nested env var overrides using double-underscore syntax (`NEUROCORE_LOGGING__LEVEL`)
- Path resolution relative to project root
- `pydantic-settings` backed schema with type validation

#### Structured Logging
- `structlog` integration with console (colored, dev) and JSON (structured, production) renderers
- Per-module named loggers via `get_logger("module")`
- File output support alongside stderr

#### Skill Discovery
- Directory scanning — auto-discovers `Skill` subclasses from `.py` files in `skills/`
- Entry point scanning — loads skills from `neurocore.skills` entry point group in installed packages
- Unified `SkillRegistry` with entry points taking precedence over directory-discovered skills
- Registry supports `register()`, `get()`, `list_skills()`, `has()`

#### Runtime & Execution
- Blueprint parser — loads YAML flow files with skill-aware component resolution
- Blueprint validation — three-stage check (YAML parse, structure, skill references)
- Config merging — `neurocore.yaml` base + blueprint overlay (blueprint wins)
- FlowEngine executor — resolves skill names, creates instances, builds FlowConfig, executes
- `load_and_run()` convenience function for single-call execution
- Custom error hierarchy: `NeuroCoreError`, `ConfigError`, `SkillError`, `BlueprintError`, `ExecutionError`

#### CLI
- `neurocore init <name>` — scaffold new projects with templates
- `neurocore run <blueprint>` — execute blueprints with `--data`, `--json`, `--verbose` options
- `neurocore skill list` — Rich table of all discovered skills
- `neurocore skill info <name>` — detailed skill metadata, config schema, and health check
- `neurocore validate <blueprint>` — validate blueprints without executing
- `neurocore --version` — version display

#### NeuroWeave Integration (separate package: `neurocore-skill-neuroweave`)
- `NeuroWeaveSkill` wrapper bridging NeuroWeave's async API to sync FlowEngine
- Three operation modes: `process` (extract), `query` (retrieve), `context` (both)
- Async-to-sync bridge via `asyncio.run()`
- Lazy NeuroWeave initialization on first `process()` call
- Entry point registration for automatic discovery

#### Documentation & Examples
- README with quickstart, skill development guide, CLI reference, and configuration reference
- `echo_agent` example — minimal working project with echo and upper skills
- `research_agent` example — NeuroWeave-powered knowledge graph agent

#### Testing
- 265 unit and integration tests in neurocore
- 17 unit tests in neurocore-skill-neuroweave
- pytest + pytest-cov + pytest-mock test infrastructure
