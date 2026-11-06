# funding-rate-scanner

A read-only scanner for **cross-venue perpetual funding-rate arbitrage**. It pulls live funding for every linear perpetual on several exchanges, pairs the same asset across venues, prices each pair after realistic execution costs, and forecasts how much of today's funding spread is likely to survive the holding period.

It uses public market data only: no API keys, no orders.

```
$ python -m funding_scanner
Fetching funding rates from 5 venues...
  okx            ok: 528 perps in 4.3s
  gate           ok: 1080 perps in 16.9s
  bitget         ok: 884 perps in 6.7s
  hyperliquid    ok: 179 perps in 9.3s
  kucoinfutures  ok: 712 perps in 4.4s
3383 perps, 1252 assets, 991 cross-venue pairs above 10% APR; checking order books for top 40...
Dropped 16 of 40: 11 thin-book, 5 ticker-mismatch? (thin-book = visible depth can't fill $10,000)
Analysing 7-day funding history for top 15...

 #  Asset    Long (APR, next)   Short (APR, next)     Net APR  Cost bp  Basis bp  B/E h  Snap 72h bp  Pred 72h bp  7d mean  %pos  Half-life h  Flags
--  -------  -----------------  --------------------  -------  -------  --------  -----  -----------  -----------  -------  ----  -----------  -----------------------
 1  NG       Gate 711% 8h/28m   OKX 1,095% 8h/28m        384%      102      -111     23          214           73     -24%   14%           42  spread-usually-negative
 2  BZ       OKX -444% 4h/28m   Gate -189% 8h/28m        254%       28       -21     10          181           61       9%   32%           23  spread-usually-negative
 3  BZ       OKX -444% 4h/28m   Bitget -234% 4h/~28m     210%       26       -12     11          146           18      15%   60%           10
 4  S        HypL -179% 1h/28m  Bitget 11% 4h/~28m       190%       68        13     31           88          -50      20%   71%            1
 5  S        HypL -179% 1h/28m  KuCoin 11% 4h/28m        190%      100        26     46           56          -79      16%   70%            3
 6  UVXY     OKX -212% 8h/28m   Gate 0% 8h/28m           212%      132       -24     55           42          -96      31%   43%            4  spread-usually-negative
 7  MELANIA  HypL -226% 1h/28m  Bitget 11% 4h/~28m       236%      109         2     40           85          -97      11%   40%            1  spread-usually-negative
 8  GMT      Gate -253% 8h/28m  Bitget -54% 4h/~28m      199%      142       -28     62           22         -103      27%   74%            6
 ...
```

*(Excerpt of a live run on 6 Nov 2026; the full output is in [docs/sample_run.txt](docs/sample_run.txt). Columns are explained below.)*

The point of the output is in the last columns. On a snapshot, MELANIA (row 7) looks like +85 bps over 72 hours. But its funding spread has historically decayed with a half-life of about 1 hour and averaged only 11% APR, so the forecast is −97 bps: **a loss once costs are paid**. Separating opportunities that last from ones that disappear before covering their costs is the core of the problem.

## How it works

1. **Collect.** `fetch_funding_rates` runs on each venue in parallel through [ccxt](https://github.com/ccxt/ccxt). Only active linear USDT/USDC-settled perpetuals are kept.
2. **Normalise.**
   - Venues pay funding every 1h, 4h or 8h, so every rate is converted to an *hourly* rate. The interval comes from the venue's response, then the market metadata (Bitget's `fundInterval`), then a per-venue default.
   - Multiplied contracts (`1000PEPE`, Hyperliquid's `kPEPE`) are mapped to the underlying asset, and prices are compared per underlying unit.
3. **Pair.** Every cross-venue pair of the same asset is considered, oriented to **short where funding is higher and long where it is lower**. Net carry per hour is `short.hourly − long.hourly`, which also handles negative funding correctly.
4. **Shortlist** by carry over the horizon minus taker fees, so order-book calls are only spent on plausible pairs.
5. **Execution costs.** For each leg the scanner walks the live order book for the configured notional, accounting for contract sizes.
   - **Cost** = taker fees to open and close both legs + 2 × entry slippage (exit assumed similar).
   - **Basis** = the short venue's price premium over the long venue. It isn't counted as profit, but it's shown because it reverses on exit.
   - A gap above 5% almost always means two different tokens share a ticker, so those pairs are dropped (`ticker-mismatch?`).
6. **History and forecast.**
   - For the best candidates it pulls 7 days of funding history on both legs. Each settled payment is spread evenly across the hours it covered, using the actual gap between settlements so interval changes are handled.
   - From the hourly spread series it computes the mean, the % of hours the spread was positive, and an **AR(1) persistence coefficient** φ (half-life = ln 0.5 / ln φ).
   - Expected carry over the horizon is `Σₖ μ + (s₀ − μ)·φᵏ`: today's spread decaying toward its historical mean at the rate it actually has.
7. **Rank** by forecast net return (forecast carry − costs). Output goes to the console plus CSV and JSON in `out/`.

## Columns

| Column | Meaning |
|---|---|
| Long / Short | Venue, funding APR, interval, and time to the next payment (`~` = estimated from the interval because the venue didn't report it) |
| Net APR | Funding spread annualised, before costs |
| Cost bp | Round-trip fees + slippage for the configured notional per leg |
| Basis bp | Short-venue price premium at entry |
| B/E h | Hours of current carry needed to cover costs |
| Snap Nh bp | Net return over the horizon if today's spread persisted |
| Pred Nh bp | Net return over the horizon under the AR(1) decay forecast |
| 7d mean / %pos / Half-life h | Historical spread level, sign stability and persistence |

## Usage

```bash
pip install -r requirements.txt
python -m funding_scanner                                   # defaults: 5 venues, $10k per leg, 72h horizon
python -m funding_scanner --notional 50000 --horizon 24 --min-apr 20
python -m funding_scanner --venues okx,bitget,hyperliquid,binance,bybit
python -m pytest -q                                         # 23 offline tests, no network needed
```

Supported venues: OKX, Gate, Bitget, Hyperliquid and KuCoin by default. Binance and Bybit are also supported but return HTTP 451/403 from some regions, so they are opt-in. Adding a venue usually means only adding its id to `venues.py`, plus a fallback interval or fee if its metadata is incomplete.

## Limitations (deliberately out of scope for this prototype)

- **Snapshot, not a backtest.** The forecast is a baseline fitted on 7 days. The next step is walk-forward evaluation on stored history: does Pred beat Snap out of sample, and after costs?
- **Taker-only costs at base fee tiers.** VIP tiers, maker entries, and DEX gas or builder fees aren't modelled.
- **No capital model yet.** Returns are per unit of notional on one leg. Real return on capital depends on margin on both legs, leverage, liquidation buffers, and the cost and delay of moving collateral between venues.
- **Visible book depth only.** The book is read once, from the top 50 levels (KuCoin 100). Hidden liquidity and queue dynamics aren't modelled.
- **Mixed settlement currencies.** USDT-margined and USDC-margined legs are treated as equivalent, which ignores depeg risk.
- **Sizing and exits.** There's no position sizing or exit logic yet. The cost and forecast functions are written so they can be reused for both.

## Layout

```
funding_scanner/
  venues.py    venue adapters: funding pull, interval/fee/contract normalisation
  scanner.py   pairing, order-book and history enrichment, ranking
  costs.py     order-book VWAP and slippage
  history.py   hourly funding series, spread statistics, AR(1) forecast
  models.py    FundingQuote / Opportunity / SpreadStats
  report.py    console table, CSV/JSON export
tests/test_core.py
```
