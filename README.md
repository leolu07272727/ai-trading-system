# AI-Powered Trading System

A fully automated, thesis-driven trading system built on Interactive Brokers, running 24/7 on a remote VPS with real-time monitoring via Telegram.

## Architecture

```
Market Data → Signal Engine → LLM Overlay (AI Judge) → Execution → IBKR
                                                           ↓
                                              Telegram Bot ← Monitoring
```

### V1: Rule-Based Quantitative System
- **Signal generation** (`strategy.py`) — Multi-factor screening with technical and fundamental filters
- **AI Judge overlay** (`llm_overlay.py`) — LLM-based second opinion on trade candidates, acting as a risk-aware gatekeeper
- **Execution pipeline** (`cycle.py` → `trade.py`) — Automated order placement with stop-loss management via IBKR API
- **EOD risk management** (`force_close.py`) — Automated end-of-day position closing with orphan order cleanup
- **Performance tracking** (`compute_perf.py`, `compute_experiment.py`) — Rigorous benchmarking against SPY buy-and-hold

### V2: Thesis-Driven Investment System
- **Thesis engine** (`thesis_monitor/`) — Monitors investment theses with predefined entry/exit criteria and falsification conditions
- **Multi-engine signal generation** — Combines fundamental thesis validation with market data
- **Virtual portfolio** — Paper trading with isolated P&L tracking against SPY benchmark

### Infrastructure
- **Telegram bot** (`telegram_bot.py`) — Full command interface: `/positions`, `/pnl`, `/status`, `/summary`, real-time trade notifications
- **Cron-driven automation** — Scheduled market scans, portfolio snapshots, daily reports, log rotation
- **Safety systems** — Paper-trading lock, risk parameter guardrails, financial report blackout periods, connection health monitoring

## Key Design Decisions

- **Evaluation-driven development**: System was rigorously backtested against SPY. When V1 showed no edge, the conclusion was accepted and the system pivoted to V2 thesis-driven approach rather than overfitting
- **Experiment discipline**: Pre-registered evaluation criteria (sample size, benchmark, kill conditions) — no moving goalposts
- **Defense in depth**: Multiple safety layers prevent accidental real-money trading, orphan orders, and unmanaged risk

## Tech Stack

Python · Interactive Brokers API (`ib_insync`) · LLM APIs (Claude/GPT) · Telegram Bot API · Linux/VPS · Cron · yfinance

## Setup

1. Clone and create virtual environment
2. Copy `.env.example` to `.env` and configure:
   - IBKR Gateway connection (paper trading port 4002)
   - Telegram bot token and chat ID
   - LLM API keys
3. Configure risk parameters in `config/rules.json`
4. Run with `python cycle.py` (single cycle) or set up cron for automation

> **Note**: This system is designed for paper trading and research purposes. The codebase enforces paper-trading mode by default.
