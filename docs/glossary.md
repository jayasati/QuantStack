# Glossary

Definitions of platform terms, grouped alphabetically. The volume in parentheses is where the term is specified in detail.

## A

- **Adaptive Stop Engine** — Manages dynamic stop losses (break-even, ATR/swing/structure/time/VWAP/volatility trails) derived from current regime, volatility, liquidity, and event risk instead of fixed percentages. (Volumes 6.1, 6.5)
- **AI Gateway** — Routing layer that sends work to fast, reasoning, or offline LLMs based on latency, complexity, cost, and quality, behind a common interface. (Volume 7)
- **AI Governance** — Hard rules forbidding the LLM from stating unsupported facts, modifying quantitative outputs, or suggesting trades outside approved Decision Objects, with full audit logs. (Volume 7)
- **AI Personas** — Configurable communication styles (Quant Analyst, Risk Manager, Educational Tutor, etc.) with defined tone, vocabulary, and detail level. (Volume 7)
- **AI Verification Engine** — Fact-checks every LLM-generated statement against platform evidence, rejecting unsupported claims and scoring per-sentence confidence. (Volume 7)
- **Alpha Decay** — The gradual loss of a feature's predictive power as markets evolve, measured via half-life and decay rate. (Volume 5.5)
- **Alpha Knowledge Base** — Tagged, semantically searchable archive of experiments, successful/failed features, and lessons learned. (Volume 5.5)
- **Alpha Research Engine** — Isolated, sandboxed subsystem that continuously discovers, evaluates, and promotes new predictive features, models, and strategies without touching production. (Volume 5.5)
- **API Gateway** — Single entry point handling authentication, rate limiting, routing, caching, versioning, and audit logging for all platform services. (Volume 10)
- **APScheduler** — The mandated in-process scheduler for all recurring tasks; OS cron jobs are prohibited. (Volume 1)

## B

- **Bayesian Regime Detection** — Probabilistic regime classification assigning continuous probabilities to each regime, allowing blended states instead of hard switching. (Volume 4)
- **Brier Score** — Calibration metric measuring the accuracy of predicted probabilities. (Volume 9)
- **Broker Abstraction Layer** — Interface layer (Broker Interface → Angel One SmartAPI adapter, future Zerodha/IBKR adapters) so business logic never knows which broker is used. (Volumes 1–2)

## C

- **Collector** — Independent, restartable, self-scheduled unit that ingests one data source and emits the standard normalized schema. (Volume 2)
- **Collector Registry** — Automatic collector discovery, scheduling configuration, dependency resolution, enable/disable, and runtime status. (Volume 2)
- **Composite Market Intelligence Score** — A 0–100 aggregate of trend, volatility, breadth, liquidity, macro, sector, flow, correlation, structure, and event-risk intelligence. (Volume 4)
- **Conflict Resolution Engine** — Detects and classifies contradictory evidence (Minor/Moderate/Critical) and resolves it via weights, historical reliability, regime, and confidence. (Volume 5.9)
- **Context Builder** — Assembles structured, versioned JSON context packages (Decision Object, Trade Blueprint, risk, simulation, analogs) as the LLM's only input. (Volume 7)
- **Conviction Engine** — Weighted combiner of evidence sources (ML probability, market context, etc.) yielding a fully explained conviction score, grade, stability, and trend. (Volume 5)

## D

- **Data Quality Engine** — Quality gate scoring every collector 0–100 on freshness, completeness, latency, and reliability; poor data quality automatically reduces conviction. (Volume 2)
- **Decision Object** — The universal handoff format bundling market state, features, prediction, conviction, opportunity rank, risk, context, and the final decision with its reason. (Volume 5.9)
- **Decision Score** — Composite score (with confidence, grade, stability) combining prediction, opportunity, risk, liquidity, model agreement, and policy compliance. (Volume 5.9)
- **Delivery Intelligence Engine** — Decides urgency, audience, channel, priority, batching, and retry policy for each piece of intelligence. (Volume 8)
- **Digital Twin** — A versioned, replayable snapshot of the full market state cloned from a Decision Object for safe simulation and stress testing. (Volume 5.95)

## E

