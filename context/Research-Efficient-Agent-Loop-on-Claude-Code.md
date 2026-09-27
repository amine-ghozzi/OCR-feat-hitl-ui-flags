# Deep Research — Efficiently Running an Agent-in-the-Loop Quality System on Claude Code

*NotebookLM deep research (fresh notebook `3334f753`), 46 web sources. Open-ended query on running a self-improving extraction quality system (Opus reference oracle + judge, regression-gated remediation) efficiently on the Anthropic Agent framework / Claude Code.*

# Engineering a Self-Improving Code Quality System: Architecture and Efficiency Patterns on the Anthropic Agent Framework

An autonomous, self-improving code quality system operating on top of a coding-agent harness represents a significant shift in enterprise software engineering.[1, 2] By combining the deterministic verification of compiler tooling with the advanced reasoning capabilities of frontier large language models, teams can establish a continuous loop that detects, diagnoses, and repairs quality issues without manual intervention.[3, 4] Implementing such a system at scale requires an architecture that optimizes token usage, minimizes latency, isolates context window pressure, and enforces strict governance over agent-driven side effects.[5, 6]

## Neuro-Symbolic Architecture and the Quality Harness

A production-ready agentic quality system avoids the fragility of naive, single-loop chatbot setups by maintaining a strict neuro-symbolic split.[5] The large language model (LLM) functions exclusively as the system control plane for reasoning, planning, and tactical editing, while the programmatic harness retains absolute authority over session state, execution runtime, physical sandbox isolation, and safety policies.[5] This design ensures that agent failures can be programmatically contained, audited, and recovered.[5]

The Anthropic Agent SDK manifests this division across two distinct packages:
* **`anthropic-sdk-python` (v0.97.0):** This is the core API client.[7] It handles key foundational elements such as the Messages API, streaming, the tool use protocol, prompt caching, and model configuration.[7] It also provides support for parallel tool calls (with multiple `tool_use` blocks per response), dynamic tool discovery via `tool_search`, `strict: true` schema enforcement, and fine-grained per-tool streaming through `eager_input_streaming`.[7]
* **`claude-agent-sdk` (v0.1.71):** This is the higher-level agent harness extracted from Claude Code.[7] It provides the iterative agent loop (gathering context, taking action, verifying work, repeating), built-in tools (such as `bash`, `read`, `write`, `web_search`), and Model Context Protocol (MCP) integration.[7] Additionally, it provides the tool permission system, context compaction configuration, and subagent spawning features.[7]

The system architecture implements five distinct, decoupled layers to maintain absolute separation of concerns [5]:

```
  +-----------------------------------------------------------------+
  |                      Surface & Adapter Layer                     |
  |     (CLI, IDE Extensions, Web UI, Webhooks, Cron Triggers)      |
  +                               +                                 +
                                  |
  +-------------------------------v---------------------------------+
  |                     Execution & Runtime Layer                   |
  |   (Event Bus, Session Manager, Checkpoint Engine, Replay Log)   |
  +                               +                                 +
                                  |
  +-------------------------------v---------------------------------+
  |                      Governance & Policy Layer                  |
  |    (Tool Authorization, Approval Gates, Sandbox Containment)    |
  +                               +                                 +
                                  |
  +-------------------------------v---------------------------------+
  |                      Capability & Tool Layer                    |
  |  (Built-in Tools, MCP Connectors, Subagent Spawner, PTC Env)    |
  +                               +                                 +
                                  |
  +-------------------------------v---------------------------------+
  |                      Context & Caching Layer                    |
  |  (System Prompts, CLAUDE.md, Local Workspace, Prompt Caches)   |
  +-----------------------------------------------------------------+
```

To support long-running execution windows, the runtime layer utilizes a highly durable, structured state model.[5] Every operational lifecycle event is modeled, captured, and version-controlled, allowing the system to handle unexpected interruptions, execute deterministic replays of agent errors, and maintain a complete audit trail of automated code mutations.[5]

| State Entity | Key Attributes | Structural Role in Quality Loop |
| :--- | :--- | :--- |
| **Session** [5] | `session_id`, `thread_id`, `execution_mode`, `approval_mode`, `compaction_state` | Represents the resumable execution unit of work. Keeps track of model profiles and the tool catalog version.[5] |
| **Task** [5] | `task_id`, `title`, `status`, `dependencies`, `blockers`, `artifact_refs` | The central coordination object that tracks progress against code quality benchmarks.[5] |
| **Artifact** [5] | `artifact_id`, `virtual_uri` (`artifact://`), `sha256`, `mime_type`, `source_metadata` | Large, persistent outputs (e.g., AST diffs, test reports) stored outside the context window to prevent inflation.[5] |
| **Step** [5] | `step_id`, `execution_kind`, `tool_name`, `status`, `temporal_durations` | Records individual agent tool calls, capturing arguments, execution statuses, and generated outputs.[5] |

Maintaining state durability requires the execution runtime to checkpoint the state model both immediately before and after invoking any external side effect.[5] This preventative logging style isolates destructive filesystem edits, making them easily reversible.[5]

## Subagent Orchestration and Parallelization Strategies

