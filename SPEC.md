# XAUUSD live scan and position-management specification

## 1. Purpose and evidence boundary

Replicate the MT5 scan workflow used in this conversation as a deterministic decision-support engine. Read current broker data, assess existing exposure first, evaluate seven timeframes, and output one actionable decision with structural invalidation.

This conversation demonstrates an analysis workflow, not a validated profitable strategy. Scans were requested intermittently; continuous tick monitoring, economic-calendar screening, backtesting, and automatic execution were not performed. Account balance changes alone do not establish strategy performance: the user placed trades between scans, changed exposure, and traded a demo account.

The percentages previously reported were subjective estimates, not measured win probabilities. Do not train a model on those numbers as outcome labels.

## 2. Architecture and scope

Use six small components in an existing system:

1. MT5 data and authenticated execution adapter.
2. Snapshot validation and normalization.
3. Structure/setup evaluator.
4. Portfolio risk and position-management evaluator.
5. Snapshot-bound selectable-provider authorization and automatic entry controller.
6. Decision formatter and audit log.

Keep execution separate from signal generation. A market entry may be submitted only after the deterministic BUY/SELL, Gemini cross-check, and blind Gemini analysis agree for one snapshot and the same setup remains actionable on the latest clean snapshot. An advisory output must never be represented as executed without a verified broker result.

## 3. Broker discovery and account validation

Read `account_info()`, `terminal_info()`, `symbols_get()`, `positions_get()`, and `orders_get()` at the start of each scan.

- Identify gold against USD using symbol name, description, base currency, and profit currency. Never hard-code a suffix without verifying it.
- Verify expected account identity, server, demo/live mode, and account currency. A mismatch blocks new entries until resolved.
- Read contract size, tick size, tick values, minimum volume, volume step, maximum volume, stop/freeze distances, permitted order modes, and filling modes.
- Calculate monetary outcomes with `order_calc_profit()` and margin with `order_calc_margin()`; do not infer cash risk from displayed lot size.
- A null result is an error, not an empty position list.
- Aggregate all gold positions and pending orders. Keep BUY and SELL exposures separate; flag an unexpected hedge.

Observed examples, not portable defaults:

| Field | First account | Later account |
|---|---|---|
| Server | JustMarkets-Demo2 | JustMarkets-Demo3 |
| Currency | USD | USC |
| Symbol | XAUUSD.m | XAUUSD.c |
| Contract size | 100 | 1 |
| Minimum lot / step | 0.01 / 0.01 | 0.01 / 0.01 |

All monetary output must include its currency. Do not label USC amounts as USD.

## 4. Coherent market snapshots

Read at least 160 bars for each timeframe, plus current bid/ask and available recent ticks. The original scans used approximately 100–160 bars, so this is a replication baseline rather than an optimized lookback.

Snapshot fields:

```text
snapshot_id, retrieval_start_utc, retrieval_end_utc
account_identity, account_currency, broker_server
tick_time_raw, tick_time_normalized, bid, ask, spread
M1, M5, M15, M30, H1, H4, D1 bars
positions, pending_orders, symbol_specifications
data_quality_flags, news_status
```

Validation:

- Require a connected terminal, positive bid/ask, ask >= bid, and sufficient bars.
- Check monotonic timestamps, OHLC consistency, and that the newest bar corresponds to the latest quote interval.
- Separate the forming candle from completed candles. A live price crossing is not a completed-candle confirmation.
- Re-read after history synchronization or a new-bar boundary. One scan in this conversation initially returned outdated lower-timeframe history, which a second read corrected.
- Broker timestamps showed an apparent three-hour offset from the PC UTC clock. Verify clock conventions explicitly; do not silently apply an assumed offset or call future-dated ticks fresh.
- Compare successive tick timestamps and PC elapsed time to detect stalled feeds. Flag unresolved timestamp normalization.
- If prices move materially during collection, refresh the snapshot before publishing an actionable entry.
- Missing/stale/inconsistent data => WAIT for new entries. Existing positions still require an explicit data-quality alert.

Configure freshness, spread, and synchronization tolerances per feed after measuring its normal behavior. These thresholds are deployment parameters, not values established by this conversation.

