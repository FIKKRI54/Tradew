# Tradew

Tradew is an experimental XAUUSD scanner and MT5 execution controller. It combines a deterministic multi-timeframe strategy with optional single-provider LLM review, native limit orders, pending-order supervision, staged exits, and a responsive dashboard.

> Research software only. The included profile is non-executable and is not evidence of profitability. Test with a demo account and validate every parameter against your broker before enabling execution.

## How it works

- Collects completed M1, M5, M15, M30, H1, H4, and D1 candles from MT5.
- Maps active supply/demand, support/resistance, pivots, order blocks, liquidity sweeps, and candle confirmations.
- Creates deterministic market or layered limit plans and validates spread, freshness, exposure, margin, stop geometry, and reachable entry distance.
- Supports both confirmed retest limits and a bounded completed-M5 momentum-pullback limit; the latter still requires aligned context and protected native-order geometry.
- Optionally asks one configured provider—Codex CLI or a Gemini browser worker—for an informed review or an independent plan.
- Submits only through the authenticated MT5 bridge when the selected authorization mode and all deterministic broker checks pass.
- Supervises owned pending orders and manages TP1/runner positions without widening stops.

The available automatic entry modes are:

- `deterministic`: uses only the built-in rules.
- `reviewed`: an informed LLM may veto the exact deterministic plan; it does not
  create a separate blind plan.
- `independent`: an LLM proposes a complete blind plan; deterministic code validates it before submission.
- `consensus`: the deterministic plan must match both an informed review and a
  separate blind independent read.

## Project layout

- `scan_engine.py`, `setups.py`, `entry_plan.py`: deterministic analysis and plan construction.
- `auto_execution.py`, `mt5_connector.py`, `mt5_bridge.py`: validation and broker submission.
- `automation.py`: continuous scan, review, execution, and supervision loop.
- `position_review.py`, `exit_management.py`: stop, TP1, runner, and partial-close management.
- `llm_advisors.py`, `independent_validation.py`: bounded LLM input and deterministic output validation.
- `replay_engine.py`: conservative historical replay using the live strategy path.
- `api_server.py`, `frontend/`: dashboard and local API.
- `tests/`: offline unit and integration-style tests.
- `SPEC.md`: original behavioral specification and design constraints.

## Local setup

Requirements:

- Windows with MetaTrader 5 for direct broker access, or a configured remote MT5 bridge.
- A Python version supported by the installed `MetaTrader5` package.
- Node.js and npm for the dashboard.

```powershell
python -m venv venv
.\venv\Scripts\python.exe -m pip install -r requirements.txt
npm.cmd --prefix frontend ci
Copy-Item scan_config.example.json scan_config.json
Copy-Item llm_config.example.json llm_config.json
Copy-Item .env.example .env
```

Edit the copied files for your own terminal and providers. Private configuration files are ignored by Git. The example profile has `automatic_execution: false`, no account binding, disabled LLM providers, and a conservative placeholder risk value.

Start the complete development application:

```powershell
npm run dev
```

Build and verify:

```powershell
npm test
npm run lint
npm run build
```

## Configuration

`scan_config.json` binds execution to the intended login, server, account mode, currency, and symbol. Keep `automatic_execution` disabled until those values and all broker units have been verified. A mismatch blocks submission.

`llm_config.json` selects exactly one provider. Models receive bounded candles and structural levels but no MT5 credentials or order tool. Their output is schema-checked and then subjected to current deterministic account, quote, exposure, risk, and broker validation.

Environment variables belong in `.env` or the service environment. The important names are documented in `.env.example`. Never commit bridge tokens, API keys, browser profiles, client certificates, account exports, or live configuration.

## Runtime data

Logs, JSONL journals, SQLite databases, replay results, push subscriptions, audit archives, and release bundles are intentionally ignored. Set `TRADEW_DATA_ROOT` to keep runtime state outside the source tree in production.

Important audit files include decisions, LLM reviews, execution attempts, pending-order supervision, setup outcomes, trailing changes, and manual portfolio actions. They are runtime evidence, not source files.

## Deployment notes

The `deploy/` directory contains reusable service and installation templates. Replace user paths, domain names, certificate locations, and bridge settings for your host. Keep the API and MT5 bridge on loopback or another authenticated private channel. If the dashboard is exposed remotely, require TLS and client authentication or an equivalent device-level access control.

The Gemini browser worker depends on a separately authenticated browser profile. That profile and its cookies must never be added to this repository. Codex CLI authentication is likewise local to the machine running the provider.

## Safety and limitations

- Passing tests proves software behavior, not trading performance.
- LLM output can be late, inconsistent, or wrong; it never bypasses deterministic execution checks.
- Historical replay cannot reproduce future liquidity, spread, slippage, gaps, or broker behavior.
- Native pending orders can fill while the application is offline; their broker-side SL and expiry are essential.
- High account-risk fractions can produce rapid drawdown or account loss even on demo accounts.
- The manual `Close 50%` operation can include non-Tradew positions and therefore requires automation to be paused plus an exact-state confirmation.

## License

No license has been granted yet. Add a license before redistributing or accepting contributions.