In a self-improving code quality system, a single-agent design can quickly encounter performance bottlenecks when tasked with analyzing a complex, multi-file codebase.[1, 5] To manage context window limits and execute tasks concurrently, the system architecture implements a worker-orchestrator pattern.[5, 8] The lead agent—operating on a high-tier model profile—serves as the centralized coordinator, focusing on analyzing structural telemetry, planning corrective operations, and delegating localized implementation tasks to specialized subagents.[5, 8, 9]

Subagents are spawned dynamically through the `claude-agent-sdk` utilizing the native `Task` tool.[7, 8] This mechanism encapsulates the subagent's execution, supplying it with its own isolated context window, specialized system prompts, and a restricted tool catalog.[5, 7] 

To prevent cascading cost inflation and potential infinite execution loops, the orchestration engine enforces a hard subagent depth limit of one ($D=1$), meaning that spawned workers are strictly prohibited from spawning downstream subagents.[2, 8] Once a subagent terminates its allocated task, only its structured JSON summary and output artifact references are returned to the coordinating lead agent, preserving the coordinator's context window.[5, 8]

For large-scale repository-wide cleanups, the runtime supports parallel orchestration through "Agent Teams" (available experimentally in Claude Code).[3, 8] This framework allows multiple background agents to execute concurrently across separate Git worktrees.[8, 10] Coordination is achieved asynchronously using a shared, decentralized Git-based ledger where subagents autonomously claim, lock, and mark tasks from a shared `tasks.json` queue, ensuring conflict-free operations.[5, 8]

## Externalized Memory Architecture and Filesystem as Working Memory

A primary failure mode of long-running coding agents is context window saturation.[1, 2] As an agent runs multiple command executions, file reads, and edit iterations, the active prompt window swells.[1, 2] This inflation degrades the model's instruction-following capabilities, introduces cognitive drift, and exponentially increases per-turn API latency and costs.[1, 2] 

The system architecture solves this by treating the repository's local filesystem and virtual artifact store as the agent's primary working memory.[5] Large command outputs, compiler telemetry, dependencies, and intermediate code revisions are saved to the workspace filesystem and referenced within the LLM context using lightweight, stable URI handles.[5] This externalized memory pattern allows the model to process large codebases without retaining massive raw datasets in its direct attention span.[5]

```
  +-----------------------------------------------------------------+
  |                       Workspace Root (Git)                      |
  +---------------------------------+-------------------------------+
                                    |
         +--------------------------+--------------------------+
         |                                                     |
  +------v------+                                       +------v------+
  |  CLAUDE.md  | <---+ (Auto-loaded system prompt)     |  .claude/  |
  +-------------+                                       +------+------+
                                                               |
                     +-------------------+---------------------+
                     |                   |                     |
              +------v------+     +------v------+       +------v------+
              |   rules/    |     |  tasks.json |       |  artifacts/ |
              +-------------+     +-------------+       +-------------+
```

The system workspace structures this working memory across dedicated file boundaries to enforce organization [5, 11]:
* **`CLAUDE.md`:** Added to the repository root and automatically loaded into Claude's system prompt at session startup.[11] This file contains the project's technology stack, explicit architectural decisions, canonical code examples, and known workarounds to prevent the agent from writing unaligned or deprecated code.[11]
* **`.claude/rules/`:** A directory containing domain-specific Markdown guidelines.[11] Using frontmatter boundaries, these rules are scoped to load only when the agent accesses matching path patterns (e.g., loading database optimization rules only when editing `src/infrastructure/db/`).[11]
* **`tasks.json`:** A structured task graph updated dynamically to track outstanding issues, completed repairs, and verification outcomes.[5]
* **`artifacts/`:** A directory hosting large, intermediate execution outputs, raw JSON data dumps, and reference fixtures.[5]
* **`plans/` / `reports/`:** Directories used for staging multi-step architectural plans and archiving final test execution telemetry.[5]

Rather than attempting to preload entire codebases or maintain an active semantic vector index (which introduces significant indexing latency, synchronization lag, and external security liabilities), the system adopts a "Search, Don't Index" philosophy.[8] Benchmarks indicate that agentic, real-time code exploration using highly optimized local search tools (such as ripgrep via the native `Grep` and `Glob` tools) outperforms semantic vector retrieval in both accuracy and operational simplicity.[8] The model programmatically queries the workspace on-demand, pulling localized files into the context window only when they are directly relevant to the current plan phase.[5, 8] For specialized symbol resolution, community plugins like `ast-grep` and MCP servers like `Serena` are integrated to provide precise Abstract Syntax Tree (AST) pattern matching.[8]

## Advanced Prompt Caching and Context-Window Management

Prompt caching is a core systems-design requirement for maintaining cost stability and sub-second response times in high-frequency quality loops.[5] Because Anthropic's caching protocol processes inputs sequentially (verifying the `tools` definition array, the top-level `system` prompt, and then the `messages` array in order), even a single token modification at the beginning of the prompt invalidates the entire downstream KV cache.[5, 12, 13] System prompts must be structured statically, placing highly dynamic execution metadata (e.g., timestamps, current user parameters, specific git commit hashes) at the absolute end of the message payload.[12, 14]