## 5. Timeframe responsibilities

| Timeframe | Responsibility |
|---|---|
| D1 | Macro context and major structural boundaries; never an entry trigger |
| H4 | Major swing structure and broad directional context |
| H1 | Intraday recovery/decline, major intraday obstacles |
| M30 | Setup context, range boundaries, opposing structure |
| M15 | Setup development, displacement, momentum deterioration |
| M5 | Completed-candle confirmation, entry structure, failed reclaim |
| M1 | Execution timing, local rejection, acceleration and exhaustion |

Perfect alignment is unnecessary. For example, an H1 recovery inside bearish H4 or D1 structure can support an intraday long when M5 provides explicit reversal evidence and its invalidation and available room justify it. D1 never authorizes an entry; conflicting D1 context requires stronger M5 reversal evidence rather than universal alignment.

## 6. Structure calculations

### Swings

Replication baseline: a pivot high is the maximum high in a five-completed-bar window; a pivot low is its minimum low. Confirm the pivot only after two subsequent bars close. Resolve equal-price ties consistently, and merge nearby duplicate levels using a configured tick/volatility tolerance.

Never use a pivot before its confirmation time in backtests. Classify successive meaningful swings as higher highs/higher lows, lower highs/lower lows, or mixed/ranging. Ignore changes smaller than the configured structural tolerance.

### ATR

The scans used a simple mean of the last 14 completed true ranges:

```text
TR = max(high-low, abs(high-previous_close), abs(low-previous_close))
ATR14 = mean(last 14 completed TR values)
```

This is not Wilder smoothing. Preserve the distinction when reproducing results.

### Levels and price behavior

Track confirmed swing levels, repeated rejection zones, previous session/day highs and lows when available, and the nearest opposing structure in either direction.

For deterministic implementation, configure and validate:

- Break: completed close beyond a level by a spread/volatility-aware buffer.
- Acceptance: completed closes or sustained trading beyond the level, without immediate reclaim.
- Failed reclaim: price returns to a broken level, rejects, and closes back on the breakout side.
- Sweep: price trades beyond a prior extreme, then closes back inside; reversal requires subsequent structure confirmation.
- Displacement: body and range expansion relative to recent completed candles, with a close near the directional extreme.
- Exhaustion: reduced progress, overlapping candles, opposing wicks, or strong opposite displacement near a target.

Exact numerical cutoffs were not established in the conversation. Treat them as research parameters and freeze them before out-of-sample testing.

## 7. Setup state machine

```text
NO_SETUP -> DETECTED -> WATCH_LEVEL / WATCH_ZONE -> CONFIRMING -> ARMED_LIMIT -> ACTIONABLE
                         \-> INVALIDATED / EXPIRED              \-> INVALIDATED / EXPIRED
ACTIONABLE -> EXPIRED / INVALIDATED
```

Each setup has direction, originating timeframe, watched level/zone, confirmation evidence, invalidation, expiry conditions, and nearest targets.

`ARMED_LIMIT` identifies a bounded retest plan. After the selected authorization mode approves the exact immutable plan, submit a native expiring MT5 `BUY_LIMIT` or `SELL_LIMIT` with its SL and TP attached. Both zones use bid-chart prices; translate the BUY limit by the observed spread because MT5 triggers it on ask. Before placement, repeat live structure, spread, exposure, account, sizing, margin, broker-distance and duplicate checks. If price is already inside the zone, use the separately validated market-order path. Record that a native pending fill cannot repeat application-level spread or balance checks at its future broker fill tick.

Research rule version 9 fixes the BUY bid/ask zone mismatch and sends the complete entry plan to the MT5 bridge. At submission the bridge validates account identity, all discovered gold exposure, quote age/spread, plan expiry, completed M5 reclaims, SL/TP geometry, broker distances, cash sizing, margin and the correct side for a native limit. A market-entry quote change during broker precheck cancels submission for reevaluation. LLM approval is conditional on the exact plan and expires after at most 300 seconds by default; it can authorize an ARMED_LIMIT for native pending placement or carry to ACTIONABLE for an immediate market order. WATCH/WAIT without directional READY authorization does not approve a plan. Plan or provider/mode changes invalidate approval before submission.