- **Early Warning Engine** — Continuous monitor of live-trade conditions that escalates Warning → Critical → Exit recommendation before a trade fails. (Volume 6.1)
- **Ensemble Prediction** — Weighted blend of multiple models (LightGBM, CatBoost, XGBoost, Random Forest, Extra Trees, Logistic Regression) producing probability, confidence, uncertainty, and disagreement. (Volume 5)
- **Error Budget** — SRE concept quantifying allowable unreliability within an SLO. (Volume 10)
- **Event Bus** — Asynchronous publish/subscribe backbone with retries, dead-letter queue, idempotency, versioning, and tracing; all inter-module communication is event-driven. (Volumes 1–2)
- **Evidence Engine / Evidence Graph** — Collects weighted, versioned evidence items from every subsystem into a graph of supports/contradicts/depends-on relationships traversed during decisions. (Volume 5.9)
- **Execution Feasibility Engine** — Evaluates spread, depth, volume, latency, slippage, impact, and volatility to reject trades that cannot realistically be executed. (Volume 5.99)
- **Explainability Package** — Structured JSON (evidence graph, SHAP features, analogs, simulation summary, decision path) that is the LLM's only input. (Volume 5.99)
- **Exposure Engine** — Measures aggregate exposure (sector, beta, macro, currency, rates) implied by the signal set, producing a Diversification Score. (Volume 5.75)

## F

- **Failure Mode Engine** — Predicts how a trade fails (false breakout, liquidity sweep, gap, macro event) with probability, mitigation, and early warning signals. (Volume 6)
- **Feature Dependency Graph** — Graph of upstream/downstream feature relationships enabling automatic recalculation. (Volume 3)
- **Feature Drift Detection** — Monitoring for distribution/covariate/concept drift using KS statistic, PSI, and Jensen-Shannon distance. (Volume 3)
- **Feature Registry** — Metadata catalog where every feature registers name, category, version, dependencies, frequency, owner, quality threshold, unit, and expected range. (Volume 3)
- **Feature Snapshot** — Frozen record of all features, versions, regime, and market report used for a prediction, enabling exact reconstruction. (Volume 5)
- **Feature Store** — The single source of truth for engineered features: an online store (fast lookup for live prediction) and an offline store (historical features for training/backtesting). (Volume 3)
- **Feature Versioning** — Never overwrite a feature (VWAP_v1 → VWAP_v2); models pin required feature versions for reproducibility. (Volume 3)
- **Fragility Score** — Companion to the Robustness Score quantifying how easily a signal breaks under perturbed assumptions. (Volume 5.95)

## H

- **Historical Analog Engine** — Finds the most similar historical market states via cosine similarity, Mahalanobis distance, DTW, and nearest-neighbor search, with subsequent outcome statistics. (Volume 4)
- **Historical Replay Engine** — Reconstructs every feature exactly as it existed at any past timestamp, preventing look-ahead bias. (Volume 3)

## I

- **Information Coefficient (IC)** — Correlation between a feature's predictions and realized outcomes, used to score predictive power. (Volumes 5.5, 9)
- **Infrastructure as Code (IaC)** — Provisioning infrastructure via versioned Terraform/Helm/Ansible so every environment is reproducible. (Volume 10)
- **Institutional Flow Intelligence** — Scores accumulation/distribution from FII, DII, ETF, block/bulk deals, insider transactions, and SAST data. (Volume 4)

## L

- **Learning Engine** — Records every trade lifecycle event (transitions, stop moves, exits, interventions) as training data; failure labels feed weekly retraining. (Volume 6.5)
- **LLM Synthesis Layer** — The LLM component that only explains signals; entries, stops, targets, and sizes always come from deterministic models. (Volumes 1, 7)

## M

