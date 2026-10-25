# Stock Confirmation and Board Trend Scoring

The production ruleset is now `production_shared_v11_stock_confirmation_board_trend_score`.
The production default in `config/risk_control.yaml` is:

```yaml
entry_sector_mode: trend_score
sector_trend_score_weight: 5.0
```

Stock price/VWAP, breakout, volume progress/local expansion, structure protection,
strategy mode restrictions, freshness, tradability, deadlines and account risk gates
remain in force. Sector direction, breadth and missing board evidence do not veto an
otherwise confirmed stock. The `sector_confirmed` field remains the actual observation,
not a synthetic `true` used to bypass an old gate.

## Scoring

Only the board's own archived/collected index quotes are used. Three directional
components are equally weighted: relative to previous close, relative to real session
open, and relative to an observation at least three minutes earlier. Each contributes
-1/0/+1; their mean times the configured weight gives a bounded adjustment. Multiple
eligible mapped boards are equally averaged, not cherry-picked for the strongest one.
This is an initial configurable heuristic, not a calibrated probability or profit claim.

Receipts must be from the same day and board plan, not synthetic or in the future.
The latest receipt may be no more than 20 seconds old. The three-minute baseline may
precede its target by no more than 20 seconds; intervening gaps may not exceed 20 seconds.
Open/previous-close references must agree throughout the window, which cannot cross lunch.
Missing, stale, discontinuous or unmapped evidence is explicitly unassessed with zero
adjustment. The board cache retains 200 seconds for the 180-second comparison plus its
boundary observation; stock snapshot hot retention stays at 180 seconds.

The adjustment is applied once to candidate strength and live observation ranking.
It is not a new minimum score required for the formal stock signal. Existing portfolio
ranking/rotation rules still consume the resulting candidate strength.

## Identity and Acquisition

THS and TDX are different sector systems. Name matches are only mapping candidates.
Preparation compares actual constituent sets before 09:25; only a unique best match
with Jaccard overlap at least 0.8 is eligible for this explicitly labelled TDX proxy
score. It is not represented as a true THS index or as formal breadth confirmation.
Missing mapping evidence is neutral. Low-overlap/ambiguous comparisons remain research data.

The preopen preparation CLI automatically prepares board mappings for new trend-score
plans. A board preparation failure is optional and does not cancel the stock plan.
The independent collector enables its existing 10-second board lane, fetching only
verified mapped codes. New stock plans collect snapshots/minutes only for the saved
two-pool union; full constituent streaming is no longer scheduled for these plans.
Strategy readers use storage only, never network providers or intraday metadata discovery.

## Replay and Backfill

New backfill jobs read saved pool membership, then fetch the pool/possible-held stock
minutes. They do not expand the full constituent universe. Existing board index archives
are captured read-only and replayed with an observation-time cutoff. Missing historical
board archives are neutral factor warnings, not fabricated from current quotes, member
averages, or a closing snapshot. Required stock evidence still blocks a strict seal.

Old saved risk dictionaries without the new mode retain `breadth_required`; old low-level
confirmation policies also retain that default. This preserves the explicit legacy research
contract. New workflow collection freezes the current risk policy as a new version;
previous sealed results cannot be silently resumed under changed code. Readiness reports
do not fail the new contract solely because breadth/optional board scores are absent.

Deployment requires updated code and a restart of the Web/strategy/collector services.
Existing immutable daily collection plans are not rewritten intraday. New acquisition
scope and mappings take effect with the next preopen plan. This change does not deploy,
send real notifications, change positions or rewrite historical pool artifacts by itself.
