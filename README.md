# QuantStack

**The Operating System for Quantitative Market Intelligence**

QuantStack is the complete architecture specification for an enterprise-grade quantitative intelligence platform: an AI-powered market research system for Indian markets that collects data via Angel One SmartAPI, analyzes markets with ensemble machine learning and Bayesian regime detection, stress-tests every idea in a Digital Twin simulation, and delivers fully explained trading signals (entry, stop loss, target, reasoning) via Telegram.

> **This is not an automated trading bot.** The platform behaves like an institutional research desk: deterministic quantitative logic makes every decision, the LLM only explains, and the final decision always remains with the trader.

## Documentation

This repository is an [MkDocs](https://www.mkdocs.org/) site using the [Material](https://squidfunk.github.io/mkdocs-material/) theme with Mermaid diagrams.

```bash
# install
pip install mkdocs-material

# live preview at http://127.0.0.1:8000
mkdocs serve

# build static site into ./site
mkdocs build
```

## Contents

| Section | Description |
|---------|-------------|
| [Home](docs/index.md) | Platform overview, signal pipeline, design principles |
| [Architecture Review](docs/overview/architecture-review.md) | Scored assessment of the original design and the 17 missing institutional components |
| [Master Blueprint](docs/overview/master-blueprint.md) | The reorganized 16-phase build plan with implementation prompts |
| [Architecture](docs/architecture.md) | Layered system architecture with Mermaid diagrams |
| [Volumes 1–10](docs/volumes/) | 18 engineering design volumes, from foundation to enterprise infrastructure |
| [Implementation Guide](docs/implementation-guide.md) | Build order, acceptance-criteria discipline, cross-cutting rules |
| [Roadmap](docs/roadmap.md) | Completed volumes, Volume 11 (SaaS) & 12 (autonomous quant), companion books |
| [Glossary](docs/glossary.md) | Definitions of every platform term |

## The volume roadmap

| Volume | Purpose |
|--------|---------|
| 1 | Foundation & System Architecture |
| 2 | Data Collection & Market Intelligence Layer |
| 3 | Feature Store & Data Quality |
| 4 | Market Intelligence & Regime Analysis |
| 5 | Opportunity Detection, Prediction & Conviction |
| 5.5 | Alpha Research Engine |
| 5.75 | Opportunity Portfolio Intelligence (OPIE) |
| 5.9 | Decision Intelligence & Meta-Orchestrator |
| 5.95 | Simulation & Digital Twin Engine |
| 5.99 | Signal Orchestration & Execution Feasibility (SOEFE) |
| 5.999 | Enterprise SDK & Plugin Ecosystem |
| 6 | Risk Intelligence & Trade Construction |
| 6.1 | Dual Risk Intelligence Framework |
| 6.5 | Trade Lifecycle & Adaptive Management |
| 7 | AI Intelligence, Explainability & Communication |
| 8 | Workspace & User Experience |
| 9 | Evaluation, Backtesting & Continuous Learning |
| 10 | Enterprise Infrastructure & Production Operations |

## Key design principles

1. **Deterministic before AI** — entries, stops, targets, and sizes come from testable quantitative logic; the LLM explains, it never decides.
2. **Modularity** — collectors, engines, and delivery are isolated, broker-agnostic, and independently replaceable.
3. **Measurability** — every collector, feature, model, and signal carries quality, confidence, and freshness scores.
4. **Versioning & reproducibility** — features, models, and decisions are versioned and replayable at any historical timestamp.

## Source

This documentation was generated from a design conversation ("Trading Bot Architecture Review") that iteratively developed the platform specification volume by volume.