- **Macro Pressure Score** — Normalized composite factor from macro inputs like USDINR, DXY, US10Y, crude, and gold. (Volume 2)
- **Market Confidence Engine** — Measures the platform's confidence in its own market assessment from data quality, feature quality, regime certainty, and agreement. (Volume 4)
- **Market Regime** — The current market state evaluated independently across dimensions (trend, volatility, liquidity, participation, macro, structure) rather than one bull/bear label. (Volume 4)
- **Market Shock Library** — Curated templates of historical crises (COVID crash, Fed surprise, budget day) replayed against Digital Twins. (Volume 5.95)
- **Market State Report** — The single structured, timestamped report aggregating all intelligence outputs, consumed by the prediction engine and persisted for replay. (Volume 4)
- **Market Structure Engine** — Institutional analysis engine computing swing structure, liquidity zones, volume profile, order blocks, fair value gaps, and stop-hunt detection instead of retail indicators. (Overview)
- **Meta-Orchestrator** — The "CEO" component coordinating all engines and the only component allowed to approve signals; publishes Decision Objects. (Volume 5.9)
- **Model Tournament** — Head-to-head evaluation where candidate models must prove statistically superior to production before promotion. (Volume 5.5)
- **Model Zoo** — Research repository of competing model types (LightGBM, XGBoost, LSTM, TabNet, etc.) with tracked hyperparameters, cost, and status. (Volume 5.5)
- **Monte Carlo Engine** — Generates 1,000+ independent price/volatility/correlation/liquidity paths to output return distributions and confidence intervals. (Volume 5.95)
- **Multi-Agent Debate** — Panel of specialist reasoning agents (Bull, Bear, Macro, Risk, Options) that critique each trade; advisory only, never overriding the quantitative engine. (Volumes 5.95, 7)
- **Multi-Horizon Prediction** — Probability-only forecasts across 5min/15min/30min/1hr/EOD/next-day horizons. (Volume 5)

## O

- **Observability** — Metrics, logs, traces, events, and profiles via OpenTelemetry, Prometheus, Grafana, Loki, and Jaeger. (Volume 10)
- **OPIE (Opportunity Portfolio Intelligence Engine)** — Ranks each trade candidate against all others, treating user attention as the scarce capital being managed. (Volume 5.75)
- **Opportunity Budget** — Attention-as-capital limits (signals per hour/sector, correlated and duplicate caps) preventing Telegram overload. (Volume 5.75)
- **Opportunity Detection Engine** — Gate that generates candidates only when a significant market condition (breakout, regime transition, liquidity sweep) is present. (Volume 5)
- **Opportunity Knowledge Graph** — Graph of stocks, regimes, features, signals, and outcomes used for conflict detection and explainability. (Volume 5.75)
- **Opportunity Universe** — Daily set of all qualified trade candidates, snapshotted for replay. (Volume 5.75)
- **Options Intelligence Engine** — Derives institutional-grade options features (OI change, max pain, IV skew, gamma/delta exposure) beyond simple PCR. (Volume 2)

## P

- **Permission Engine** — Explicit, audited grants governing what each plugin may do (read/write features, publish events, internet, notifications). (Volume 5.999)
- **Personalization Engine** — Tailors information per user profile (Beginner, Swing Trader, Intraday, Investor, Analyst, Institution). (Volume 8)
- **Platform Health Score** — Single composite score rolled up from Data, Feature, Prediction, Decision, Risk, AI, User, and Infrastructure scores. (Volume 9)
- **Platform Scorecard** — Daily executive report combining ten quality scores into one view. (Volume 9)
- **Plugin Manifest / SDK / Registry / Lifecycle** — The extensibility framework: manifest declares identity and permissions; the SDK is the only stable interface; the registry stores installed plugins; the lifecycle manages install → run → upgrade → uninstall with rollback. (Volume 5.999)
- **Policy Engine** — Enforces configurable, versioned business constraints (outage bans, event blackouts, concentration limits, alert caps). (Volume 5.9)
- **Position Sizing Engine** — Volatility-adjusted sizing supporting fixed fractional, ATR risk, bounded Kelly fraction, expected shortfall, and risk parity. (Volume 6)
- **Probability Calibration** — Correction of overconfident raw ML probabilities via Platt scaling, isotonic regression, or temperature scaling. (Volume 5)
- **Promotion Pipeline** — Staged path from candidate through offline validation, walk-forward, shadow mode, paper trading, and mandatory human approval to production. (Volume 5.5)
- **Prompt Orchestrator** — Manages versioned, A/B-testable prompt templates for LLM tasks. (Volume 7)

## R

- **Regime Transition Engine** — Detects shifts between regimes: transition probability, speed, confidence loss, and instability alerts. (Volume 4)
- **Replay Center** — Reconstructs the exact platform state (market state, decisions, messages, reports) at any historical moment. (Volume 8)
- **Risk Budget Engine** — Daily/weekly/sector/correlation/volatility/tail budgets that shrink dynamically after losses or elevated market risk. (Volume 6)
- **Risk Consensus Engine** — Combines strategic, tactical, simulation, and decision-score inputs into a consensus risk score with voting, conflict resolution, and escalation. (Volume 6.1)
- **Risk Weight Learning** — Adaptive adjustment of consensus voting weights based on historical trade outcomes. (Volume 6.1)
- **Robustness Score** — Composite of scenario, Monte Carlo, stress, gap, liquidity, sensitivity, and replay results; signals below threshold are rejected regardless of conviction. (Volume 5.95)