The system must account for a critical lookback window limitation: the caching engine can only reference existing cache blocks that have been previously written.[13] If an expanding multi-turn conversation pushes the active cache breakpoint 20 or more content blocks past the last written block, the lookback window fails to locate the prefix, resulting in a complete cache miss.[13] 

To prevent this, the orchestration engine applies a Lookback Mitigation Rule: it programmatically registers up to four explicit, block-level cache breakpoints (`cache_control`) at staggered intervals throughout the message history.[13] As the conversation grows, the system writes new cache markers closer to the execution front, ensuring stable cache hits even during deep debugging runs.[13]

```
Turn 1: -> -> [User Msg 1] (Write Cache 1)
Turn 2: -> -> [User Msg 1 (Cached)] -> (Write Cache 2)
...
Turn N: -> ->... [Msg N-2 (Cached)] -> [Msg N-1] (Write Cache 4)
                                                        ^--- (Keeps cache within lookback range)
```

Furthermore, the system leverages model-specific capabilities to manage caching dynamics:
* **Mid-Conversation System Messages:** When running on Claude Opus 4.8, the system can inject new, localized system instructions mid-conversation without invalidating the top-level system cache.[13] This is achieved by appending a `{"role": "system"}` block directly to the active `messages` array rather than modifying the root system field, preserving the existing prefix cache.[13]
* **Whitespace & Serialization Normalization:** Since even a single unexpected space, carriage return, or JSON key-ordering variation invalidates cached KV tensors, all system-generated prompts and tool parameters must pass through a deterministic serialization filter that strips dynamic whitespace and enforces lexical key ordering before dispatching the payload to the API.[6, 12]

When conversation history inevitably approaches the context limit, the `claude-agent-sdk` triggers automatic context compaction at the `context_token_threshold` (configured to 100,000 tokens).[7] Because this compaction event flushes the message history to clear space, the system intercepts the `PreCompact` hook to serialize the active task state and diff checkpoints directly to the local filesystem.[7] This ensures the agent maintains continuity and can resume the repair loop across compaction boundaries without experiencing cognitive reset.[5, 7]

## Model-Tier Routing and Cost Optimizations

Executing all development operations on frontier models introduces significant financial overhead.[15] To achieve cost-effectiveness, the system implements a dynamic, multi-tier model routing matrix that matches task complexity with the corresponding capability and cost profile.[15, 16]

| Task Category | Optimal Model Tier | Input/Output Token Cost Ratio | Operational Rationale |
| :--- | :--- | :--- | :--- |
| **Triage & Classification** [15, 16] | Claude Haiku 4.5 [9, 17] | Base (approx. 60x cheaper than Opus) [15] | Analyzes raw compiler logs, filters out repetitive issues, and generates initial task complexity classification with high token throughput.[17] |
| **Standard Feature Code & Unit Tests** [15] | Claude Sonnet 4.6 [9, 18] | Moderate (approx. 5x cheaper than Opus) [15, 18] | Handles localized, multi-file code modifications, constructs targeted test coverage, and resolves straightforward linter errors.[15] |
| **Architectural Design & Reference Judge** [9, 17, 18] | Claude Opus 4.8 / 4.7 [19, 20] | Premium | Analyzes systemic codebase dependency graphs, resolves ambiguous bugs, and acts as the reference oracle and LLM judge.[17, 18] |

Navigating this routing matrix requires a strict escalation hierarchy [17, 18]:

```
                     +---------------------------------------+
                     |    Error Detected / Quality Issue     |
                     +-------------------+-------------------+
                                         |
                                         v
                     +---------------------------------------+
                     |       Triage Model (Haiku 4.5)        |
                     |  - Analyzes log context               |
                     |  - Classifies complexity              |
                     +-------------------+-------------------+
                                         |
                        +----------------+----------------+
                        | Simple                          | Complex
                        v                                 v
             +--------------------+            +--------------------+
             | Implementation     |            | Escalation Model   |
             | Model (Sonnet 4.6) |            | (Opus 4.8 / 4.7)   |
             |  - Generates fix   |            |  - Multi-file plan |
             +----------+---------+            |  - Systemic refactor|
                        |                      +----------+---------+
                        | Fail                            |
                        +---------------------------------+
```

If a standard repair attempted by Sonnet 4.6 fails the regression gate test suite three times consecutively (enforcing the Three-Strike Rule), the task is immediately escalated to Opus.[15, 18, 21] This programmatic escalation ensures that the premium model is reserved for edge cases that Sonnet cannot resolve.[15, 17, 18]

For non-urgent, high-volume quality evaluations (e.g., executing overnight code review sweeps or processing massive synthetic test generation runs), the system bypasses interactive inference entirely and routes tasks to the Message Batches API.[3, 20, 22] Batch executions are processed asynchronously within 24 hours (with most completing in under one hour) at a flat 50% discount.[20, 23]

To maximize cache efficiency within asynchronous batches, the system groups identical requests together.[12, 20] Because batch processing does not support the runtime lookback window (making `max_tokens: 0` pre-warming invalid), the system coordinates batched requests to execute in parallel blocks sharing identical `cache_control` headers, forcing the provider-side KV cache to stay warm for concurrent worker executions.[20]

## Deterministic Quality Gates, Regression Testing, and Auto Mode Classifier