Research rule version 10 supervises every Tradew-owned native pending order on each scan. It revalidates the immutable submitted plan against fresh broker account, order, quote and completed-M5 data, and removes the order when the structure breaks, a completed M5 candle reclaims the confirmation level, the plan expires, its price/volume/SL/TP changes, or conflicting gold exposure appears. Ticket, magic, symbol, direction and setup-reference ownership must all match before cancellation; manual or untracked orders are never modified. Replay applies the same completed-M5 and structural cancellation boundary.

Research rule version 11 separates Independent Auto Entry from deterministic authorization. The deterministic engine provides only a low-cost material-event trigger: a qualifying candidate, completed M5 displacement, or proximity to a strong mapped zone. After a global cooldown, the selected provider receives the bounded blind candle/level context and must return either WAIT or a complete native BUY_LIMIT/SELL_LIMIT plan. The executable entry, stop, target, confirmation level and structural invalidation must be directionally coherent and grounded in prices present in that blind dataset. Fresh live quote, account, exposure, expiry, broker-distance, sizing, margin, and duplicate checks remain deterministic and mandatory. Consensus mode retains deterministic-plan agreement semantics.

Research rule version 12 makes setup lifecycle terminal after invalidation, expiry, or closure; suppresses repeated minimum-volume submission attempts for the same immutable setup; and normalizes requested pending expiration to the broker's observed one-minute precision before recording the supervised contract. Replay entries retain setup family, heuristic score, candle evidence, session, volatility regime, and H4/H1/M30/M15 trend context. A retrospective operational audit and one 1,000-M5-bar replay did not establish a profitable technical edge, so no candle family, score, session, or reward/risk filter is promoted from that in-sample evidence. Changes to the trading technique require prospective or held-out confirmation.

The production replay path now runs this same evaluator on six historical timeframes at M1 opens, including compound balance sizing and enabled automatic trailing/break-even behavior. It never exposes later completed candles. Its output explicitly labels M1 sampling and current-contract approximation limits and deterministic counterfactual authorization; historical LLM decisions are not fabricated. The legacy M5-only helper is diagnostic only.

Supported setup families:

1. Breakout with follow-through.
2. Breakout followed by a rejected retest.
3. Pullback continuation with a confirmed lower high/higher low.
4. Liquidity sweep followed by reversal confirmation.

Do not chase a move simply because the earlier trigger worked. A confirmed setup outside its bounded entry zone remains armed until price retests the zone, the setup expires, or completed structure invalidates it.

Research rule version 23 retains objective completed-candle engulfing, rejection, two/three-soldier or crow, displacement, order-block, liquidity, and confirmed multi-timeframe supply/demand evidence. These labels are fixed calculations rather than LLM chart interpretation. Entry cash risk is sized from the configured fraction of current account balance. Runtime authorization may use deterministic-only, blind-independent, or full-consensus mode. A broker-valid new entry is split on hedging accounts into a 65% TP1 child and 35% no-TP runner while preserving total volume and initial risk; unsplittable minimum volumes remain a single TP1 child. The runner may receive only risk-reducing stop changes before TP1 and becomes eligible for confirmed-M5 structural trailing after TP1 is verified from quote or broker history. Automatic stop management never widens risk. Cash, margin, broker-distance, fresh-data, authorization, and duplicate-setup limits remain mandatory.

Research rule version 24 removes the realized daily-loss entry brake. A prior loss total from the current UTC day no longer blocks an otherwise valid new submission. Post-loss cooldown, consecutive-loss, session-entry, exposure, margin, broker-distance, fresh-data, authorization, structural-validity, and duplicate-setup controls remain active. Production replay follows the same rule.