## S

- **Scenario Generator** — Produces hundreds of realistic future scenarios (gaps, volatility spikes, flash crashes, stop hunts), each with an assigned probability. (Volume 5.95)
- **Security Sandbox** — Isolation layer restricting plugin filesystem, network, database, secrets, memory, and CPU. (Volume 5.999)
- **Service Mesh** — mTLS, retries, circuit breakers, canary releases, and distributed tracing between microservices. (Volume 10)
- **Shadow Mode** — Candidate models evaluate silently alongside production; promotion only after statistical superiority is shown. (Volume 9)
- **Signal Package** — Structured JSON payload: instrument, direction, entry/stop/target, risk-reward, confidence, grade, regime, reason codes, expiration. Never natural language. (Volume 5.99)
- **Signal Quality Certification** — Final audit across data, feature, prediction, simulation, execution, and communication quality: Certified / Rejected / Needs Review / Research Only. (Volume 5.99)
- **Signal Readiness Engine** — Final gate producing a readiness score and grade: Ready, Not Ready, Research Only, or Watchlist. (Volume 5.99)
- **Signal Scheduling Engine** — Assigns each signal Immediate / Delayed / Wait-Confirmation / Discard / Batch / Expiration. (Volume 5.75)
- **SOEFE (Signal Orchestration & Execution Feasibility Engine)** — The "Product Manager" layer deciding whether, when, and how a signal reaches a human trader. (Volume 5.99)
- **Standard Output Schema** — Common collector event structure (timestamp, source, normalized value, confidence, quality score) making downstream components source-agnostic. (Volume 2)
- **Stop Loss Intelligence** — Generates multiple stop candidates (ATR, swing low, liquidity zone, structure break, time) scored on expected loss, hit probability, and gap survivability. (Volume 6)
- **Strategic Risk Engine** — CIO-style engine scoring macro/regime/event/tail risk: *should we participate at all?* (Volume 6.1)

## T

- **Tactical Risk Engine** — Head-Trader-style engine scoring execution risk (spread, slippage, liquidity, stop placement, gap risk): *can we execute safely?* (Volume 6.1)
- **Telegram Delivery Contract** — Formal, versioned schema for signal payloads replacing free-text messages, including follow-up IDs and expiration. (Volume 5.99)
- **Telegram Feed Optimizer** — Final pre-delivery selector maximizing information value and minimizing redundancy, with Conservative → Institutional modes. (Volume 5.75)
- **Thesis Validation Engine** — Continuously checks whether the original trade thesis remains valid during the trade's life. (Volume 6.5)
- **Trade Blueprint** — The permanent object bundling direction, entry, scaling plan, size, stop, targets, risk, management plan, execution plan, failure modes, and simulation summary. (Volume 6)
- **Trade Health** — Continuously recalculated composite score (health, momentum, stability, confidence, quality) for each active trade. (Volume 6.5)
- **Trade Manager / Trade State Engine** — Manages every trade as a living state machine across 15 states (Created → Closed) with persisted transitions and reasons. (Volume 6.5)
- **Trade Qualification Engine** — Final filter rejecting high-conviction trades on liquidity, spread, event risk, model disagreement, or data-quality grounds, with explicit reasons. (Volume 5)
- **Trade Workspace** — Interactive per-signal view: blueprint, simulations, regime, risk, analogs, live health, AI explanation, timeline. (Volume 8)
- **Triple Barrier Labeling** — Outcome labeling using dynamic profit/stop/time barriers (extended with gap, trailing, event, liquidity barriers): Win / Loss / Timeout / Partial. (Volume 5)

## U–W

- **Universal Evaluation Engine** — Common evaluation contract (inputs, outputs, expected vs actual, confidence, latency, version) exposed by every module. (Volume 9)
- **Walk-Forward Validation** — Time-aware validation (rolling/expanding windows, purged K-fold, embargo) preventing optimization on future data. (Volume 5.5)
- **Workspace API** — The unified API layer through which all clients access platform intelligence. (Volume 8)