A continuous, self-improving quality system must implement robust verification frameworks to ensure that autonomous edits do not introduce regressions.[1, 4] Relying on the LLM to verify its own work in the same session is an anti-pattern; instead, the system enforces a strict quality gate that provides a clear, machine-readable pass/fail signal.[1]

The verification pipeline implements tiered gates to evaluate code changes progressively [24]:
* **Commit Gate:** Runs fast, localized unit tests and syntax checks on files matching the modified scope (`git diff main --name-only`).[3, 24] Execution must complete in under two minutes.[24]
* **Build Gate:** Triggered upon successful commit verification. Compiles the codebase, runs linting rules, and evaluates broader regression test suites, completing in under one hour.[24]
* **Release Gate (LLM-as-a-Judge):** Invokes Claude Opus as a reference judge to execute pairwise evaluations.[4, 24, 25] The model compares semantic outputs against known gold-standard fixtures and checks for logical or architectural regressions that deterministic compilers cannot detect.[4, 24, 25]

```
  +------------------+      Pass      +------------------+      Pass      +------------------+
  |   Commit Gate    | -------------> |    Build Gate    | -------------> |   Release Gate   |
  |  - Localized AST |                |  - Compile/Build |                |  - LLM-as-a-Judge|
  |  - Scoped Tests  |                |  - Integration   |                |  - Pairwise Eval |
  +------------------+                +------------------+                +------------------+
```

To automate authorization decisions during headless execution without exposing the system to unsafe operations, the runtime implements a model-based classifier ("Auto Mode").[26] This classifier uses a conservative evaluation template to analyze the safety and intent of tool calls before execution [26]:

```
  [Proposed Action]
         |
         v
  1. Check against Block Rules (e.g., recursive deletes, network egress to unapproved IPs)
         |
         +---> Match found? ---> YES --->
         |
         | NO
         v
  2. Verify Exceptions (Is this a pre-approved, safe read-only operation?)
         |
         +---> Yes? ------------> YES --->
         |
         | NO
         v
  3. Validate User Intent (Does the command context directly align with the task goal?)
         |
         +---> Authorized? -----> YES --->
         |
         | NO
         +------------------------------>
```

To prevent evasion attacks (where an agent attempts to hide a destructive shell execution inside a benign wrapper script), the classifier analyzes the fully assembled command payload rather than the surface-level invocation text.[26] If an agent writes a Python file that dynamically assembles and runs a Bash execution, the classifier evaluates the final assembled command extracted from the active session transcript before granting execution rights.[26]

For continuous operational monitoring, the system integrates the BigQuery Agent Analytics framework.[4] A single-line plugin captures complete session trajectories (user inputs, prompt templates, tool call arrays, exact outputs, and per-step latencies) and streams them to BigQuery.[4] Evaluation engines run continuous analysis over this data:

* **Categorical Evaluator:** Uses SQL-driven `AI.GENERATE` queries to evaluate grounding and classification server-side.[4] It flags any agent session where the returned answer was fabricated or ungrounded in real tool outputs.[4]
* **Code Evaluator:** Runs fast, deterministic aggregations over session logs, checking resource metrics against predefined budgets [4]:

$$\text{Latency} \le 10\text{s} \quad \land \quad \text{Token Cost} \le 50,000\text{ Tokens} \quad \land \quad \text{Tool Error Rate} \le 5\%$$

Any candidate fix or prompt adjustment that violates these operational thresholds or triggers a gate-class violation (such as a fabrication check dropping from a passing status to zero passing cases) is blocked and immediately reverted.[4, 25]

## Headless CI Automation, Hooks, and Task Queues

Executing a continuous, self-improving loop in CI/CD environments requires a robust non-interactive runtime.[27] Interactive setups suffer from terminal freezes and high memory requirements (1–2 GB of RAM), which are poorly suited for headless runner environments.[27] The headless execution pipeline utilizes the non-interactive CLI flags of Claude Code (`-p` / `--print`) to enforce resource constraints.[10, 27]

```
                             +-------------------+
                             |  Webhook / Cron   |
                             +---------+---------+
                                       |
                                       v
                             +---------+---------+
                             |   Task Queue      |
                             |   (tasks.json)    |
                             +---------+---------+
                                       |
                                       v
                             +---------+---------+
                             |   Runner Host     |
                             |  - Headless Run   |
                             |  - Memory Limit   |
                             +---------+---------+
                                       |
                         +-------------+-------------+
                         |                           |
                  +------v------+             +------v------+
                  | Success     |             | Failure     |
                  | - Git Commit|             | - Log Trace |
                  | - Auto-PR   |             | - Revert Fix|
                  +-------------+             +-------------+
```

The automation framework processes tasks via a decentralized directory-based queue [28]:
* **`TO-DO/`:** Contains pending mini-prompt specs, each representing a single, isolated task.[10, 28]
* **`DONE/`:** Holds successfully completed task specifications to prevent reprocessing.[28]
* **`RETRY/`:** Stores failed task definitions with appended compiler telemetry, awaiting scheduled retry blocks.[28]
* **`logs/` / `results/`:** Directories for recording execution outputs and tracing historical session metrics.[28]