Research rule version 30 makes zone hierarchy and execution eligibility explicit. A
local M5 pattern is deferred as `WATCH_ZONE` when an active, already-confirmed M15+
entry zone lies beyond the local thesis invalidation; the old pattern is never
silently promoted into that different thesis. A fresh completed M5 rejection and
M1 confirmation are required at the controlling zone. When the setup already
overlaps its controlling zone, protection is placed beyond the full zone plus the
largest of the strategy buffer, twice the spread, or 0.25 M5 ATR. Each ladder layer
must pass pending-side, protected-stop-distance, and banked-TP1 reward/risk
preflight before `ARMED_LIMIT`; rejected layer risk is not redistributed. MT5
repeats exact cash, margin, distance, and order checks. Replay consumes the same
version-30 evaluator and eligible layer set. `reviewed` authorization sends the
exact deterministic plan to one informed LLM and requires same-snapshot `AGREE`;
it does not run or trade a blind independent plan.

Research rule version 31 adds a bounded momentum-continuation path without
weakening the existing retest path. A breakout-follow-through or pullback-
continuation setup may arm a shallow native limit at the completed displacement
candle's broken level/body only when the M5 candle is complete and higher-timeframe
context remains aligned. Other setup families still require a completed M1 retest
and rejection, including broker-side reproduction. Momentum TP1 discovery uses an
active opposing M5-H1 zone first, then an unbroken confirmed M5-H1 pivot, and only
when neither exists a fixed M5-ATR projection. The blind independent provider may
propose the exact completed-M5 candle open or body midpoint under the same aligned-
displacement rule. Deterministic validation reproduces entry, target, stop,
reachability, freshness, and broker geometry before submission. Replay invokes the
same version-31 evaluator and layer preflight as live deterministic execution.

### Example from the session

```text
4309.82 support fails
-> attempted recovery rejects near 4310.03
-> bearish continuation becomes plausible
-> price approaches 4303 support: fresh-entry quality falls
-> later break below 4302.49 reverses above 4305
-> immediate short thesis invalidates
```

These are historical examples, not reusable trading levels.

## 8. Entry eligibility and risk

An entry is actionable only when all of the following hold:

- Data and account checks pass.
- The setup has current confirmation, not an old trigger.
- A structural stop exists outside normal local noise.
- There is sufficient room before opposing structure after spread and expected costs.
- Position size fits the account's explicit risk budget, margin limits, and aggregate exposure cap.
- No existing position or pending order creates unintended duplication or opposition.

Place the stop beyond thesis invalidation with a buffer for spread and local volatility. For shorts, account for ask-side stop triggering when analyzing bid-based charts. Do not tighten a stop solely to manufacture an attractive risk/reward ratio.

Choose TP1/TP2 from actual structural objectives. Report reward/risk separately for each target, calculated at executable prices. Include commissions and a slippage allowance where known. If costs are unavailable, label calculations incomplete.

Sizing procedure:

```text
risk_budget = account_balance * balance_risk_fraction
loss_per_lot = abs(order_calc_profit(direction, symbol, 1 lot, entry, stop))
raw_volume = risk_budget / (loss_per_lot + estimated_cost_per_lot)
volume = floor_to_broker_step(raw_volume)
```

Validate the resulting volume with broker limits and calculation functions. If below minimum lot, WAIT; never round upward past the risk budget. Evaluate all positions together, including correlated gold exposure and pending orders. No averaging down, martingale, or loss-based size increases.

The active configuration sets `balance_risk_fraction` to `0.3`. Broker volume is rounded down, so actual calculated stop risk may be slightly below but never above 30% of the latest account balance.

## 9. Existing-position management takes priority

Inspect positions before considering new entries. Report count, direction, total lots, weighted entry, floating P/L, actual SL/TP coverage, and monetary sensitivity to a $1 gold move.

Rule version 25 has one narrow exception: setup discovery and new submission may continue when every existing gold position is a Tradew-owned runner and its broker stop is already at or beyond that position's own entry price. There must be no pending gold order. TP1, legacy, manual/foreign, wrong-magic, and unprotected exposure continues to block new entries. Immediately before submission, require the protected-runner tickets, direction, volume, entry, and stop to match the initial preflight snapshot exactly.

