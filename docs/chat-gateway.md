# Chat gateway

已有自己模型调用与工具循环的宿主可改走独立的 [Hook 接入示例](hook-integration.md)：只向 Serein 取前情，在实际模型请求成功后由宿主登记交付。Hook 不会替宿主调用聊天模型或归档对话。

Upstream configuration uses the `gateway.upstreams` structure: one connection
per upstream, multiple model IDs, and optional public aliases mapped to real upstream names.
The public implementation aggregates the configured model lists for `/v1/models` and resolves
each chat request independently. Duplicate public aliases are rejected instead of silently
choosing the first upstream. This configuration does not provide key rotation or failover.

## Context and delivery

- Recall uses actual current user text after removing external attachments, workspace blocks,
  leading `proxy_sender` and `【系统提示…】`, and recognized external-context sections.
  A pure attachment or a tool continuation does not start a new memory query.
- Operit rewriting is enabled by default and configurable. Stable context remains near the
  leading system instructions; current activity and recalled memory enter the current user
  message. Multimodal and tool protocol messages retain the original conservative handling.
- A tool continuation can reuse the exact prepared prefix. The source prefix and model/tools
  contract must match; snapshots expire after one hour and are cleared after a final answer.
  Missing reasoning fields are restored only for matching assistant tool calls in that window.
- Prompt cache keys and retention settings preserve caller-supplied values. Native Anthropic
  mode supports automatic cache control or explicit breakpoints on system, tools and an earlier
  assistant message. The current user turn receives no explicit breakpoint. Token estimates
  and breakpoint distances are heuristic estimates.
- Model, identity, Operit and memory setting changes separate snapshot namespaces. There is no
  cross-window result cache. The proxy records successful upstream deliveries; selection and
  incomplete/failed streams do not become successful delivery receipts.
- The public host uses a five-response recent-card window. The core still checks
  cooldown after selecting the final cards and never substitutes a third-place candidate.