This non-interactive loop executes tasks programmatically, ensuring system safety and efficiency:

```bash
#!/bin/bash
# Headless Quality Verification Loop

TASK_SPEC="TO-DO/001_fix_null_pointer.prompt"
LOG_DIR="logs"
SESSION_ID_FILE="run/active_session.id"

# 1. Execute the prompt in headless mode using strict resource controls
# Restrict permitted tools, set max turn budgets, and output results in JSON
claude -p "$(cat $TASK_SPEC)" \
  --dangerously-skip-permissions \
  --allowedTools "Bash,Read,Edit,Write" \
  --max-turns 6 \
  --output-format json > "$LOG_DIR/execution_out.json"

# 2. Extract session metadata to support continuity tracking
jq -r '.session_id' "$LOG_DIR/execution_out.json" > "$SESSION_ID_FILE"

# 3. Check for memory and context efficiency using the verbose context tool
# Emits a structured token allocation breakdown to prevent cache-busting bloating
claude -p "/context" \
  --resume "$(cat $SESSION_ID_FILE)" \
  --verbose \
  --output-format json > "$LOG_DIR/context_breakdown.json"

# 4. Programmatically disable unused MCP servers to free up context
UNUSED_SERVERS=$(jq -r '.system_tools | select(.hits == 0) |.name' "$LOG_DIR/context_breakdown.json")
for server in $UNUSED_SERVERS; do
  mcp-cli disable "$server"
done
```

The system coordinates these tasks across three execution triggers [3]:
* **Event-Driven Triggers:** Webhooks capture incoming Git commits and pull requests, parsing modifications via `git diff main --name-only` to trigger targeted, affected-scope evaluations.[3]
* **Scheduled Triggers:** Automated cron entries execute overnight, processing deep-dive security audits and resource optimizations using asynchronous batching.[3, 28]
* **On-Demand Triggers:** Engineers invoke custom skills (e.g., `/review-pr` or `/migrate-database`) directly from their development environment to execute manual checks.[3, 11]

To support custom automated actions, the agent SDK provides hooks that run during execution [7, 11]:
* **`PreToolUse`:** Intercepts tool calls before execution to validate parameter payloads.[7, 8]
* **`PostToolUse`:** Analyzes tool outcomes, updates the local `progress.md` log, and logs token usage for cost control.[7, 8, 15]
* **`SubagentStart` / `Stop`:** Manages lifecycle tracing and handles context transitions between orchestrator and worker agents.[7]

## System Pitfalls, Anti-Patterns, and Emerging Best Practices

Deploying autonomous coding agents within enterprise development pipelines can expose teams to systemic anti-patterns that degrade quality and increase operational costs.[11, 15]

```
        Pitfalls & Anti-Patterns               Emerging Best Practices
  +-----------------------------------+  +-----------------------------------+
  | - Using CLAUDE.md as a Linter     |  | - Spec-Driven Code Execution      |
  | - Naive Full-Context Caching      |  | - Grep-Based "Search, Don't Index"|
  | - Letting Agent Loops Run Forever |  | - Structured Error Analysis       |
  | - Routing File Navigation to Opus |  | - Rigid 3-Strike Escalation Gates |
  +-----------------------------------+  +-----------------------------------+
```

### Critical Architecture Pitfalls

* **Using `CLAUDE.md` as a Linter:** Attempting to force an agentic system to parse style guidelines and indentation rules through its system prompt wastes context window space and increases costs.[11] Linters and code formatters are deterministic tasks that must be executed programmatically via pre-commit hooks or local compiler toolchains, keeping the model's focus on logic and planning.[5, 11]
* **Naive Full-Context Caching:** Enabling full-context caching without explicit boundary separation allows dynamic, session-specific variables to enter cache blocks.[12, 14, 29] This results in constant cache invalidation and latency spikes.[14, 29] Only stable, reusable elements should be targeted for caching.[12, 14]
* **Infinite Execution Loops:** Allowing an agent to execute without strict limits can lead to infinite retry loops, where the system repeatedly runs the same failing command.[2] To prevent this, the harness must enforce hard caps on session turn counts, restrict execution times, and programmatically break the loop when repetitive tool calls are detected.[2, 27]
* **Routing File Navigation to Premium Models:** Routing initial codebase exploration and file navigation to Claude Opus instead of Claude Haiku increases input costs with no improvement in accuracy.[9, 17] Haiku is highly efficient at file listing, classification, and text parsing, and should be used to stage code contexts before invoking Sonnet or Opus.[9, 17]

### Emerging Engineering Best Practices

* **Structured Error Analysis:** The system implements a structured framework inspired by established industry practices for error analysis.[21] The pipeline begins with **Open Coding**, where the reference judge parses 20–50 actual agent execution traces and writes free-form notes on logical errors.[21] It then applies **Axial Coding**, where an LLM consolidates these raw notes into distinct, structured failure categories.[21] Finally, **Basic Counting** measures the frequency of each category, allowing developers to target prompt optimizations and tool updates to the most common failure modes.[21]
* **Spec-Driven Code Execution:** Rather than allowing the agent to write code and verify its correctness concurrently within the same session, the harness requires a strict workflow: **Explore, Plan, Implement, and Commit**.[1, 10] The agent must first document its plan in `plans/` and have it validated by a deterministic build check before making any modifications to the codebase.[1, 5, 10]
* **Surgical Changes and Constraint Enforcement:** The system strictly enforces localized edits, instructing the agent to modify only the code directly related to the active task.[21] Adjacent files must not be refactored or modified, preventing semantic drift and minimizing the scope of code review.[21]