For independent-auto plans in rule version 26, require the blind LLM analysis to select exactly one explicit risk tier for READY output: LOW = 10%, MEDIUM = 20%, or HIGH = 30% of current account balance. WATCH and NO_SETUP must use no tier. Missing, invalid, or mismatched tiers reject the plan; never silently default to LOW. Convert the label to its fixed fraction in trusted application code, bind the label, reason, and fraction into the immutable entry plan, and revalidate that mapping before broker submission. The percentage is a maximum setup stop-risk budget; volume-step rounding and an intentionally reduced execution profile may use less.

Decision priority:

1. Failed thesis or unacceptable portfolio exposure: recommend CLOSE or a concretely sized PARTIAL CLOSE.
2. Missing protection: recommend a structural SL; if that stop exceeds the risk budget, reduce exposure or close rather than conceal the risk with an arbitrary tight stop.
3. Favorable structure persists: WAIT / hold existing position with its specified protection.
4. New favorable structure permits less risk: MOVE SL or TRAIL SL.
5. Break-even only after sufficient favorable structure makes the original risk unnecessary.

Partial-close output must specify exact volume/tickets or a deterministic allocation rule, remaining exposure, and remaining risk. A vague instruction to reduce exposure is insufficient for implementation.

Never widen the stop to avoid acknowledging invalidation. A previous bearish view does not justify holding after a failed breakdown. A closed short does not automatically justify a new long.

## 10. Confidence reporting

Use separate BUY and SELL setup-quality scores; neither is a safety percentage and they need not sum to 100.

For initial deployment, use an explicitly labeled heuristic score based on structure, confirmation, momentum, available room, execution cost, and data quality. Publish component contributions and version the scoring formula. Do not invent precise probabilities.

To publish a win probability, first define the event (for example, target hit before stop within a stated horizon), collect out-of-sample outcomes, and calibrate separately by setup and market regime. Report sample size and calibration uncertainty. Until then label percentages: **heuristic setup score; not win probability**.

## 11. Reassessment triggers

Refresh positions and risk when account exposure changes. Reassess execution conditions on fresh ticks, M1 timing on M1 closes, and setup confirmation on M5 closes. Refresh higher-timeframe structure on their closes; monitor intrabar invalidation without treating forming bars as completed confirmations.

Also reassess immediately after a structural break/reclaim, abnormal spread, large displacement, feed interruption, or known high-impact event. Use adaptive polling within measured MT5 performance limits; never claim continuous monitoring unless the process is actually running and its heartbeat is observable.

If a reliable calendar is connected, record event source/time and apply a validated event-risk policy. Otherwise report `news_status: unavailable`; price behavior does not identify the cause of volatility.

## 12. Output contract

Allowed primary actions: BUY, SELL, WAIT, CLOSE, PARTIAL CLOSE, MOVE SL, MOVE SL TO BE, TRAIL SL.

```text
symbol, account_mode, snapshot_time, session
action, setup_id, setup_state
bid, ask, spread
buy_setup_score, sell_setup_score, score_method
entry, stop, targets, volume, cash_risk, risk_currency, reward_risk
positions_summary, proposed_management_changes
reasons[maximum 3], invalidation, next_trigger
data_quality, news_status, execution_status
```

For WAIT, omit executable entry fields and state the exact next trigger. For management, distinguish actual stops from proposed stops. Use `execution_status: awaiting_authorization` before entry execution and report `submitted` only from a verified broker response. Management outputs other than verified automatic trailing/break-even results remain advisory.

## 13. Validation before relying on the system

- Replay historical bars with pivot confirmation delays and no future information.
- Use bid/ask data and realistic spread, commission, slippage, and stop execution assumptions.
- Test session changes, high volatility, feed gaps, history synchronization, account switches, cent currencies, and sudden position additions.
- Evaluate entries and management rules separately, then together.
- Use walk-forward/out-of-sample testing; log rejected setups as well as accepted ones.
- Measure net expectancy, drawdown, tail loss, exposure, execution error rates, and score calibration—not just win rate.
- Compare against simpler baselines and paper-trade prospectively before drawing conclusions about an edge.

Keep an append-only decision log containing the exact input snapshot, rule version, scores, proposed action, and subsequent outcome. This makes the workflow reproducible and prevents hindsight from rewriting the original decision.