---

1. Best practices for Claude Code - Claude Code Docs, [https://code.claude.com/docs/en/best-practices](https://code.claude.com/docs/en/best-practices)
2. AI Agents in Production: Agentic Engineering Workflows | PADISO Blog, [https://www.padiso.co/blog/ai-agents-production-agentic-engineering-workflows/](https://www.padiso.co/blog/ai-agents-production-agentic-engineering-workflows/)
3. Overview - Claude Code Docs, [https://code.claude.com/docs/en/overview](https://code.claude.com/docs/en/overview)
4. Your Agent Can Fix Its Own Prompt. Here's How. | by Eva Khmelinskaya | Google Cloud, [https://medium.com/google-cloud/your-agent-can-fix-its-own-prompt-heres-how-f7bfa970ccb5](https://medium.com/google-cloud/your-agent-can-fix-its-own-prompt-heres-how-f7bfa970ccb5)
5. Modern Agent Harness Blueprint 2026 · GitHub, [https://gist.github.com/amazingvince/52158d00fb8b3ba1b8476bc62bb562e3](https://gist.github.com/amazingvince/52158d00fb8b3ba1b8476bc62bb562e3)
6. Prompt Caching Infrastructure: Reducing LLM Costs and Latency - Introl, [https://introl.com/blog/prompt-caching-infrastructure-llm-cost-latency-reduction-guide-2025](https://introl.com/blog/prompt-caching-infrastructure-llm-cost-latency-reduction-guide-2025)
7. Anthropic Agent SDK: What It Ships vs. What It Leaves to You ..., [https://www.augmentcode.com/guides/anthropic-agent-sdk-what-ships-vs-what-you-build](https://www.augmentcode.com/guides/anthropic-agent-sdk-what-ships-vs-what-you-build)
8. claude-code-ultimate-guide/guide/core/architecture.md at ... - GitHub, [https://github.com/FlorianBruniaux/claude-code-ultimate-guide/blob/main/guide/core/architecture.md](https://github.com/FlorianBruniaux/claude-code-ultimate-guide/blob/main/guide/core/architecture.md)
9. Best AI Model for Coding Agents in 2026: A Routing Guide, [https://www.augmentcode.com/guides/ai-model-routing-guide](https://www.augmentcode.com/guides/ai-model-routing-guide)
10. TIL: The /context command works in non-interactive mode - useful for Ralph loops : r/ClaudeCode - Reddit, [https://www.reddit.com/r/ClaudeCode/comments/1qs5mgj/til_the_context_command_works_in_noninteractive/](https://www.reddit.com/r/ClaudeCode/comments/1qs5mgj/til_the_context_command_works_in_noninteractive/)
11. CLAUDE.md for .NET Developers - Complete Guide with Templates - codewithmukesh, [https://codewithmukesh.com/blog/claude-md-mastery-dotnet/](https://codewithmukesh.com/blog/claude-md-mastery-dotnet/)
12. How to Use Prompt Caching to Cut Claude Code Token Costs in Dynamic Workflows, [https://www.mindstudio.ai/blog/prompt-caching-cut-token-costs-claude-dynamic-workflows](https://www.mindstudio.ai/blog/prompt-caching-cut-token-costs-claude-dynamic-workflows)
13. Prompt caching - Claude API Docs, [https://platform.claude.com/docs/en/build-with-claude/prompt-caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)
14. Don't Break the Cache: An Evaluation of Prompt Caching for Long-Horizon Agentic Tasks, [https://arxiv.org/html/2601.06007v2](https://arxiv.org/html/2601.06007v2)
15. Claude Opus vs Sonnet vs Haiku: Model Routing Guide to Cut Your ..., [https://duet.so/guides/claude-opus-vs-sonnet-model-routing](https://duet.so/guides/claude-opus-vs-sonnet-model-routing)
16. Claude Haiku vs Claude Sonnet vs Claude Opus: Understanding Anthropic's AI Model Family | by Aritra Mitra | May, 2026 | Medium, [https://medium.com/@aritra007mitra/claude-haiku-vs-claude-sonnet-vs-claude-opus-understanding-anthropics-ai-model-family-97d8d22e5542](https://medium.com/@aritra007mitra/claude-haiku-vs-claude-sonnet-vs-claude-opus-understanding-anthropics-ai-model-family-97d8d22e5542)
17. Sonnet vs Haiku: Claude Model Comparison for Developers - Morph, [https://www.morphllm.com/sonnet-vs-haiku](https://www.morphllm.com/sonnet-vs-haiku)
18. Claude Opus vs Sonnet: Which Claude Model Should You Use First? - YingTu, [https://yingtu.ai/en/blog/opus-sonnet](https://yingtu.ai/en/blog/opus-sonnet)
19. Documentation - Claude API Docs - Claude Console, [https://platform.claude.com/docs/en/home](https://platform.claude.com/docs/en/home)
20. Batch processing - Claude API Docs, [https://platform.claude.com/docs/en/build-with-claude/batch-processing](https://platform.claude.com/docs/en/build-with-claude/batch-processing)
21. Agentic Coding for PPC: Claude Code Setup | Peter Jonker, [https://peterjonker.dev/posts/agentic-coding-ppc-claude-code/](https://peterjonker.dev/posts/agentic-coding-ppc-claude-code/)
22. Inference Features | DigitalOcean Documentation, [https://docs.digitalocean.com/products/inference/details/features/](https://docs.digitalocean.com/products/inference/details/features/)
23. Anthropic Launches Message Batches API for Cost-Effective Querying with Claude AI, [https://aiintransit.medium.com/anthropic-launches-message-batches-api-for-cost-effective-querying-with-claude-ai-3bd4ba3a003e](https://aiintransit.medium.com/anthropic-launches-message-batches-api-for-cost-effective-querying-with-claude-ai-3bd4ba3a003e)
24. Regression Testing in CI/CD Pipelines - Complete Guide - Virtuoso QA, [https://www.virtuosoqa.com/post/ci-cd-regression-testing](https://www.virtuosoqa.com/post/ci-cd-regression-testing)
25. Closed-Loop Quality Assurance for Production Clinical AI Documentation - medRxiv, [https://www.medrxiv.org/content/10.64898/2026.05.27.26353977v1.full](https://www.medrxiv.org/content/10.64898/2026.05.27.26353977v1.full)
26. How we built Claude Code auto mode: a safer way to skip permissions - Anthropic, [https://www.anthropic.com/engineering/claude-code-auto-mode](https://www.anthropic.com/engineering/claude-code-auto-mode)
27. Claude Code Non-Interactive Mode: How to Use It on Limited ..., [https://pasqualepillitteri.it/en/news/220/claude-code-non-interactive-mode-limited-hosting](https://pasqualepillitteri.it/en/news/220/claude-code-non-interactive-mode-limited-hosting)
28. How to run Claude Code as non-interactive mini prompts for true 24 ..., [https://amitkoth.com/claude-code-automation-non-interactive/](https://amitkoth.com/claude-code-automation-non-interactive/)
29. Don't Break the Cache: An Evaluation of Prompt Caching for Long-Horizon Agentic Tasks, [https://arxiv.org/html/2601.06007v1](https://arxiv.org/html/2601.06007v1)


---

## Sources (web)

- [Modern Agent Harness Blueprint 2026 · GitHub](https://gist.github.com/amazingvince/52158d00fb8b3ba1b8476bc62bb562e3)
- [claude-code-ultimate-guide/guide/core/architecture.md at ... - GitHub](https://github.com/FlorianBruniaux/claude-code-ultimate-guide/blob/main/guide/core/architecture.md)
- [Claude Opus vs Sonnet vs Haiku: Model Routing Guide to Cut Your ...](https://duet.so/guides/claude-opus-vs-sonnet-model-routing)
- [Anthropic Agent SDK: What It Ships vs. What It Leaves to You ...](https://www.augmentcode.com/guides/anthropic-agent-sdk-what-ships-vs-what-you-build)
- [Best practices for Claude Code - Claude Code Docs](https://code.claude.com/docs/en/best-practices)
- [Overview - Claude Code Docs](https://code.claude.com/docs/en/overview)
- [Don't Break the Cache: An Evaluation of Prompt Caching for Long-Horizon Agentic Tasks](https://arxiv.org/html/2601.06007v2)
- [How to Use Prompt Caching to Cut Claude Code Token Costs in Dynamic Workflows](https://www.mindstudio.ai/blog/prompt-caching-cut-token-costs-claude-dynamic-workflows)
- [TIL: The /context command works in non-interactive mode - useful for Ralph loops : r/ClaudeCode - Reddit](https://www.reddit.com/r/ClaudeCode/comments/1qs5mgj/til_the_context_command_works_in_noninteractive/)
- [Your Agent Can Fix Its Own Prompt. Here's How. | by Eva Khmelinskaya | Google Cloud](https://medium.com/google-cloud/your-agent-can-fix-its-own-prompt-heres-how-f7bfa970ccb5)
- [Prompt caching - Claude API Docs](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)
- [Batch processing - Claude API Docs](https://platform.claude.com/docs/en/build-with-claude/batch-processing)
- [AI Agents in Production: Agentic Engineering Workflows | PADISO Blog](https://www.padiso.co/blog/ai-agents-production-agentic-engineering-workflows/)
- [Agentic Coding for PPC: Claude Code Setup | Peter Jonker](https://peterjonker.dev/posts/agentic-coding-ppc-claude-code/)
- [Best AI Model for Coding Agents in 2026: A Routing Guide](https://www.augmentcode.com/guides/ai-model-routing-guide)
- [CLAUDE.md for .NET Developers - Complete Guide with Templates - codewithmukesh](https://codewithmukesh.com/blog/claude-md-mastery-dotnet/)
- [Prompt Caching Infrastructure: Reducing LLM Costs and Latency - Introl](https://introl.com/blog/prompt-caching-infrastructure-llm-cost-latency-reduction-guide-2025)
- [Documentation - Claude API Docs - Claude Console](https://platform.claude.com/docs/en/home)
- [Anthropic Launches Message Batches API for Cost-Effective Querying with Claude AI](https://aiintransit.medium.com/anthropic-launches-message-batches-api-for-cost-effective-querying-with-claude-ai-3bd4ba3a003e)
- [Claude Code Non-Interactive Mode: How to Use It on Limited ...](https://pasqualepillitteri.it/en/news/220/claude-code-non-interactive-mode-limited-hosting)
- [Don't Break the Cache: An Evaluation of Prompt Caching for Long-Horizon Agentic Tasks](https://arxiv.org/html/2601.06007v1)
- [Claude Haiku vs Claude Sonnet vs Claude Opus: Understanding Anthropic's AI Model Family | by Aritra Mitra | May, 2026 | Medium](https://medium.com/@aritra007mitra/claude-haiku-vs-claude-sonnet-vs-claude-opus-understanding-anthropics-ai-model-family-97d8d22e5542)
- [Sonnet vs Haiku: Claude Model Comparison for Developers - Morph](https://www.morphllm.com/sonnet-vs-haiku)
- [Claude Opus vs Sonnet: Which Claude Model Should You Use First? - YingTu](https://yingtu.ai/en/blog/opus-sonnet)
- [Inference Features | DigitalOcean Documentation](https://docs.digitalocean.com/products/inference/details/features/)
- [Regression Testing in CI/CD Pipelines - Complete Guide - Virtuoso QA](https://www.virtuosoqa.com/post/ci-cd-regression-testing)
- [Closed-Loop Quality Assurance for Production Clinical AI Documentation - medRxiv](https://www.medrxiv.org/content/10.64898/2026.05.27.26353977v1.full)
- [How we built Claude Code auto mode: a safer way to skip permissions - Anthropic](https://www.anthropic.com/engineering/claude-code-auto-mode)
- [How to run Claude Code as non-interactive mini prompts for true 24 ...](https://amitkoth.com/claude-code-automation-non-interactive/)
- [Anthropic Agent SDK + ACP — Governance Install Guide](https://agenticcontrolplane.com/integrations/anthropic-agent-sdk)
- [Build a Custom Claude Agent With the Anthropic Agent SDK | systemprompt.io](https://systemprompt.io/guides/build-custom-claude-agent)
- [Model Context Protocol (MCP) Tool Descriptions Are Smelly! Towards Improving AI Agent Efficiency with Augmented MCP Tool Descriptions - arXiv](https://arxiv.org/html/2602.14878v1)
- [Optimizing MCP for Production: 10 Proven Performance Techniques - ERP Today](https://erp.today/mcp-performance-optimization-production-ai/)
- [[Workflow] Optimizing MCP Tool Design for LLM Agent Efficiency and Cost Reduction: Lessons from a Token Usage Experiment : r/ClaudeWorkflows - Reddit](https://www.reddit.com/r/ClaudeWorkflows/comments/1tvj2pa/workflow_optimizing_mcp_tool_design_for_llm_agent/)
- [MCP Gateway Comparison: 10 Tools for Governing AI Agent Access - Zuplo](https://zuplo.com/blog/mcp-gateway-comparison)
- [What you need to know about the Model Context Protocol (MCP) - Merge.dev](https://www.merge.dev/blog/model-context-protocol)
- [[2601.06007] Don't Break the Cache: An Evaluation of Prompt Caching for Long-Horizon Agentic Tasks - arXiv](https://arxiv.org/abs/2601.06007)
- [Don't Break the Cache: An Evaluation of Prompt Caching for Long-Horizon Agentic Tasks - arXiv](https://arxiv.org/pdf/2601.06007)
- [Tool Search: Redefining Agent Tool Calling (Epsilla Insights)](https://www.epsilla.com/blogs/2026-04-19-tool-search-redefining-agent-tool-calling-epsilla-)
- [The Complete Developer's Guide to Claude Code Commands | Tim Dietrich](https://timdietrich.me/blog/claude-code-commands-guide/)
- [Create a Message Batch - Claude API Reference](https://platform.claude.com/docs/en/api/messages/batches/create)
- [Claude Opus 4/7 + Claude Code: 7 Practical Tips for Maximizing Extended Context](https://www.damiangalarza.com/posts/2026-04-30-claude-opus-4-7-claude-code-tips-extended-context/)
- [Anthropic AI Introduces the Message Batches API: A Powerful and Cost-Effective Way to Process Large Volumes of Queries Asynchronously - MarkTechPost](https://www.marktechpost.com/2024/10/09/anthropic-ai-introduces-the-message-batches-api-a-powerful-and-cost-effective-way-to-process-large-volumes-of-queries-asynchronously/)
- [Anthropic's Claude models | Gemini Enterprise Agent Platform | Google Cloud Documentation](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/partner-models/claude)
- [Claude Opus 4.7 Deep Dive: Capabilities, Migration, and the New Economics of Long-Running Agents | Caylent](https://caylent.com/blog/claude-opus-4-7-deep-dive-capabilities-migration-and-the-new-economics-of-long-running-agents)