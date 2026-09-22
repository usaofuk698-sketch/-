//+------------------------------------------------------------------+
//|                                                       123_M5.mq5  |
//|      One bot, five philosophies -- combines every EA in this     |
//|      project into a single dispatcher with one shared position.  |
//|                        XAUUSD - M5 - built for Exness MT5        |
//+------------------------------------------------------------------+
//| WHAT THIS IS
//| This is not a sixth, independently invented strategy. It is a dispatcher
//| over the five that already exist and are already shipped as their own
//| EAs: XauRetest (breakout, then a proven retest), XauBreak (breakout,
//| immediate entry, no retest wait), XauTrend (trades an established
//| trend's pullback), Anas (fades a stretched, exhausted move back to the
//| mean) and XauWyck (fades a failed probe of a volume-profile value area).
//| Every bar this EA tries them, IN THIS ORDER, and takes the first signal
//| it gets:
//|
//|   1. RT  breakout + retest             (InpEnableRetest)
//|   1b. RT immediate-breakout sub-trigger (InpRtUseImmediateBreakout, OFF)
//|   2. BK  breakout momentum, no retest  (InpEnableBreakout)
//|   3. TR  trend pullback                (InpEnableTrend)
//|   4. MR  mean-reversion scalp          (InpEnableMeanReversion)
//|   5. WK  volume-profile Wyckoff fade   (InpEnableWyckoff)
//|
//| This order is a defensible tie-break for the rare bar where more than
//| one philosophy's conditions line up at once -- most-confirmed first --
//| not a claim that any one of them is "better". Exactly one position is
//| ever open at a time (HasOpenPosition() below, same rule as every EA in
//| this project): whichever sub-strategy fires first takes the ONE trade
//| this bar is allowed to have, and the others simply do not get to act.
//|
//| WHICH SUB-STRATEGY OWNS AN OPEN TRADE
//| Each sub-strategy's own break-even/trailing rules differ on purpose --
//| e.g. breakout momentum deliberately does NOT move its stop early (see
//| its own section below), while the others do. OpenTrade() below tags
//| every position's comment with which sub-strategy opened it ("123:RT",
//| "123:BK", "123:TR", "123:MR", "123:WK"), and ManageOpenPosition() reads
//| that tag back to hand management to the correct sub-strategy's own
//| rules -- this survives an EA or terminal restart because the tag lives
//| in the position's comment on the server, not in EA memory.
//|
//| WHAT IS NEW HERE, AND WHAT IS NOT
//| Nothing about any single sub-strategy's edge is new -- every entry
//| condition and every stop/target formula below is ported unchanged from
//| that strategy's own EA. What IS new, and therefore UNVALIDATED, is
//| running all five side by side and letting whichever fires first take
//| the trade: their filters were tuned in isolation, not for this
//| combination, and interaction effects (e.g. one sub-strategy's cooldown
//| freeing up a bar right when another's setup completes) have not been
//| checked against real data. Treat this exactly like XauRetest's own
//| opt-in immediate-breakout trigger: real Strategy Tester validation
//| before real money, not a promotion in trust just because five familiar
//| names are inside it.
//|
//| SESSION HOURS ARE OPEN BY DEFAULT
//| XauRetest's hard-won 8-10/12-16 GMT windows are proven for THAT
//| strategy's own filters, not for this five-way combination, so they are
//| NOT reused here as a default. Window 1 below defaults to the full day
//| (00-24 GMT); windows 2 and 3 are off. Narrow these once a real backtest
//| shows which hours this specific combination should trade -- re-imposing
//| unproven hours here would repeat the same mistake as widening them
//| without evidence, just in the other direction.
//|
//| Same invariants as every other EA here: closed bars only (bar 0 is
//| still forming and is never read), stops are only ever tightened, and
//| nothing broker-specific is hardcoded.
//+------------------------------------------------------------------+
#property copyright "123"
#property link      ""
#property version   "1.00"

#include <Trade\Trade.mqh>
#include <Trade\PositionInfo.mqh>

CTrade        trade;
CPositionInfo posInfo;

//--- enums ---------------------------------------------------------
enum ENUM_TZ_MODE
  {
   TZ_AUTO   = 0,  // Detect the server's GMT offset automatically
   TZ_MANUAL = 1   // Use the ServerGmtOffset input below
  };

//+------------------------------------------------------------------+
//| INPUTS                                                           |
//+------------------------------------------------------------------+
input group "=== Risk (read this section first, applies to every sub-strategy) ==="
input double InpRiskPercent        = 0.5;    // Risk per trade (% of equity)
input double InpMaxDailyLossPct    = 3.0;    // Daily loss limit (%). 0 = OFF
input int    InpMaxTradesPerDay    = 10;     // Max trades per day, ACROSS all sub-strategies. 0 = unlimited
input int    InpMaxConsecLosses    = 6;      // Cool off after N losses in a row. 0 = OFF
input double InpMinStopDistance    = 0.30;   // Refuse stops tighter than this (price units)
input double InpMaxStopDistance    = 30.0;   // Refuse stops wider than this (price units)
input double InpFixedLots          = 0.0;    // >0 overrides risk sizing (NOT recommended)

input group "=== Session (hours are GMT/UTC, not server time) ==="
input ENUM_TZ_MODE InpTzMode       = TZ_AUTO; // How to resolve server time -> GMT
input int    InpServerGmtOffset    = 0;      // Server GMT offset when TZ_MANUAL
// Open all day by default -- see the file header. Set a window's Start ==
// End to disable it, same convention as XauRetest.
input int    InpSession1Start      = 0;      // Window 1: OPEN by default (whole day)
input int    InpSession1End        = 24;
input int    InpSession2Start      = 0;      // Window 2: off by default
input int    InpSession2End        = 0;
input int    InpSession3Start      = 0;      // Window 3: off by default
input int    InpSession3End        = 0;
input int    InpNoNewTradesAfter   = 24;     // No new entries from this GMT hour (24 = off)
input int    InpFlatByHour         = 24;     // Force flat at this GMT hour (24 = never)
input bool   InpTradeMonday        = true;
input bool   InpTradeTuesday       = true;
input bool   InpTradeWednesday     = true;
input bool   InpTradeThursday      = true;
input bool   InpTradeFriday        = true;

input group "=== Execution ==="
input long   InpMagicNumber        = 770644; // Identifies this EA's own trades.
                                             // MUST differ from XauTrend (770577), Anas
                                             // (770588), XauBreak (770599), XauMicro
                                             // (770611), XauWyck (770622) and XauRetest
                                             // (770633): each EA manages only positions
                                             // with its own magic, else a shared number
                                             // would have each bot closing the others'
                                             // trades.
input int    InpMaxSpreadPoints    = 500;    // Skip entries above this spread, IN POINTS (as MT5 shows it)
input int    InpSlippagePoints     = 30;     // Max deviation on market orders
input int    InpMaxBarsInTrade     = 96;     // Close a trade older than this (0 = off). Generous enough for BK's far target.

input group "=== Which sub-strategies may fire (all ON: this bot exists to combine them) ==="
input bool   InpEnableRetest         = true;   // 1. breakout + retest (most confirmed)
input bool   InpEnableBreakout       = true;   // 2. breakout momentum, no retest wait
input bool   InpEnableTrend          = true;   // 3. trend pullback
input bool   InpEnableMeanReversion  = true;   // 4. mean-reversion scalp
input bool   InpEnableWyckoff        = true;   // 5. volume-profile Wyckoff fade

//===================================================================
// RT -- breakout + retest (XauRetest)
//===================================================================
input group "=== RT: breakout + retest -- the level being broken ==="
input int    InpRtRangeLookback      = 20;
input double InpRtBreakMarginAtr     = 0.10;
input double InpRtMinBarRangeAtr     = 0.50;
input int    InpRtAtrPeriod          = 14;

input group "=== RT: regime ==="
input int    InpRtRegimeLookback     = 288;
input double InpRtAtrMinMult         = 0.55;
input double InpRtAtrMaxMult         = 3.00;
input int    InpRtAdxPeriod          = 14;
input double InpRtAdxMin             = 15.0;
input int    InpRtHtfEmaPeriod       = 100;
input bool   InpRtRequireHtfAlign    = true;

input group "=== RT: the retest ==="
input int    InpRtRetestMaxBars      = 12;
input double InpRtRetestToleranceAtr = 0.15;
input double InpRtRetestClosePosMin  = 0.68;
input int    InpRtCooldownBars       = 12;

input group "=== RT: stop and target ==="
input double InpRtStopAtrBuffer      = 0.20;
input double InpRtMinStopAtrMult     = 1.00;
input double InpRtMinTargetSpreadRatio = 6.0;

input group "=== RT: quality score (scales size and target, never the entry decision) ==="
input double InpRtQualityMinRR       = 1.50;
input double InpRtQualityMaxRR       = 3.00;
input bool   InpRtUseMeasuredMove    = true;
input double InpRtQualityMaxRiskMult = 1.50;

input group "=== RT: optional second trigger -- immediate breakout (OFF by default) ==="
input bool   InpRtUseImmediateBreakout   = false;
input double InpRtImmBreakoutMinStrength = 0.60;
input double InpRtImmBreakoutRR          = 2.00;
input double InpRtImmBreakoutStopAtrMult = 1.20;

input group "=== RT: in-trade management ==="
input bool   InpRtUseBreakeven       = true;
input double InpRtBreakevenAtR       = 1.0;
input double InpRtBreakevenOffAtr    = 0.05;
input bool   InpRtUseTrail           = true;
input double InpRtTrailAtR           = 1.5;
input double InpRtTrailAtrMult       = 1.20;

//===================================================================
// BK -- breakout momentum, no retest wait (XauBreak)
//===================================================================
input group "=== BK: breakout momentum -- the range being broken ==="
input int    InpBkRangeLookback      = 12;
input double InpBkSqueezeMaxAtr      = 4.00;
input double InpBkBreakMarginAtr     = 0.05;

input group "=== BK: impulse quality ==="
input int    InpBkAtrPeriod          = 14;
input double InpBkMinBarRangeAtr     = 0.60;
input double InpBkClosePositionMin   = 0.60;

input group "=== BK: regime ==="
input int    InpBkRegimeLookback     = 288;
input double InpBkAtrMinMult         = 0.55;
input double InpBkAtrMaxMult         = 3.00;
input int    InpBkAdxPeriod          = 14;
input double InpBkAdxMin             = 0.0;
input int    InpBkTrendEmaPeriod     = 100;
input bool   InpBkRequireTrendAlign  = false;

input group "=== BK: stop and target (tight risk, distant reward -- the whole point) ==="
input double InpBkStopAtrBuffer      = 0.20;
input double InpBkMinStopAtrMult     = 1.00;
input double InpBkRewardRisk         = 4.00;
input double InpBkMinTargetSpreadRatio = 8.0;

input group "=== BK: in-trade management (see the break-even trap note below) ==="
// InpBkUseBreakeven defaults OFF on purpose: at BK's low hit rate the maths
// only works if winners reach the full target, and an early break-even
// stop converts a large share of them into scratches. The time stop
// (InpMaxBarsInTrade) is the safety valve instead.
input bool   InpBkUseBreakeven       = false;
input double InpBkBreakevenAtR       = 2.0;
input double InpBkBreakevenOffAtr    = 0.05;

//===================================================================
// TR -- trend pullback (XauTrend)
//===================================================================
input group "=== TR: trend pullback -- direction ==="
input int    InpTrEmaFast            = 21;
input int    InpTrEmaSlow            = 55;
input int    InpTrEmaTrend           = 200;

input group "=== TR: filters ==="
input int    InpTrAdxPeriod          = 14;
input double InpTrAdxMin             = 22.0;
input int    InpTrAtrPeriod          = 14;
input int    InpTrRegimeLookback     = 288;
input double InpTrAtrMinMult         = 0.80;
input double InpTrAtrMaxMult         = 2.50;

input group "=== TR: pullback trigger ==="
input int    InpTrRsiPeriod          = 14;
input double InpTrRsiPullbackLong    = 45.0;
input double InpTrRsiPullbackShort   = 55.0;
input int    InpTrPullbackLookback   = 8;

input group "=== TR: stop and target ==="
input int    InpTrSwingLookback      = 12;
input double InpTrStopAtrBuffer      = 0.35;
input double InpTrMinStopAtrMult     = 1.00;
input double InpTrRewardRisk         = 1.8;

input group "=== TR: in-trade management ==="
input bool   InpTrUseBreakeven       = true;
input double InpTrBreakevenAtR       = 1.0;
input double InpTrBreakevenOffAtr    = 0.10;
input bool   InpTrUseTrail           = true;
input double InpTrTrailAtR           = 1.5;
input double InpTrTrailAtrMult       = 1.5;

//===================================================================
// MR -- mean-reversion scalp (Anas)
//===================================================================
input group "=== MR: mean-reversion scalp -- the mean ==="
input int    InpMrEmaPeriod          = 20;

input group "=== MR: regime guard (ABOVE AdxMax = trending; do NOT fade it) ==="
input int    InpMrAdxPeriod          = 14;
input double InpMrAdxMax             = 50.0;
input int    InpMrAtrPeriod          = 14;
input int    InpMrRegimeLookback     = 288;
input double InpMrAtrMinMult         = 0.55;
input double InpMrAtrMaxMult         = 3.00;

input group "=== MR: stretch trigger ==="
input double InpMrStretchAtr         = 0.60;
input int    InpMrRsiPeriod          = 7;
input double InpMrRsiOversold        = 38.0;
input double InpMrRsiOverbought      = 62.0;

input group "=== MR: stop and target ==="
input int    InpMrSwingLookback      = 6;
input double InpMrStopAtrBuffer      = 0.25;
input double InpMrMinStopAtrMult     = 1.30;
input double InpMrRewardRisk         = 1.20;
input double InpMrMinTargetSpreadRatio = 5.0;

input group "=== MR: entry quality ==="
input int    InpMrHtfEmaPeriod       = 100;
input int    InpMrHtfSlopeLookback   = 20;
input double InpMrHtfMaxSlopeAtr     = 0.06;
input double InpMrClosePositionMin   = 0.55;
input bool   InpMrRequireDivergence  = false;
input int    InpMrDivergenceLookback = 12;

input group "=== MR: in-trade management ==="
input bool   InpMrUseBreakeven       = true;
input double InpMrBreakevenAtR       = 0.8;
input double InpMrBreakevenOffAtr    = 0.05;

//===================================================================
// WK -- volume-profile Wyckoff fade (XauWyck)
//===================================================================
input group "=== WK: volume profile ==="
input int    InpWkProfileLookback    = 96;
input int    InpWkProfileBins        = 40;
input double InpWkValueAreaPct       = 0.70;
input int    InpWkProfileUpdateBars  = 12;

input group "=== WK: the failed probe ==="
input double InpWkProbeDepthAtr      = 0.10;
input double InpWkCloseBackMarginAtr = 0.05;
input double InpWkProbeVolumeMax     = 1.40;
input double InpWkClosePositionMin   = 0.55;

input group "=== WK: regime (AdxMax is a CEILING: do not fade a real trend) ==="
input int    InpWkAtrPeriod          = 14;
input int    InpWkRegimeLookback     = 288;
input double InpWkAtrMinMult         = 0.55;
input double InpWkAtrMaxMult         = 3.00;
input int    InpWkAdxPeriod          = 14;
input double InpWkAdxMax             = 45.0;

input group "=== WK: stop and target ==="
input double InpWkStopAtrBuffer      = 0.30;
input double InpWkMinStopAtrMult     = 1.00;
input double InpWkRewardRisk         = 2.20;
input bool   InpWkTargetPoc          = true;
input double InpWkMinTargetSpreadRatio = 6.0;

input group "=== WK: in-trade management ==="
input bool   InpWkUseBreakeven       = false;
input double InpWkBreakevenAtR       = 1.5;
input double InpWkBreakevenOffAtr    = 0.05;

input group "=== Display ==="
input bool   InpShowPanel          = true;

//+------------------------------------------------------------------+
//| GLOBALS                                                          |
//+------------------------------------------------------------------+
// -- indicator handles, one set per sub-strategy so each can be configured
// -- independently even though most default to the same periods.
int hRtAtr = INVALID_HANDLE, hRtAdx = INVALID_HANDLE, hRtHtfEma = INVALID_HANDLE;
int hBkAtr = INVALID_HANDLE, hBkAdx = INVALID_HANDLE, hBkTrend = INVALID_HANDLE;
int hTrEmaFast = INVALID_HANDLE, hTrEmaSlow = INVALID_HANDLE, hTrEmaTrend = INVALID_HANDLE;
int hTrAtr = INVALID_HANDLE, hTrAdx = INVALID_HANDLE, hTrRsi = INVALID_HANDLE;
int hMrEma = INVALID_HANDLE, hMrHtfEma = INVALID_HANDLE;
int hMrAtr = INVALID_HANDLE, hMrAdx = INVALID_HANDLE, hMrRsi = INVALID_HANDLE;
int hWkAtr = INVALID_HANDLE, hWkAdx = INVALID_HANDLE;

datetime g_lastBarTime   = 0;
int      g_gmtOffsetHrs  = 0;

// --- per-day state (shared across all sub-strategies)
int      g_dayOfYear     = -1;
double   g_dayStartEquity= 0.0;
int      g_tradesToday   = 0;
double   g_dayRealisedPnl = 0.0;
int      g_consecLosses  = 0;
bool     g_halted        = false;
string   g_haltReason    = "";

// --- RT pending-breakout state (see XauRetest_M5.mq5's own header for the
// full rationale: advanced unconditionally every closed bar, TryEntryRt()
// reads the SNAPSHOT taken before that bar's own advance).
double   g_rtPendingLevel    = 0.0;
int      g_rtPendingSide     = 0;
int      g_rtPendingAge      = 0;
double   g_rtPendingStrength = 0.0;
double   g_rtPendingWidth    = 0.0;
double   g_rtRetestLevel     = 0.0;
int      g_rtRetestSide      = 0;
double   g_rtRetestStrength  = 0.0;
double   g_rtRetestWidth     = 0.0;
datetime g_rtLastFireBarTime = 0;
int      g_rtImmSide         = 0;
double   g_rtImmStrength     = 0.0;

// --- WK cached profile (rebuilt on schedule, exactly like XauWyck_M5.mq5)
double   g_wkPoc = 0.0, g_wkVah = 0.0, g_wkVal = 0.0;
int      g_wkBarsSinceProfile = 999999;

// --- symbol spec, resolved once in OnInit
double   g_maxSpreadPrice = 0.0;
double   g_tickSize      = 0.0;
double   g_tickValue     = 0.0;
double   g_volMin        = 0.0;
double   g_volMax        = 0.0;
double   g_volStep       = 0.0;
int      g_stopsLevelPts = 0;
int      g_freezeLevelPts= 0;

string   g_status        = "starting";

//+------------------------------------------------------------------+
//| INIT                                                             |
//+------------------------------------------------------------------+
int OnInit()
  {
   if(!ResolveSymbolSpec())
      return(INIT_FAILED);

   if(!ValidateInputs())
      return(INIT_FAILED);

   if(Period() != PERIOD_M5)
      PrintFormat("WARNING: this EA was designed and tested on M5; the chart is %s. "
                  "Every period input is counted in BARS, so they mean different "
                  "lengths of time on another timeframe.", EnumToString((ENUM_TIMEFRAMES)Period()));

   hRtAtr    = iATR(_Symbol, PERIOD_CURRENT, InpRtAtrPeriod);
   hRtAdx    = iADX(_Symbol, PERIOD_CURRENT, InpRtAdxPeriod);
   hRtHtfEma = iMA(_Symbol, PERIOD_CURRENT, InpRtHtfEmaPeriod, 0, MODE_EMA, PRICE_CLOSE);

   hBkAtr    = iATR(_Symbol, PERIOD_CURRENT, InpBkAtrPeriod);
   hBkAdx    = iADX(_Symbol, PERIOD_CURRENT, InpBkAdxPeriod);
   hBkTrend  = iMA(_Symbol, PERIOD_CURRENT, InpBkTrendEmaPeriod, 0, MODE_EMA, PRICE_CLOSE);

   hTrEmaFast  = iMA(_Symbol, PERIOD_CURRENT, InpTrEmaFast,  0, MODE_EMA, PRICE_CLOSE);
   hTrEmaSlow  = iMA(_Symbol, PERIOD_CURRENT, InpTrEmaSlow,  0, MODE_EMA, PRICE_CLOSE);
   hTrEmaTrend = iMA(_Symbol, PERIOD_CURRENT, InpTrEmaTrend, 0, MODE_EMA, PRICE_CLOSE);
   hTrAtr      = iATR(_Symbol, PERIOD_CURRENT, InpTrAtrPeriod);
   hTrAdx      = iADX(_Symbol, PERIOD_CURRENT, InpTrAdxPeriod);
   hTrRsi      = iRSI(_Symbol, PERIOD_CURRENT, InpTrRsiPeriod, PRICE_CLOSE);

   hMrEma    = iMA(_Symbol, PERIOD_CURRENT, InpMrEmaPeriod,    0, MODE_EMA, PRICE_CLOSE);
   hMrHtfEma = iMA(_Symbol, PERIOD_CURRENT, InpMrHtfEmaPeriod, 0, MODE_EMA, PRICE_CLOSE);
   hMrAtr    = iATR(_Symbol, PERIOD_CURRENT, InpMrAtrPeriod);
   hMrAdx    = iADX(_Symbol, PERIOD_CURRENT, InpMrAdxPeriod);
   hMrRsi    = iRSI(_Symbol, PERIOD_CURRENT, InpMrRsiPeriod, PRICE_CLOSE);

   hWkAtr    = iATR(_Symbol, PERIOD_CURRENT, InpWkAtrPeriod);
   hWkAdx    = iADX(_Symbol, PERIOD_CURRENT, InpWkAdxPeriod);

   if(hRtAtr==INVALID_HANDLE || hRtAdx==INVALID_HANDLE || hRtHtfEma==INVALID_HANDLE ||
      hBkAtr==INVALID_HANDLE || hBkAdx==INVALID_HANDLE || hBkTrend==INVALID_HANDLE ||
      hTrEmaFast==INVALID_HANDLE || hTrEmaSlow==INVALID_HANDLE || hTrEmaTrend==INVALID_HANDLE ||
      hTrAtr==INVALID_HANDLE || hTrAdx==INVALID_HANDLE || hTrRsi==INVALID_HANDLE ||
      hMrEma==INVALID_HANDLE || hMrHtfEma==INVALID_HANDLE || hMrAtr==INVALID_HANDLE ||
      hMrAdx==INVALID_HANDLE || hMrRsi==INVALID_HANDLE ||
      hWkAtr==INVALID_HANDLE || hWkAdx==INVALID_HANDLE)
     {
      Print("ERROR: failed to create one or more indicator handles.");
      return(INIT_FAILED);
     }

   trade.SetExpertMagicNumber(InpMagicNumber);
   trade.SetDeviationInPoints(InpSlippagePoints);
   trade.SetTypeFillingBySymbol(_Symbol);

   g_gmtOffsetHrs = ResolveGmtOffset();
   ResetDailyState(true);

   PrintFormat("123 M5 started on %s | server GMT offset %+d h | "
               "tick %.5f, tick value %.5f, lots %.2f-%.2f step %.2f | stops level %d pts",
               _Symbol, g_gmtOffsetHrs, g_tickSize, g_tickValue,
               g_volMin, g_volMax, g_volStep, g_stopsLevelPts);
   PrintFormat("Sub-strategies: RT=%s BK=%s TR=%s MR=%s WK=%s (RT immediate trigger=%s)",
               (InpEnableRetest ? "on" : "off"), (InpEnableBreakout ? "on" : "off"),
               (InpEnableTrend ? "on" : "off"), (InpEnableMeanReversion ? "on" : "off"),
               (InpEnableWyckoff ? "on" : "off"), (InpRtUseImmediateBreakout ? "on" : "off"));
   PrintSessionWindow("Window 1", InpSession1Start, InpSession1End);
   PrintSessionWindow("Window 2", InpSession2Start, InpSession2End);
   PrintSessionWindow("Window 3", InpSession3Start, InpSession3End);

   double pointSize = SymbolInfoDouble(_Symbol, SYMBOL_POINT);
   g_maxSpreadPrice = InpMaxSpreadPoints * pointSize;
   PrintFormat("Max spread: %d points = %.2f USD/oz  (symbol digits %d, 1 point = %.5f)",
               InpMaxSpreadPoints, g_maxSpreadPrice, (int)_Digits, pointSize);
   if(g_maxSpreadPrice >= 3.0)
      PrintFormat("WARNING: a %.2f USD/oz spread limit is very permissive. Typical XAUUSD "
                  "spread is 0.15-0.50. At this setting the filter will almost never block "
                  "a trade, so you will pay whatever spread the broker quotes, including "
                  "the rollover blowout.", g_maxSpreadPrice);
   if(g_maxSpreadPrice <= 0.0)
      Print("ERROR: max spread resolved to zero. Every entry will be blocked.");

   if(InpMaxDailyLossPct <= 0.0 || InpMaxConsecLosses <= 0 || InpMaxTradesPerDay <= 0)
      PrintFormat("RISK GUARDS DISABLED -> daily loss: %s | loss streak: %s | trades/day: %s. "
                  "Nothing will stop this EA inside a losing day except the per-trade stop.",
                  (InpMaxDailyLossPct <= 0.0 ? "OFF" : "on"),
                  (InpMaxConsecLosses <= 0   ? "OFF" : "on"),
                  (InpMaxTradesPerDay <= 0   ? "OFF" : "on"));

   Print("UNVALIDATED COMBINATION: each sub-strategy above ships on its own with its own "
         "evidence trail (breakout+retest aside, none of it real-money evidence gathered "
         "in this project). Running all five side by side is new on top of that. Backtest "
         "this specific configuration before risking real money -- see the file header.");

   return(INIT_SUCCEEDED);
  }

//+------------------------------------------------------------------+
void OnDeinit(const int reason)
  {
   IndicatorRelease(hRtAtr); IndicatorRelease(hRtAdx); IndicatorRelease(hRtHtfEma);
   IndicatorRelease(hBkAtr); IndicatorRelease(hBkAdx); IndicatorRelease(hBkTrend);
   IndicatorRelease(hTrEmaFast); IndicatorRelease(hTrEmaSlow); IndicatorRelease(hTrEmaTrend);
   IndicatorRelease(hTrAtr); IndicatorRelease(hTrAdx); IndicatorRelease(hTrRsi);
   IndicatorRelease(hMrEma); IndicatorRelease(hMrHtfEma);
   IndicatorRelease(hMrAtr); IndicatorRelease(hMrAdx); IndicatorRelease(hMrRsi);
   IndicatorRelease(hWkAtr); IndicatorRelease(hWkAdx);
   Comment("");
  }

//+------------------------------------------------------------------+
//| Resolve everything broker-specific from the symbol itself.       |
//+------------------------------------------------------------------+
bool ResolveSymbolSpec()
  {
   if(!SymbolInfoInteger(_Symbol, SYMBOL_SELECT))
     {
      if(!SymbolSelect(_Symbol, true))
        {
         PrintFormat("ERROR: symbol %s is not available in Market Watch.", _Symbol);
         return(false);
        }
     }

   g_tickSize       = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_SIZE);
   g_tickValue      = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_VALUE);
   g_volMin         = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
   g_volMax         = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX);
   g_volStep        = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);
   g_stopsLevelPts  = (int)SymbolInfoInteger(_Symbol, SYMBOL_TRADE_STOPS_LEVEL);
   g_freezeLevelPts = (int)SymbolInfoInteger(_Symbol, SYMBOL_TRADE_FREEZE_LEVEL);

   if(g_tickSize <= 0.0 || g_tickValue <= 0.0 || g_volStep <= 0.0)
     {
      PrintFormat("ERROR: incomplete symbol spec for %s (tickSize=%.8f tickValue=%.8f step=%.8f). "
                  "Open a chart of this symbol once so the terminal downloads its contract data.",
                  _Symbol, g_tickSize, g_tickValue, g_volStep);
      return(false);
     }
   return(true);
  }

//+------------------------------------------------------------------+
bool ValidateInputs()
  {
   if(InpRiskPercent <= 0.0 || InpRiskPercent > 5.0)
     {
      Print("ERROR: RiskPercent must be in (0, 5]. Above ~2% a normal losing run wipes the account.");
      return(false);
     }
   if(InpMinStopDistance <= 0.0)     { Print("ERROR: MinStopDistance must be > 0"); return(false); }
   if(InpMinStopDistance >= InpMaxStopDistance)
     { Print("ERROR: MinStopDistance must be < MaxStopDistance"); return(false); }

   if(!InpEnableRetest && !InpEnableBreakout && !InpEnableTrend &&
      !InpEnableMeanReversion && !InpEnableWyckoff)
     { Print("ERROR: at least one sub-strategy must be enabled."); return(false); }

   // RT
   if(InpRtQualityMinRR <= 0.0)      { Print("ERROR: RtQualityMinRR must be > 0"); return(false); }
   if(InpRtQualityMaxRR < InpRtQualityMinRR)
     { Print("ERROR: RtQualityMaxRR must be >= RtQualityMinRR"); return(false); }
   if(InpRtQualityMaxRiskMult < 1.0) { Print("ERROR: RtQualityMaxRiskMult must be >= 1.0"); return(false); }
   if(InpRtHtfEmaPeriod < 2)         { Print("ERROR: RtHtfEmaPeriod must be >= 2"); return(false); }
   if(InpRtRangeLookback < 2)        { Print("ERROR: RtRangeLookback must be >= 2"); return(false); }
   if(InpRtRetestMaxBars < 1)        { Print("ERROR: RtRetestMaxBars must be >= 1"); return(false); }
   if(InpRtRetestClosePosMin < 0.0 || InpRtRetestClosePosMin > 1.0)
     { Print("ERROR: RtRetestClosePosMin must be in [0, 1]"); return(false); }
   if(InpRtRegimeLookback < 20)      { Print("ERROR: RtRegimeLookback must be >= 20"); return(false); }
   if(InpRtUseImmediateBreakout)
     {
      if(InpRtImmBreakoutMinStrength < 0.0 || InpRtImmBreakoutMinStrength > 1.0)
        { Print("ERROR: RtImmBreakoutMinStrength must be in [0, 1]"); return(false); }
      if(InpRtImmBreakoutRR <= 0.0)        { Print("ERROR: RtImmBreakoutRR must be > 0"); return(false); }
      if(InpRtImmBreakoutStopAtrMult <= 0.0) { Print("ERROR: RtImmBreakoutStopAtrMult must be > 0"); return(false); }
     }

   // BK
   if(InpBkRewardRisk < 1.5)
     { Print("ERROR: BkRewardRisk below 1.5 defeats this sub-strategy's low hit rate."); return(false); }
   if(InpBkRangeLookback < 2)        { Print("ERROR: BkRangeLookback must be >= 2"); return(false); }
   if(InpBkSqueezeMaxAtr <= 0.0)     { Print("ERROR: BkSqueezeMaxAtr must be > 0"); return(false); }
   if(InpBkTrendEmaPeriod < 2)       { Print("ERROR: BkTrendEmaPeriod must be >= 2"); return(false); }
   if(InpBkClosePositionMin < 0.0 || InpBkClosePositionMin > 1.0)
     { Print("ERROR: BkClosePositionMin must be in [0, 1]"); return(false); }
   if(InpBkRegimeLookback < 20)      { Print("ERROR: BkRegimeLookback must be >= 20"); return(false); }

   // TR
   if(InpTrRewardRisk <= 0.0)        { Print("ERROR: TrRewardRisk must be > 0"); return(false); }
   if(InpTrEmaFast >= InpTrEmaSlow)  { Print("ERROR: TrEmaFast must be < TrEmaSlow"); return(false); }
   if(InpTrRegimeLookback < 20)      { Print("ERROR: TrRegimeLookback must be >= 20"); return(false); }
   if(InpTrSwingLookback < 2)        { Print("ERROR: TrSwingLookback must be >= 2"); return(false); }
   if(InpTrPullbackLookback < 2)     { Print("ERROR: TrPullbackLookback must be >= 2"); return(false); }

   // MR
   if(InpMrRewardRisk <= 0.0)        { Print("ERROR: MrRewardRisk must be > 0"); return(false); }
   if(InpMrEmaPeriod < 2)            { Print("ERROR: MrEmaPeriod must be >= 2"); return(false); }
   if(InpMrStretchAtr <= 0.0)        { Print("ERROR: MrStretchAtr must be > 0"); return(false); }
   if(InpMrRsiOversold >= InpMrRsiOverbought)
     { Print("ERROR: MrRsiOversold must be < MrRsiOverbought"); return(false); }
   if(InpMrHtfEmaPeriod < 2)         { Print("ERROR: MrHtfEmaPeriod must be >= 2"); return(false); }
   if(InpMrHtfSlopeLookback < 1)     { Print("ERROR: MrHtfSlopeLookback must be >= 1"); return(false); }
   if(InpMrClosePositionMin < 0.0 || InpMrClosePositionMin > 1.0)
     { Print("ERROR: MrClosePositionMin must be in [0, 1]"); return(false); }
   if(InpMrDivergenceLookback < 2)   { Print("ERROR: MrDivergenceLookback must be >= 2"); return(false); }
   if(InpMrAdxMax <= 0.0 || InpMrAdxMax > 100.0)
     { Print("ERROR: MrAdxMax must be in (0, 100]"); return(false); }
   if(InpMrRegimeLookback < 20)      { Print("ERROR: MrRegimeLookback must be >= 20"); return(false); }
   if(InpMrSwingLookback < 2)        { Print("ERROR: MrSwingLookback must be >= 2"); return(false); }

   // WK
   if(InpWkProfileLookback < 10)     { Print("ERROR: WkProfileLookback must be >= 10"); return(false); }
   if(InpWkProfileBins < 5)          { Print("ERROR: WkProfileBins must be >= 5"); return(false); }
   if(InpWkValueAreaPct <= 0.0 || InpWkValueAreaPct > 1.0)
     { Print("ERROR: WkValueAreaPct must be in (0, 1]"); return(false); }
   if(InpWkProfileUpdateBars < 1)    { Print("ERROR: WkProfileUpdateBars must be >= 1"); return(false); }
   if(InpWkClosePositionMin < 0.0 || InpWkClosePositionMin > 1.0)
     { Print("ERROR: WkClosePositionMin must be in [0, 1]"); return(false); }
   if(InpWkAdxMax <= 0.0 || InpWkAdxMax > 100.0)
     { Print("ERROR: WkAdxMax must be in (0, 100]"); return(false); }
   if(InpWkRegimeLookback < 20)      { Print("ERROR: WkRegimeLookback must be >= 20"); return(false); }
   if(InpWkRewardRisk <= 0.0)        { Print("ERROR: WkRewardRisk must be > 0"); return(false); }

   return(true);
  }

//+------------------------------------------------------------------+
//| Server clock -> GMT (see XauRetest_M5.mq5 for the full rationale).|
//+------------------------------------------------------------------+
int ResolveGmtOffset()
  {
   if(InpTzMode == TZ_MANUAL)
      return(InpServerGmtOffset);

   datetime srv = TimeCurrent();
   datetime gmt = TimeGMT();
   if(srv <= 0 || gmt <= 0)
     {
      Print("WARNING: could not read server/GMT time; assuming the server is GMT+0. "
            "Set TzMode=TZ_MANUAL and ServerGmtOffset if the session hours look wrong.");
      return(0);
     }
   return((int)MathRound((double)(srv - gmt) / 3600.0));
  }

datetime ToGmt(datetime serverTime)
  {
   long secs = (long)serverTime - (long)g_gmtOffsetHrs * 3600;
   if(secs < 0) secs = 0;
   return((datetime)secs);
  }

int GmtHour(datetime serverTime)
  {
   MqlDateTime t;
   TimeToStruct(ToGmt(serverTime), t);
   return(t.hour);
  }

int GmtDayOfWeek(datetime serverTime)
  {
   MqlDateTime t;
   TimeToStruct(ToGmt(serverTime), t);
   return(t.day_of_week);
  }

void PrintSessionWindow(string label, int start, int end)
  {
   if(start == end)
     {
      PrintFormat("%s: OFF (start == end)", label);
      return;
     }
   PrintFormat("%s: %02d:00-%02d:00 GMT  =  %02d:00-%02d:00 server time",
               label, start, end,
               (start + g_gmtOffsetHrs + 24) % 24,
               (end   + g_gmtOffsetHrs + 24) % 24);
  }

//+------------------------------------------------------------------+
//| MAIN LOOP                                                        |
//+------------------------------------------------------------------+
void OnTick()
  {
   if(!IsNewBar())
     {
      if(InpShowPanel) DrawPanel();
      return;
     }

   RollDailyStateIfNeeded();
   UpdateConsecutiveLossesFromHistory();

   datetime now = TimeCurrent();

   // RT's pending-breakout state must advance UNCONDITIONALLY every closed
   // bar, whether or not a position is open -- same invariant as
   // XauRetest_M5.mq5 itself, and for the same reason: a breakout that
   // occurs while a trade (of ANY sub-strategy) is open must still arm a
   // retest for later.
   g_rtRetestLevel    = g_rtPendingLevel;
   g_rtRetestSide     = g_rtPendingSide;
   g_rtRetestStrength = g_rtPendingStrength;
   g_rtRetestWidth    = g_rtPendingWidth;
   AdvanceRtPendingBreakout();

   if(HasOpenPosition())
     {
      ManageOpenPosition(now);
      if(InpShowPanel) DrawPanel();
      return;
     }

   string why = "";
   if(!MayOpen(now, why))
     {
      g_status = "no entry: " + why;
      if(InpShowPanel) DrawPanel();
      return;
     }

   TryEntry();
   if(InpShowPanel) DrawPanel();
  }

//+------------------------------------------------------------------+
bool IsNewBar()
  {
   datetime t = iTime(_Symbol, PERIOD_CURRENT, 0);
   if(t == 0) return(false);
   if(t == g_lastBarTime) return(false);
   g_lastBarTime = t;
   return(true);
  }

//+------------------------------------------------------------------+
//| DAILY STATE                                                      |
//+------------------------------------------------------------------+
void ResetDailyState(bool firstRun)
  {
   MqlDateTime t;
   TimeToStruct(ToGmt(TimeCurrent()), t);
   g_dayOfYear      = t.day_of_year;
   g_dayStartEquity = AccountInfoDouble(ACCOUNT_EQUITY);
   g_dayRealisedPnl = 0.0;
   g_tradesToday    = 0;
   g_consecLosses   = 0;
   g_halted         = false;
   g_haltReason     = "";
   if(!firstRun)
      PrintFormat("New trading day. Equity %.2f, counters reset.", g_dayStartEquity);
  }

void RollDailyStateIfNeeded()
  {
   MqlDateTime t;
   TimeToStruct(ToGmt(TimeCurrent()), t);
   if(t.day_of_year != g_dayOfYear)
      ResetDailyState(false);
  }

//+------------------------------------------------------------------+
//| Count today's closed trades and the current losing streak from   |
//| deal history, ACROSS ALL SUB-STRATEGIES (same magic number), so  |
//| the daily limits bound the worst case of the combination, not    |
//| any one sub-strategy alone.                                      |
//+------------------------------------------------------------------+
void UpdateConsecutiveLossesFromHistory()
  {
   MqlDateTime t;
   TimeToStruct(TimeCurrent(), t);
   t.hour = 0; t.min = 0; t.sec = 0;
   datetime dayStart = StructToTime(t);

   if(!HistorySelect(dayStart, TimeCurrent() + 60))
      return;

   int    trades   = 0;
   int    streak   = 0;
   double realised = 0.0;
   int    total    = HistoryDealsTotal();

   for(int i = 0; i < total; i++)
     {
      ulong ticket = HistoryDealGetTicket(i);
      if(ticket == 0) continue;
      if(HistoryDealGetString(ticket, DEAL_SYMBOL) != _Symbol) continue;
      if(HistoryDealGetInteger(ticket, DEAL_MAGIC) != InpMagicNumber) continue;
      if(HistoryDealGetInteger(ticket, DEAL_ENTRY) != DEAL_ENTRY_OUT) continue;

      trades++;
      double profit = HistoryDealGetDouble(ticket, DEAL_PROFIT)
                    + HistoryDealGetDouble(ticket, DEAL_SWAP)
                    + HistoryDealGetDouble(ticket, DEAL_COMMISSION);
      realised += profit;
      if(profit < 0.0) streak++;
      else             streak = 0;
     }

   g_tradesToday    = trades;
   g_consecLosses   = streak;
   g_dayRealisedPnl = realised;

   double equity = AccountInfoDouble(ACCOUNT_EQUITY);
   double base   = (equity > 0.0) ? equity : g_dayStartEquity;
   double limit  = -MathAbs(base * InpMaxDailyLossPct / 100.0);
   bool dailyLossActive = (InpMaxDailyLossPct > 0.0);

   if(dailyLossActive && g_dayRealisedPnl <= limit && !g_halted)
     {
      g_halted = true;
      g_haltReason = "daily loss limit";
      PrintFormat("HALT: realised %.2f today, past the %.1f%% limit (%.2f on %.2f equity). "
                  "No more trades today.",
                  g_dayRealisedPnl, InpMaxDailyLossPct, limit, base);
     }
   else if(InpMaxConsecLosses > 0 && g_consecLosses >= InpMaxConsecLosses && !g_halted)
     {
      g_halted = true;
      g_haltReason = "consecutive losses";
      PrintFormat("HALT: %d losses in a row. No more trades today.", g_consecLosses);
     }
  }

//+------------------------------------------------------------------+
//| GATES                                                            |
//+------------------------------------------------------------------+
bool IsTradingDay(int dow)
  {
   switch(dow)
     {
      case 1: return(InpTradeMonday);
      case 2: return(InpTradeTuesday);
      case 3: return(InpTradeWednesday);
      case 4: return(InpTradeThursday);
      case 5: return(InpTradeFriday);
     }
   return(false);
  }

bool WindowContainsHour(int h, int start, int end)
  {
   if(start == end) return(false);
   if(start < end)  return(h >= start && h < end);
   return(h >= start || h < end);
  }

bool InSession(datetime serverTime)
  {
   int h = GmtHour(serverTime);
   return(WindowContainsHour(h, InpSession1Start, InpSession1End)
       || WindowContainsHour(h, InpSession2Start, InpSession2End)
       || WindowContainsHour(h, InpSession3Start, InpSession3End));
  }

double CurrentSpread()
  {
   double ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
   double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);
   return(ask - bid);
  }

bool MayOpen(datetime now, string &reason)
  {
   if(g_halted)                                  { reason = g_haltReason;        return(false); }
   if(!IsTradingDay(GmtDayOfWeek(now)))          { reason = "not a trading day"; return(false); }
   if(!InSession(now))                           { reason = "outside session";   return(false); }
   if(GmtHour(now) >= InpNoNewTradesAfter)       { reason = "late in session";   return(false); }
   if(InpMaxTradesPerDay > 0 && g_tradesToday >= InpMaxTradesPerDay)
      { reason = "max trades/day"; return(false); }
   if(InpMaxConsecLosses > 0 && g_consecLosses >= InpMaxConsecLosses)
      { reason = "loss streak"; return(false); }

   double spread = CurrentSpread();
   if(spread > g_maxSpreadPrice)
     {
      reason = StringFormat("spread %.2f > %.2f", spread, g_maxSpreadPrice);
      return(false);
     }
   if(!TerminalInfoInteger(TERMINAL_TRADE_ALLOWED)) { reason = "AutoTrading off";  return(false); }
   if(!AccountInfoInteger(ACCOUNT_TRADE_EXPERT))    { reason = "EA trading off";   return(false); }
   return(true);
  }

//+------------------------------------------------------------------+
//| POSITION HELPERS                                                 |
//+------------------------------------------------------------------+
bool HasOpenPosition()
  {
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      if(!posInfo.SelectByIndex(i)) continue;
      if(posInfo.Symbol() != _Symbol) continue;
      if(posInfo.Magic() != InpMagicNumber) continue;
      return(true);
     }
   return(false);
  }

bool SelectOwnPosition()
  {
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      if(!posInfo.SelectByIndex(i)) continue;
      if(posInfo.Symbol() != _Symbol) continue;
      if(posInfo.Magic() != InpMagicNumber) continue;
      return(true);
     }
   return(false);
  }

//+------------------------------------------------------------------+
//| INDICATOR READS -- all at shift 1 or older (closed bars only)    |
//+------------------------------------------------------------------+
bool ReadBuffer(int handle, int bufferIndex, int start, int count, double &out[])
  {
   ArraySetAsSeries(out, true);
   int copied = CopyBuffer(handle, bufferIndex, start, count, out);
   return(copied == count);
  }

//--- Median of ATR over the regime window, for a GIVEN handle (each
//--- sub-strategy has its own ATR handle/period).
double MedianAtr(int handle, int lookback)
  {
   double buf[];
   if(!ReadBuffer(handle, 0, 1, lookback, buf))
      return(0.0);

   double sorted[];
   ArrayResize(sorted, lookback);
   ArrayCopy(sorted, buf, 0, 0, lookback);
   ArraySetAsSeries(sorted, false);
   ArraySort(sorted);

   if(lookback % 2 == 1)
      return(sorted[lookback / 2]);
   return(0.5 * (sorted[lookback / 2 - 1] + sorted[lookback / 2]));
  }

//+------------------------------------------------------------------+
//| SIZING (shared by every sub-strategy)                            |
//+------------------------------------------------------------------+
double CalcLots(double entry, double stop, double riskMult = 1.0)
  {
   if(InpFixedLots > 0.0)
      return(NormalizeLots(InpFixedLots));

   double distance = MathAbs(entry - stop);
   if(distance <= 0.0) return(0.0);

   double equity   = AccountInfoDouble(ACCOUNT_EQUITY);
   double riskCash = equity * InpRiskPercent / 100.0 * MathMax(riskMult, 0.0);

   double ticks       = distance / g_tickSize;
   double lossPerLot  = ticks * g_tickValue;
   if(lossPerLot <= 0.0) return(0.0);

   return(NormalizeLots(riskCash / lossPerLot));
  }

double NormalizeLots(double lots)
  {
   if(lots <= 0.0) return(0.0);
   lots = MathFloor(lots / g_volStep) * g_volStep;
   lots = MathMin(lots, g_volMax);
   if(lots < g_volMin) return(0.0);
   return(NormalizeDouble(lots, 2));
  }

double MinStopOffset()
  {
   double point = SymbolInfoDouble(_Symbol, SYMBOL_POINT);
   int    pts   = MathMax(g_stopsLevelPts, g_freezeLevelPts);
   return(pts * point);
  }

//+------------------------------------------------------------------+
//| Shared order placement. tag identifies which sub-strategy owns   |
//| this trade (embedded in the position comment: "123:RT" etc.) so  |
//| ManageOpenPosition() can hand it back to the right rules even     |
//| after an EA or terminal restart.                                  |
//+------------------------------------------------------------------+
void OpenTrade(ENUM_ORDER_TYPE type, double price, double sl, double tp, double atrV,
                double riskMult, double minTargetSpreadRatio, string tag)
  {
   double distance = MathAbs(price - sl);

   if(distance < InpMinStopDistance)
     {
      g_status = StringFormat("%s: stop too tight (%.2f)", tag, distance);
      return;
     }
   if(distance > InpMaxStopDistance)
     {
      g_status = StringFormat("%s: stop too wide (%.2f)", tag, distance);
      return;
     }

   double targetDistance = MathAbs(tp - price);
   double spreadNow      = CurrentSpread();
   if(minTargetSpreadRatio > 0.0 && spreadNow > 0.0 &&
      targetDistance < minTargetSpreadRatio * spreadNow)
     {
      g_status = StringFormat("%s: target %.2f < %.1fx spread %.2f",
                              tag, targetDistance, minTargetSpreadRatio, spreadNow);
      return;
     }

   double lots = CalcLots(price, sl, riskMult);
   if(lots <= 0.0)
     {
      g_status = "size below broker minimum - equity too small for this risk %";
      return;
     }

   sl = NormalizeDouble(sl, _Digits);
   tp = NormalizeDouble(tp, _Digits);

   string comment = "123:" + tag;
   bool ok = (type == ORDER_TYPE_BUY)
             ? trade.Buy(lots, _Symbol, 0.0, sl, tp, comment)
             : trade.Sell(lots, _Symbol, 0.0, sl, tp, comment);

   if(ok)
     {
      g_status = StringFormat("%s: opened %s %.2f lots", tag, (type==ORDER_TYPE_BUY ? "BUY":"SELL"), lots);
      PrintFormat("[%s] %s %.2f lots @ ~%.2f  sl=%.2f tp=%.2f  risk=%.2f%% x%.2f  ATR=%.2f  spread=%.2f",
                  tag, (type==ORDER_TYPE_BUY ? "BUY":"SELL"), lots, price, sl, tp,
                  InpRiskPercent, riskMult, atrV, CurrentSpread());
     }
   else
     {
      g_status = StringFormat("%s: order failed: %d %s", tag, trade.ResultRetcode(), trade.ResultRetcodeDescription());
      PrintFormat("[%s] ORDER FAILED retcode=%d (%s) lots=%.2f sl=%.2f tp=%.2f",
                  tag, trade.ResultRetcode(), trade.ResultRetcodeDescription(), lots, sl, tp);
     }
  }

//+------------------------------------------------------------------+
//| ENTRY DISPATCHER                                                 |
//+------------------------------------------------------------------+
void TryEntry()
  {
   if(InpEnableRetest)
     {
      if(TryEntryRt())
         return;
      if(InpRtUseImmediateBreakout && TryEntryRtImmediate())
         return;
     }
   if(InpEnableBreakout   && TryEntryBk()) return;
   if(InpEnableTrend      && TryEntryTr()) return;
   if(InpEnableMeanReversion && TryEntryMr()) return;
   if(InpEnableWyckoff    && TryEntryWk()) return;
   if(g_status == "" ) g_status = "no setup (any sub-strategy)";
  }

//+------------------------------------------------------------------+
//| RT -- breakout + retest (see XauRetest_M5.mq5 for the full        |
//| rationale of every filter and the pending-breakout state machine).|
//+------------------------------------------------------------------+
void AdvanceRtPendingBreakout()
  {
   g_rtImmSide     = 0;
   g_rtImmStrength = 0.0;

   MqlRates rates[];
   ArraySetAsSeries(rates, true);
   int wantBars = InpRtRangeLookback + 2;
   if(CopyRates(_Symbol, PERIOD_CURRENT, 1, wantBars, rates) != wantBars)
      return;

   double close = rates[0].close;

   if(g_rtPendingSide != 0)
     {
      g_rtPendingAge++;
      bool closedThrough = (g_rtPendingSide == 1 && close < g_rtPendingLevel)
                         || (g_rtPendingSide == -1 && close > g_rtPendingLevel);
      if(g_rtPendingAge > InpRtRetestMaxBars || closedThrough)
        {
         g_rtPendingSide     = 0;
         g_rtPendingLevel    = 0.0;
         g_rtPendingStrength = 0.0;
         g_rtPendingWidth    = 0.0;
        }
     }

   double atr[];
   if(!ReadBuffer(hRtAtr, 0, 1, 2, atr)) return;
   double atrV = atr[0];
   if(atrV <= 0.0) return;

   double rangeHigh = rates[1].high, rangeLow = rates[1].low;
   for(int k = 1; k <= InpRtRangeLookback && k < wantBars; k++)
     {
      rangeHigh = MathMax(rangeHigh, rates[k].high);
      rangeLow  = MathMin(rangeLow,  rates[k].low);
     }

   double barRange = rates[0].high - rates[0].low;
   double closePos = (barRange > 0.0) ? (close - rates[0].low) / barRange : 0.5;
   double margin   = InpRtBreakMarginAtr * atrV;
   bool   decisive = barRange >= InpRtMinBarRangeAtr * atrV;

   bool brokeUp   = decisive && (close > rangeHigh + margin) && (closePos >= 0.5);
   bool brokeDown = decisive && (close < rangeLow  - margin) && ((1.0 - closePos) >= 0.5);

   double barStrength = MathMin(MathMax((barRange / atrV - InpRtMinBarRangeAtr) / InpRtMinBarRangeAtr, 0.0), 1.0);

   if(brokeUp && !brokeDown)
     {
      g_rtPendingSide     = 1;
      g_rtPendingLevel    = rangeHigh;
      g_rtPendingAge      = 0;
      g_rtPendingStrength = barStrength;
      g_rtPendingWidth    = rangeHigh - rangeLow;
     }
   else if(brokeDown && !brokeUp)
     {
      g_rtPendingSide     = -1;
      g_rtPendingLevel    = rangeLow;
      g_rtPendingAge      = 0;
      g_rtPendingStrength = barStrength;
      g_rtPendingWidth    = rangeHigh - rangeLow;
     }

   //--- opt-in second trigger: is bar i ITSELF a fresh, unusually decisive
   //--- breakout, gated by the same regime/ADX/HTF filters as the retest path.
   if(InpRtUseImmediateBreakout)
     {
      bool immUp   = brokeUp   && !brokeDown && (barStrength >= InpRtImmBreakoutMinStrength);
      bool immDown = brokeDown && !brokeUp   && (barStrength >= InpRtImmBreakoutMinStrength);
      if(immUp || immDown)
        {
         double adxBuf[], htfBuf[];
         if(ReadBuffer(hRtAdx, 0, 1, 2, adxBuf) && ReadBuffer(hRtHtfEma, 0, 1, 2, htfBuf))
           {
            double medAtr   = MedianAtr(hRtAtr, InpRtRegimeLookback);
            double adxV     = adxBuf[0];
            double htfEmaV  = htfBuf[0];
            int    sideCand = immUp ? 1 : -1;
            bool regimeOk = (medAtr > 0.0)
                            && (atrV >= InpRtAtrMinMult * medAtr)
                            && (atrV <= InpRtAtrMaxMult * medAtr)
                            && (InpRtAdxMin <= 0.0 || adxV >= InpRtAdxMin)
                            && (!InpRtRequireHtfAlign
                                || (sideCand == 1  && close > htfEmaV)
                                || (sideCand == -1 && close < htfEmaV));
            if(regimeOk)
              {
               g_rtImmSide     = sideCand;
               g_rtImmStrength = barStrength;
              }
           }
        }
     }
  }

double RtClamp01(double x)
  {
   if(x < 0.0) return(0.0);
   if(x > 1.0) return(1.0);
   return(x);
  }

double RtQualityScore(double breakoutStrength, double adxV, double precision, double rejection)
  {
   double adxComponent = (InpRtAdxMin > 0.0 && MathIsValidNumber(adxV))
                          ? RtClamp01((adxV - InpRtAdxMin) / InpRtAdxMin)
                          : 0.5;
   double bs = MathIsValidNumber(breakoutStrength) ? RtClamp01(breakoutStrength) : 0.5;
   return((bs + adxComponent + RtClamp01(precision) + RtClamp01(rejection)) / 4.0);
  }

bool TryEntryRt()
  {
   if(g_rtRetestSide == 0)
     {
      g_status = "RT: no pending breakout to retest";
      return(false);
     }

   datetime thisBarTime = iTime(_Symbol, PERIOD_CURRENT, 1);
   if(g_rtLastFireBarTime != 0)
     {
      int barsSinceFire = iBarShift(_Symbol, PERIOD_CURRENT, g_rtLastFireBarTime, false);
      if(barsSinceFire < InpRtCooldownBars)
        {
         g_status = StringFormat("RT: cooldown (%d/%d bars)", barsSinceFire, InpRtCooldownBars);
         return(false);
        }
     }

   double atr[], adx[], htfEma[];
   if(!ReadBuffer(hRtAtr, 0, 1, 2, atr) || !ReadBuffer(hRtAdx, 0, 1, 2, adx)
      || !ReadBuffer(hRtHtfEma, 0, 1, 2, htfEma))
     {
      g_status = "RT: indicator data not ready";
      return(false);
     }

   MqlRates rates[];
   ArraySetAsSeries(rates, true);
   if(CopyRates(_Symbol, PERIOD_CURRENT, 1, 2, rates) != 2)
     {
      g_status = "RT: price data not ready";
      return(false);
     }

   double close = rates[0].close, high_ = rates[0].high, low_ = rates[0].low;
   double atrV = atr[0], adxV = adx[0], htfEmaV = htfEma[0];
   if(atrV <= 0.0) { g_status = "RT: ATR unavailable"; return(false); }

   double medAtr = MedianAtr(hRtAtr, InpRtRegimeLookback);
   if(medAtr <= 0.0) { g_status = "RT: regime ATR unavailable"; return(false); }
   if(atrV < InpRtAtrMinMult * medAtr) { g_status = "RT: volatility too low";  return(false); }
   if(atrV > InpRtAtrMaxMult * medAtr) { g_status = "RT: volatility too high"; return(false); }
   if(InpRtAdxMin > 0.0 && adxV < InpRtAdxMin)
     {
      g_status = StringFormat("RT: ADX %.1f < %.1f", adxV, InpRtAdxMin);
      return(false);
     }
   if(InpRtRequireHtfAlign)
     {
      if(g_rtRetestSide == 1 && close <= htfEmaV)
        { g_status = "RT: against higher-timeframe trend (long)"; return(false); }
      if(g_rtRetestSide == -1 && close >= htfEmaV)
        { g_status = "RT: against higher-timeframe trend (short)"; return(false); }
     }

   double barRange = high_ - low_;
   double closePos = (barRange > 0.0) ? (close - low_) / barRange : 0.5;
   double tol       = InpRtRetestToleranceAtr * atrV;
   double minOff    = MinStopOffset();
   double sl, tp, price;
   double quality, riskMult;

   if(g_rtRetestSide == 1)
     {
      bool retestOk = (low_ <= g_rtRetestLevel + tol) && (close > g_rtRetestLevel)
                      && (closePos >= InpRtRetestClosePosMin);
      if(!retestOk) { g_status = "RT: no valid retest (support)"; return(false); }
      g_rtLastFireBarTime = thisBarTime;

      double precision = (tol > 0.0) ? (1.0 - MathAbs(low_ - g_rtRetestLevel) / tol) : 0.5;
      double rejection = (InpRtRetestClosePosMin < 1.0)
                          ? (closePos - InpRtRetestClosePosMin) / (1.0 - InpRtRetestClosePosMin)
                          : 1.0;
      quality = RtQualityScore(g_rtRetestStrength, adxV, precision, rejection);

      price = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
      sl    = MathMin(low_ - InpRtStopAtrBuffer * atrV, close - InpRtMinStopAtrMult * atrV);
      if(sl >= price - minOff) sl = price - MathMax(minOff, InpMinStopDistance);
      double rr     = InpRtQualityMinRR + quality * (InpRtQualityMaxRR - InpRtQualityMinRR);
      double tpDist = rr * (price - sl);
      if(InpRtUseMeasuredMove && g_rtRetestWidth > 0.0) tpDist = MathMax(tpDist, g_rtRetestWidth);
      tp       = price + tpDist;
      riskMult = 1.0 + quality * (InpRtQualityMaxRiskMult - 1.0);
      OpenTrade(ORDER_TYPE_BUY, price, sl, tp, atrV, riskMult, InpRtMinTargetSpreadRatio, "RT");
      return(true);
     }
   else
     {
      bool retestOk = (high_ >= g_rtRetestLevel - tol) && (close < g_rtRetestLevel)
                      && ((1.0 - closePos) >= InpRtRetestClosePosMin);
      if(!retestOk) { g_status = "RT: no valid retest (resistance)"; return(false); }
      g_rtLastFireBarTime = thisBarTime;

      double precision = (tol > 0.0) ? (1.0 - MathAbs(high_ - g_rtRetestLevel) / tol) : 0.5;
      double rejection = (InpRtRetestClosePosMin < 1.0)
                          ? ((1.0 - closePos) - InpRtRetestClosePosMin) / (1.0 - InpRtRetestClosePosMin)
                          : 1.0;
      quality = RtQualityScore(g_rtRetestStrength, adxV, precision, rejection);

      price = SymbolInfoDouble(_Symbol, SYMBOL_BID);
      sl    = MathMax(high_ + InpRtStopAtrBuffer * atrV, close + InpRtMinStopAtrMult * atrV);
      if(sl <= price + minOff) sl = price + MathMax(minOff, InpMinStopDistance);
      double rr     = InpRtQualityMinRR + quality * (InpRtQualityMaxRR - InpRtQualityMinRR);
      double tpDist = rr * (sl - price);
      if(InpRtUseMeasuredMove && g_rtRetestWidth > 0.0) tpDist = MathMax(tpDist, g_rtRetestWidth);
      tp       = price - tpDist;
      riskMult = 1.0 + quality * (InpRtQualityMaxRiskMult - 1.0);
      OpenTrade(ORDER_TYPE_SELL, price, sl, tp, atrV, riskMult, InpRtMinTargetSpreadRatio, "RT");
      return(true);
     }
  }

bool TryEntryRtImmediate()
  {
   if(g_rtImmSide == 0)
     {
      g_status = "RT-imm: no immediate breakout";
      return(false);
     }

   datetime thisBarTime = iTime(_Symbol, PERIOD_CURRENT, 1);
   if(g_rtLastFireBarTime != 0)
     {
      int barsSinceFire = iBarShift(_Symbol, PERIOD_CURRENT, g_rtLastFireBarTime, false);
      if(barsSinceFire < InpRtCooldownBars)
        {
         g_status = StringFormat("RT-imm: cooldown (%d/%d bars)", barsSinceFire, InpRtCooldownBars);
         return(false);
        }
     }

   double atr[];
   if(!ReadBuffer(hRtAtr, 0, 1, 2, atr)) { g_status = "RT-imm: indicator data not ready"; return(false); }
   double atrV = atr[0];
   if(atrV <= 0.0) { g_status = "RT-imm: ATR unavailable"; return(false); }

   double close  = iClose(_Symbol, PERIOD_CURRENT, 1);
   double minOff = MinStopOffset();
   double quality  = RtClamp01(g_rtImmStrength);
   double riskMult = 1.0 + quality * (InpRtQualityMaxRiskMult - 1.0);
   double price, sl, tp;

   if(g_rtImmSide == 1)
     {
      price = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
      sl    = close - InpRtImmBreakoutStopAtrMult * atrV;
      if(sl >= price - minOff) sl = price - MathMax(minOff, InpMinStopDistance);
      tp    = price + InpRtImmBreakoutRR * (price - sl);
      g_rtLastFireBarTime = thisBarTime;
      OpenTrade(ORDER_TYPE_BUY, price, sl, tp, atrV, riskMult, InpRtMinTargetSpreadRatio, "RT");
     }
   else
     {
      price = SymbolInfoDouble(_Symbol, SYMBOL_BID);
      sl    = close + InpRtImmBreakoutStopAtrMult * atrV;
      if(sl <= price + minOff) sl = price + MathMax(minOff, InpMinStopDistance);
      tp    = price - InpRtImmBreakoutRR * (sl - price);
      g_rtLastFireBarTime = thisBarTime;
      OpenTrade(ORDER_TYPE_SELL, price, sl, tp, atrV, riskMult, InpRtMinTargetSpreadRatio, "RT");
     }
   return(true);
  }

void ManageRt(datetime now, ulong ticket, long posType, double entry, double sl, double tp)
  {
   double atrBuf[];
   if(!ReadBuffer(hRtAtr, 0, 1, 2, atrBuf)) return;
   double atrV = atrBuf[0];
   if(atrV <= 0.0) return;

   double close  = iClose(_Symbol, PERIOD_CURRENT, 1);
   double risk   = MathAbs(entry - sl);
   if(risk <= 0.0) return;

   double newSl = sl;
   if(posType == POSITION_TYPE_BUY)
     {
      double gainedR = (close - entry) / risk;
      if(InpRtUseBreakeven && gainedR >= InpRtBreakevenAtR)
         newSl = MathMax(newSl, entry + InpRtBreakevenOffAtr * atrV);
      if(InpRtUseTrail && gainedR >= InpRtTrailAtR)
         newSl = MathMax(newSl, close - InpRtTrailAtrMult * atrV);
      double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);
      newSl = MathMin(newSl, bid - MinStopOffset());
      if(newSl <= sl) return;
     }
   else
     {
      double gainedR = (entry - close) / risk;
      if(InpRtUseBreakeven && gainedR >= InpRtBreakevenAtR)
         newSl = MathMin(newSl, entry - InpRtBreakevenOffAtr * atrV);
      if(InpRtUseTrail && gainedR >= InpRtTrailAtR)
         newSl = MathMin(newSl, close + InpRtTrailAtrMult * atrV);
      double ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
      newSl = MathMax(newSl, ask + MinStopOffset());
      if(newSl >= sl) return;
     }
   ApplyStopUpdate(ticket, newSl, sl, tp);
  }

//+------------------------------------------------------------------+
//| BK -- breakout momentum, no retest wait (see XauBreak_M5.mq5).    |
//+------------------------------------------------------------------+
bool TryEntryBk()
  {
   int need = MathMax(InpBkRegimeLookback, MathMax(InpBkTrendEmaPeriod, InpBkRangeLookback)) + 10;
   if(Bars(_Symbol, PERIOD_CURRENT) < need)
     {
      g_status = StringFormat("BK: warming up (%d/%d bars)", Bars(_Symbol, PERIOD_CURRENT), need);
      return(false);
     }

   double atr[], adx[], trend[];
   if(!ReadBuffer(hBkAtr,   0, 1, 2, atr) ||
      !ReadBuffer(hBkAdx,   0, 1, 2, adx) ||
      !ReadBuffer(hBkTrend, 0, 1, 2, trend))
     {
      g_status = "BK: indicator data not ready";
      return(false);
     }

   MqlRates rates[];
   ArraySetAsSeries(rates, true);
   int wantBars = InpBkRangeLookback + 2;
   if(CopyRates(_Symbol, PERIOD_CURRENT, 1, wantBars, rates) != wantBars)
     {
      g_status = "BK: price data not ready";
      return(false);
     }

   double close = rates[0].close;
   double atrV  = atr[0];
   double adxV  = adx[0];
   double trendV= trend[0];
   if(atrV <= 0.0) { g_status = "BK: ATR unavailable"; return(false); }

   double medAtr = MedianAtr(hBkAtr, InpBkRegimeLookback);
   if(medAtr <= 0.0) { g_status = "BK: regime ATR unavailable"; return(false); }
   if(atrV < InpBkAtrMinMult * medAtr) { g_status = "BK: volatility too low";  return(false); }
   if(atrV > InpBkAtrMaxMult * medAtr) { g_status = "BK: volatility too high"; return(false); }
   if(InpBkAdxMin > 0.0 && adxV < InpBkAdxMin)
     {
      g_status = StringFormat("BK: ADX %.1f < %.1f", adxV, InpBkAdxMin);
      return(false);
     }

   double rangeHigh = rates[1].high, rangeLow = rates[1].low;
   for(int k = 1; k <= InpBkRangeLookback && k < wantBars; k++)
     {
      rangeHigh = MathMax(rangeHigh, rates[k].high);
      rangeLow  = MathMin(rangeLow,  rates[k].low);
     }
   double width = rangeHigh - rangeLow;

   if(width > InpBkSqueezeMaxAtr * atrV)
     {
      g_status = StringFormat("BK: range too wide (%.1f x ATR)", width / atrV);
      return(false);
     }

   double barRange = rates[0].high - rates[0].low;
   if(barRange < InpBkMinBarRangeAtr * atrV)
     {
      g_status = StringFormat("BK: bar too small (%.2f x ATR)", barRange / atrV);
      return(false);
     }

   double closePos = (barRange > 0.0) ? (close - rates[0].low) / barRange : 0.5;
   double margin   = InpBkBreakMarginAtr * atrV;

   bool upOk   = (close > rangeHigh + margin) && (closePos >= InpBkClosePositionMin);
   bool downOk = (close < rangeLow  - margin) && ((1.0 - closePos) >= InpBkClosePositionMin);

   if(InpBkRequireTrendAlign)
     {
      upOk   = upOk   && (close > trendV);
      downOk = downOk && (close < trendV);
     }

   if(upOk == downOk) { g_status = "BK: no breakout"; return(false); }

   double sl, tp, price;
   double minOff = MinStopOffset();

   if(upOk)
     {
      price = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
      sl    = MathMin(rangeHigh - InpBkStopAtrBuffer * atrV, close - InpBkMinStopAtrMult * atrV);
      if(sl >= price - minOff) sl = price - MathMax(minOff, InpMinStopDistance);
      tp    = price + InpBkRewardRisk * (price - sl);
      OpenTrade(ORDER_TYPE_BUY, price, sl, tp, atrV, 1.0, InpBkMinTargetSpreadRatio, "BK");
     }
   else
     {
      price = SymbolInfoDouble(_Symbol, SYMBOL_BID);
      sl    = MathMax(rangeLow + InpBkStopAtrBuffer * atrV, close + InpBkMinStopAtrMult * atrV);
      if(sl <= price + minOff) sl = price + MathMax(minOff, InpMinStopDistance);
      tp    = price - InpBkRewardRisk * (sl - price);
      OpenTrade(ORDER_TYPE_SELL, price, sl, tp, atrV, 1.0, InpBkMinTargetSpreadRatio, "BK");
     }
   return(true);
  }

void ManageBk(datetime now, ulong ticket, long posType, double entry, double sl, double tp)
  {
   // Deliberately does almost nothing beyond an optional break-even (OFF by
   // default): at BK's low hit rate, winners must be allowed to reach the
   // full target. See "THE BREAK-EVEN TRAP" in the file header.
   if(!InpBkUseBreakeven) return;

   double atrBuf[];
   if(!ReadBuffer(hBkAtr, 0, 1, 2, atrBuf)) return;
   double atrV = atrBuf[0];
   if(atrV <= 0.0) return;

   double close = iClose(_Symbol, PERIOD_CURRENT, 1);
   double risk  = MathAbs(entry - sl);
   if(risk <= 0.0) return;

   double newSl = sl;
   if(posType == POSITION_TYPE_BUY)
     {
      if((close - entry) / risk >= InpBkBreakevenAtR)
         newSl = MathMax(newSl, entry + InpBkBreakevenOffAtr * atrV);
      double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);
      newSl = MathMin(newSl, bid - MinStopOffset());
      if(newSl <= sl) return;
     }
   else
     {
      if((entry - close) / risk >= InpBkBreakevenAtR)
         newSl = MathMin(newSl, entry - InpBkBreakevenOffAtr * atrV);
      double ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
      newSl = MathMax(newSl, ask + MinStopOffset());
      if(newSl >= sl) return;
     }
   ApplyStopUpdate(ticket, newSl, sl, tp);
  }

//+------------------------------------------------------------------+
//| TR -- trend pullback (see XauTrend_M5.mq5).                       |
//+------------------------------------------------------------------+
bool TryEntryTr()
  {
   int need = MathMax(InpTrRegimeLookback, InpTrEmaTrend) + 10;
   if(Bars(_Symbol, PERIOD_CURRENT) < need)
     {
      g_status = StringFormat("TR: warming up (%d/%d bars)", Bars(_Symbol, PERIOD_CURRENT), need);
      return(false);
     }

   double emaF[], emaS[], emaT[], atr[], adx[], rsi[];
   int rsiCount = InpTrPullbackLookback + 2;

   if(!ReadBuffer(hTrEmaFast, 0, 1, 2, emaF) ||
      !ReadBuffer(hTrEmaSlow, 0, 1, 2, emaS) ||
      !ReadBuffer(hTrEmaTrend,0, 1, 2, emaT) ||
      !ReadBuffer(hTrAtr,     0, 1, 2, atr)  ||
      !ReadBuffer(hTrAdx,     0, 1, 2, adx)  ||
      !ReadBuffer(hTrRsi,     0, 1, rsiCount, rsi))
     {
      g_status = "TR: indicator data not ready";
      return(false);
     }

   MqlRates rates[];
   ArraySetAsSeries(rates, true);
   int wantBars = MathMax(InpTrSwingLookback + 3, 5);
   if(CopyRates(_Symbol, PERIOD_CURRENT, 1, wantBars, rates) != wantBars)
     {
      g_status = "TR: price data not ready";
      return(false);
     }

   double close   = rates[0].close;
   double open    = rates[0].open;
   double prevHi  = rates[1].high;
   double prevLo  = rates[1].low;

   double atrV    = atr[0];
   double adxV    = adx[0];
   double rsiV    = rsi[0];
   if(atrV <= 0.0) { g_status = "TR: ATR unavailable"; return(false); }

   double medAtr = MedianAtr(hTrAtr, InpTrRegimeLookback);
   if(medAtr <= 0.0) { g_status = "TR: regime ATR unavailable"; return(false); }
   if(atrV < InpTrAtrMinMult * medAtr) { g_status = "TR: volatility too low";  return(false); }
   if(atrV > InpTrAtrMaxMult * medAtr) { g_status = "TR: volatility too high"; return(false); }

   if(adxV < InpTrAdxMin) { g_status = StringFormat("TR: ADX %.1f < %.1f", adxV, InpTrAdxMin); return(false); }

   double swingLow = rates[0].low;
   double swingHigh= rates[0].high;
   for(int i = 0; i < InpTrSwingLookback && i < wantBars; i++)
     {
      swingLow  = MathMin(swingLow,  rates[i].low);
      swingHigh = MathMax(swingHigh, rates[i].high);
     }

   double rsiMin = rsi[0], rsiMax = rsi[0];
   for(int i = 0; i < InpTrPullbackLookback && i < rsiCount; i++)
     {
      rsiMin = MathMin(rsiMin, rsi[i]);
      rsiMax = MathMax(rsiMax, rsi[i]);
     }

   bool longOk  = (emaF[0] > emaS[0]) && (close > emaT[0]) &&
                  (rsiMin <= InpTrRsiPullbackLong)  && (rsiV > InpTrRsiPullbackLong) &&
                  (close > prevHi) && (close > open);

   bool shortOk = (emaF[0] < emaS[0]) && (close < emaT[0]) &&
                  (rsiMax >= InpTrRsiPullbackShort) && (rsiV < InpTrRsiPullbackShort) &&
                  (close < prevLo) && (close < open);

   if(longOk == shortOk) { g_status = "TR: no setup"; return(false); }

   double sl, tp, price;
   double minOff = MinStopOffset();

   if(longOk)
     {
      price = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
      sl    = MathMin(swingLow - InpTrStopAtrBuffer * atrV, close - InpTrMinStopAtrMult * atrV);
      if(sl >= price - minOff) sl = price - MathMax(minOff, InpMinStopDistance);
      tp    = price + InpTrRewardRisk * (price - sl);
      OpenTrade(ORDER_TYPE_BUY, price, sl, tp, atrV, 1.0, 0.0, "TR");
     }
   else
     {
      price = SymbolInfoDouble(_Symbol, SYMBOL_BID);
      sl    = MathMax(swingHigh + InpTrStopAtrBuffer * atrV, close + InpTrMinStopAtrMult * atrV);
      if(sl <= price + minOff) sl = price + MathMax(minOff, InpMinStopDistance);
      tp    = price - InpTrRewardRisk * (sl - price);
      OpenTrade(ORDER_TYPE_SELL, price, sl, tp, atrV, 1.0, 0.0, "TR");
     }
   return(true);
  }

void ManageTr(datetime now, ulong ticket, long posType, double entry, double sl, double tp)
  {
   double atrBuf[];
   if(!ReadBuffer(hTrAtr, 0, 1, 2, atrBuf)) return;
   double atrV = atrBuf[0];
   if(atrV <= 0.0) return;

   double close = iClose(_Symbol, PERIOD_CURRENT, 1);
   double risk  = MathAbs(entry - sl);
   if(risk <= 0.0) return;

   double newSl = sl;
   if(posType == POSITION_TYPE_BUY)
     {
      double gainedR = (close - entry) / risk;
      if(InpTrUseBreakeven && gainedR >= InpTrBreakevenAtR)
         newSl = MathMax(newSl, entry + InpTrBreakevenOffAtr * atrV);
      if(InpTrUseTrail && gainedR >= InpTrTrailAtR)
         newSl = MathMax(newSl, close - InpTrTrailAtrMult * atrV);
      double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);
      newSl = MathMin(newSl, bid - MinStopOffset());
      if(newSl <= sl) return;
     }
   else
     {
      double gainedR = (entry - close) / risk;
      if(InpTrUseBreakeven && gainedR >= InpTrBreakevenAtR)
         newSl = MathMin(newSl, entry - InpTrBreakevenOffAtr * atrV);
      if(InpTrUseTrail && gainedR >= InpTrTrailAtR)
         newSl = MathMin(newSl, close + InpTrTrailAtrMult * atrV);
      double ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
      newSl = MathMax(newSl, ask + MinStopOffset());
      if(newSl >= sl) return;
     }
   ApplyStopUpdate(ticket, newSl, sl, tp);
  }

//+------------------------------------------------------------------+
//| MR -- mean-reversion scalp (see Anas_M5.mq5).                     |
//+------------------------------------------------------------------+
bool TryEntryMr()
  {
   int need = MathMax(InpMrRegimeLookback,
                      MathMax(InpMrEmaPeriod, InpMrHtfEmaPeriod + InpMrHtfSlopeLookback)) + 10;
   if(Bars(_Symbol, PERIOD_CURRENT) < need)
     {
      g_status = StringFormat("MR: warming up (%d/%d bars)", Bars(_Symbol, PERIOD_CURRENT), need);
      return(false);
     }

   double ema[], atr[], adx[], rsi[], htf[];
   int rsiBars = MathMax(InpMrDivergenceLookback + 2, 2);
   if(!ReadBuffer(hMrHtfEma, 0, 1, InpMrHtfSlopeLookback + 1, htf) ||
      !ReadBuffer(hMrEma, 0, 1, 2, ema) ||
      !ReadBuffer(hMrAtr, 0, 1, 2, atr) ||
      !ReadBuffer(hMrAdx, 0, 1, 2, adx) ||
      !ReadBuffer(hMrRsi, 0, 1, rsiBars, rsi))
     {
      g_status = "MR: indicator data not ready";
      return(false);
     }

   MqlRates rates[];
   ArraySetAsSeries(rates, true);
   int wantBars = MathMax(InpMrSwingLookback + 3, MathMax(InpMrDivergenceLookback + 2, 5));
   if(CopyRates(_Symbol, PERIOD_CURRENT, 1, wantBars, rates) != wantBars)
     {
      g_status = "MR: price data not ready";
      return(false);
     }

   double close = rates[0].close;
   double open  = rates[0].open;
   double atrV  = atr[0];
   double adxV  = adx[0];
   double rsiV  = rsi[0];
   double emaV  = ema[0];
   if(atrV <= 0.0) { g_status = "MR: ATR unavailable"; return(false); }

   double medAtr = MedianAtr(hMrAtr, InpMrRegimeLookback);
   if(medAtr <= 0.0) { g_status = "MR: regime ATR unavailable"; return(false); }
   if(atrV < InpMrAtrMinMult * medAtr) { g_status = "MR: volatility too low";  return(false); }
   if(atrV > InpMrAtrMaxMult * medAtr) { g_status = "MR: volatility too high"; return(false); }

   if(adxV >= InpMrAdxMax)
     {
      g_status = StringFormat("MR: trending, ADX %.1f >= %.1f", adxV, InpMrAdxMax);
      return(false);
     }

   double swingLow = rates[0].low, swingHigh = rates[0].high;
   for(int i = 0; i < InpMrSwingLookback && i < wantBars; i++)
     {
      swingLow  = MathMin(swingLow,  rates[i].low);
      swingHigh = MathMax(swingHigh, rates[i].high);
     }

   double stretch = (close - emaV) / atrV;

   double barRange = rates[0].high - rates[0].low;
   double closePos = (barRange > 0.0) ? (close - rates[0].low) / barRange : 0.5;
   bool longBarOk  = closePos >= InpMrClosePositionMin;
   bool shortBarOk = (1.0 - closePos) >= InpMrClosePositionMin;

   double htfSlope = (htf[0] - htf[InpMrHtfSlopeLookback]) / (double)InpMrHtfSlopeLookback;
   double slopeAtr = htfSlope / atrV;
   bool longHtfOk  = (InpMrHtfMaxSlopeAtr <= 0.0) || (slopeAtr >= -InpMrHtfMaxSlopeAtr);
   bool shortHtfOk = (InpMrHtfMaxSlopeAtr <= 0.0) || (slopeAtr <=  InpMrHtfMaxSlopeAtr);

   bool longDivOk = true, shortDivOk = true;
   if(InpMrRequireDivergence)
     {
      double priorLow = rates[1].low, priorHigh = rates[1].high;
      double priorRsiLow = rsi[1],    priorRsiHigh = rsi[1];
      for(int k = 1; k <= InpMrDivergenceLookback && k < wantBars && k < rsiBars; k++)
        {
         priorLow     = MathMin(priorLow,     rates[k].low);
         priorHigh    = MathMax(priorHigh,    rates[k].high);
         priorRsiLow  = MathMin(priorRsiLow,  rsi[k]);
         priorRsiHigh = MathMax(priorRsiHigh, rsi[k]);
        }
      longDivOk  = (rates[0].low  <= priorLow)  && (rsiV > priorRsiLow);
      shortDivOk = (rates[0].high >= priorHigh) && (rsiV < priorRsiHigh);
     }

   bool longOk  = (stretch <= -InpMrStretchAtr) && (rsiV <= InpMrRsiOversold)
                  && (close > open) && longBarOk && longHtfOk && longDivOk;
   bool shortOk = (stretch >=  InpMrStretchAtr) && (rsiV >= InpMrRsiOverbought)
                  && (close < open) && shortBarOk && shortHtfOk && shortDivOk;

   if(longOk == shortOk)
     {
      g_status = StringFormat("MR: no setup (stretch %+.2f, RSI %.0f)", stretch, rsiV);
      return(false);
     }

   double sl, tp, price;
   double minOff = MinStopOffset();

   if(longOk)
     {
      price = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
      sl    = MathMin(swingLow - InpMrStopAtrBuffer * atrV, close - InpMrMinStopAtrMult * atrV);
      if(sl >= price - minOff) sl = price - MathMax(minOff, InpMinStopDistance);
      tp    = price + InpMrRewardRisk * (price - sl);
      OpenTrade(ORDER_TYPE_BUY, price, sl, tp, atrV, 1.0, InpMrMinTargetSpreadRatio, "MR");
     }
   else
     {
      price = SymbolInfoDouble(_Symbol, SYMBOL_BID);
      sl    = MathMax(swingHigh + InpMrStopAtrBuffer * atrV, close + InpMrMinStopAtrMult * atrV);
      if(sl <= price + minOff) sl = price + MathMax(minOff, InpMinStopDistance);
      tp    = price - InpMrRewardRisk * (sl - price);
      OpenTrade(ORDER_TYPE_SELL, price, sl, tp, atrV, 1.0, InpMrMinTargetSpreadRatio, "MR");
     }
   return(true);
  }

void ManageMr(datetime now, ulong ticket, long posType, double entry, double sl, double tp)
  {
   if(!InpMrUseBreakeven) return;

   double atrBuf[];
   if(!ReadBuffer(hMrAtr, 0, 1, 2, atrBuf)) return;
   double atrV = atrBuf[0];
   if(atrV <= 0.0) return;

   double close = iClose(_Symbol, PERIOD_CURRENT, 1);
   double risk  = MathAbs(entry - sl);
   if(risk <= 0.0) return;

   double newSl = sl;
   if(posType == POSITION_TYPE_BUY)
     {
      if((close - entry) / risk >= InpMrBreakevenAtR)
         newSl = MathMax(newSl, entry + InpMrBreakevenOffAtr * atrV);
      double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);
      newSl = MathMin(newSl, bid - MinStopOffset());
      if(newSl <= sl) return;
     }
   else
     {
      if((entry - close) / risk >= InpMrBreakevenAtR)
         newSl = MathMin(newSl, entry - InpMrBreakevenOffAtr * atrV);
      double ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
      newSl = MathMax(newSl, ask + MinStopOffset());
      if(newSl >= sl) return;
     }
   ApplyStopUpdate(ticket, newSl, sl, tp);
  }

//+------------------------------------------------------------------+
//| WK -- volume-profile Wyckoff fade (see XauWyck_M5.mq5).            |
//+------------------------------------------------------------------+
bool BuildWkProfile(double &poc, double &vah, double &val)
  {
   int lookback = InpWkProfileLookback;
   int bins     = InpWkProfileBins;
   if(lookback < 10 || bins < 5) return(false);

   MqlRates r[];
   ArraySetAsSeries(r, true);
   if(CopyRates(_Symbol, PERIOD_CURRENT, 1, lookback, r) != lookback)
      return(false);

   double lo = r[0].low, hi = r[0].high;
   for(int k = 0; k < lookback; k++)
     {
      lo = MathMin(lo, r[k].low);
      hi = MathMax(hi, r[k].high);
     }
   if(hi <= lo) return(false);

   double width = (hi - lo) / bins;
   if(width <= 0.0) return(false);

   double hist[];
   ArrayResize(hist, bins);
   ArrayInitialize(hist, 0.0);

   double total = 0.0;
   for(int k = 0; k < lookback; k++)
     {
      double v = (double)r[k].tick_volume;   // TICK volume: gold CFDs have no real volume
      if(v <= 0.0) v = 1.0;
      int first = (int)MathFloor((r[k].low  - lo) / width);
      int last  = (int)MathCeil ((r[k].high - lo) / width);
      if(first < 0) first = 0;
      if(last <= first) last = first + 1;
      if(last > bins)   last = bins;
      if(first >= bins) first = bins - 1;
      double share = v / (double)(last - first);
      for(int b = first; b < last; b++) { hist[b] += share; total += share; }
     }
   if(total <= 0.0) return(false);

   int pocIdx = 0;
   for(int b = 1; b < bins; b++)
      if(hist[b] > hist[pocIdx]) pocIdx = b;

   int loIdx = pocIdx, hiIdx = pocIdx;
   double covered = hist[pocIdx];
   double target  = total * InpWkValueAreaPct;
   while(covered < target && (loIdx > 0 || hiIdx < bins - 1))
     {
      double down = (loIdx > 0)        ? hist[loIdx - 1] : -1.0;
      double up   = (hiIdx < bins - 1) ? hist[hiIdx + 1] : -1.0;
      if(up >= down) { hiIdx++; covered += hist[hiIdx]; }
      else           { loIdx--; covered += hist[loIdx]; }
     }

   poc = lo + (pocIdx + 0.5) * width;
   vah = lo + (hiIdx + 1)    * width;
   val = lo + loIdx          * width;
   return(vah > val);
  }

bool TryEntryWk()
  {
   int need = MathMax(InpWkRegimeLookback, InpWkProfileLookback) + 10;
   if(Bars(_Symbol, PERIOD_CURRENT) < need)
     {
      g_status = StringFormat("WK: warming up (%d/%d bars)", Bars(_Symbol, PERIOD_CURRENT), need);
      return(false);
     }

   double atr[], adx[];
   if(!ReadBuffer(hWkAtr, 0, 1, 2, atr) || !ReadBuffer(hWkAdx, 0, 1, 2, adx))
     {
      g_status = "WK: indicator data not ready";
      return(false);
     }

   MqlRates rates[];
   ArraySetAsSeries(rates, true);
   if(CopyRates(_Symbol, PERIOD_CURRENT, 1, 3, rates) != 3)
     {
      g_status = "WK: price data not ready";
      return(false);
     }

   double close = rates[0].close;
   double highV = rates[0].high;
   double lowV  = rates[0].low;
   double atrV  = atr[0];
   double adxV  = adx[0];
   if(atrV <= 0.0) { g_status = "WK: ATR unavailable"; return(false); }

   double medAtr = MedianAtr(hWkAtr, InpWkRegimeLookback);
   if(medAtr <= 0.0) { g_status = "WK: regime ATR unavailable"; return(false); }
   if(atrV < InpWkAtrMinMult * medAtr) { g_status = "WK: volatility too low";  return(false); }
   if(atrV > InpWkAtrMaxMult * medAtr) { g_status = "WK: volatility too high"; return(false); }

   if(adxV >= InpWkAdxMax)
     {
      g_status = StringFormat("WK: trending, ADX %.1f >= %.1f", adxV, InpWkAdxMax);
      return(false);
     }

   // Refresh the cached profile on schedule. Same design as XauWyck_M5.mq5:
   // only rebuilt while this function actually runs, i.e. only while flat --
   // an open position (of ANY sub-strategy) leaves the profile un-refreshed
   // until the position closes and WK gets to run again.
   g_wkBarsSinceProfile++;
   if(g_wkBarsSinceProfile >= InpWkProfileUpdateBars || g_wkVah <= g_wkVal)
     {
      if(BuildWkProfile(g_wkPoc, g_wkVah, g_wkVal))
         g_wkBarsSinceProfile = 0;
      else
        {
         g_status = "WK: profile unavailable";
         return(false);
        }
     }
   if(g_wkVah <= g_wkVal) { g_status = "WK: profile invalid"; return(false); }

   double volAvg = 0.0;
   int volBars = MathMin(InpWkProfileLookback, 200);
   MqlRates vr[];
   ArraySetAsSeries(vr, true);
   if(CopyRates(_Symbol, PERIOD_CURRENT, 1, volBars, vr) == volBars)
     {
      double sum = 0.0;
      for(int k = 0; k < volBars; k++) sum += (double)vr[k].tick_volume;
      volAvg = sum / volBars;
     }
   double volNow = (double)rates[0].tick_volume;
   bool volOk = (volAvg <= 0.0) || (volNow <= InpWkProbeVolumeMax * volAvg);

   double barRange = highV - lowV;
   double closePos = (barRange > 0.0) ? (close - lowV) / barRange : 0.5;

   double probe  = InpWkProbeDepthAtr * atrV;
   double margin = InpWkCloseBackMarginAtr * atrV;

   bool spring = (lowV  <= g_wkVal - probe) && (close >= g_wkVal + margin)
                 && (closePos >= InpWkClosePositionMin) && volOk;
   bool upthrust = (highV >= g_wkVah + probe) && (close <= g_wkVah - margin)
                 && ((1.0 - closePos) >= InpWkClosePositionMin) && volOk;

   if(spring == upthrust)
     {
      g_status = StringFormat("WK: no setup (VAL %.2f POC %.2f VAH %.2f)", g_wkVal, g_wkPoc, g_wkVah);
      return(false);
     }

   double sl, tp, price;
   double minOff = MinStopOffset();

   if(spring)
     {
      price = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
      sl    = MathMin(lowV - InpWkStopAtrBuffer * atrV, close - InpWkMinStopAtrMult * atrV);
      if(sl >= price - minOff) sl = price - MathMax(minOff, InpMinStopDistance);
      tp    = price + InpWkRewardRisk * (price - sl);
      if(InpWkTargetPoc && g_wkPoc > price) tp = MathMin(tp, g_wkPoc);
      if(tp <= price) { g_status = "WK: target below entry"; return(false); }
      OpenTrade(ORDER_TYPE_BUY, price, sl, tp, atrV, 1.0, InpWkMinTargetSpreadRatio, "WK");
     }
   else
     {
      price = SymbolInfoDouble(_Symbol, SYMBOL_BID);
      sl    = MathMax(highV + InpWkStopAtrBuffer * atrV, close + InpWkMinStopAtrMult * atrV);
      if(sl <= price + minOff) sl = price + MathMax(minOff, InpMinStopDistance);
      tp    = price - InpWkRewardRisk * (sl - price);
      if(InpWkTargetPoc && g_wkPoc < price) tp = MathMax(tp, g_wkPoc);
      if(tp >= price) { g_status = "WK: target above entry"; return(false); }
      OpenTrade(ORDER_TYPE_SELL, price, sl, tp, atrV, 1.0, InpWkMinTargetSpreadRatio, "WK");
     }
   return(true);
  }

void ManageWk(datetime now, ulong ticket, long posType, double entry, double sl, double tp)
  {
   if(!InpWkUseBreakeven) return;

   double atrBuf[];
   if(!ReadBuffer(hWkAtr, 0, 1, 2, atrBuf)) return;
   double atrV = atrBuf[0];
   if(atrV <= 0.0) return;

   double close = iClose(_Symbol, PERIOD_CURRENT, 1);
   double risk  = MathAbs(entry - sl);
   if(risk <= 0.0) return;

   double newSl = sl;
   if(posType == POSITION_TYPE_BUY)
     {
      if((close - entry) / risk >= InpWkBreakevenAtR)
         newSl = MathMax(newSl, entry + InpWkBreakevenOffAtr * atrV);
      double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);
      newSl = MathMin(newSl, bid - MinStopOffset());
      if(newSl <= sl) return;
     }
   else
     {
      if((entry - close) / risk >= InpWkBreakevenAtR)
         newSl = MathMin(newSl, entry - InpWkBreakevenOffAtr * atrV);
      double ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
      newSl = MathMax(newSl, ask + MinStopOffset());
      if(newSl >= sl) return;
     }
   ApplyStopUpdate(ticket, newSl, sl, tp);
  }

//+------------------------------------------------------------------+
//| Shared stop-tighten helper: rounds, checks it is worth a round     |
//| trip, and applies it. Every ManageXx() above already guarantees    |
//| newSl only ever tightens before calling this.                      |
//+------------------------------------------------------------------+
void ApplyStopUpdate(ulong ticket, double newSl, double oldSl, double tp)
  {
   newSl = NormalizeDouble(newSl, _Digits);
   if(MathAbs(newSl - oldSl) < g_tickSize) return;

   if(trade.PositionModify(ticket, newSl, tp))
      g_status = StringFormat("stop -> %.2f", newSl);
   else
      PrintFormat("PositionModify #%I64u failed: %d (%s)",
                  ticket, trade.ResultRetcode(), trade.ResultRetcodeDescription());
  }

//+------------------------------------------------------------------+
//| MANAGEMENT DISPATCHER: reads which sub-strategy owns this trade   |
//| from the position's comment ("123:RT", "123:BK", ...) and hands   |
//| it to that sub-strategy's own management rules.                   |
//+------------------------------------------------------------------+
void ManageOpenPosition(datetime now)
  {
   if(!SelectOwnPosition()) return;

   ulong  ticket   = posInfo.Ticket();
   long   posType  = posInfo.PositionType();
   double entry    = posInfo.PriceOpen();
   double sl       = posInfo.StopLoss();
   double tp       = posInfo.TakeProfit();
   string comment  = posInfo.Comment();

   //--- forced flat: session over, wrong day, or the day's risk budget is spent
   bool mustFlat = g_halted
                   || !IsTradingDay(GmtDayOfWeek(now))
                   || (GmtHour(now) >= InpFlatByHour);

   if(mustFlat)
     {
      if(trade.PositionClose(ticket))
         PrintFormat("Closed #%I64u: %s", ticket,
                     (g_halted ? g_haltReason : "session close"));
      g_status = "flattened";
      return;
     }

   //--- time stop (shared: InpMaxBarsInTrade applies across every sub-strategy)
   if(InpMaxBarsInTrade > 0)
     {
      datetime opened = (datetime)posInfo.Time();
      int barsHeld = iBarShift(_Symbol, PERIOD_CURRENT, opened, false);
      if(barsHeld >= InpMaxBarsInTrade)
        {
         if(trade.PositionClose(ticket))
            PrintFormat("Closed #%I64u: held %d bars (max %d)", ticket, barsHeld, InpMaxBarsInTrade);
         g_status = "time stop";
         return;
        }
     }

   if(sl == 0.0) return;      // nothing to tighten against

        if(StringFind(comment, "RT") >= 0) ManageRt(now, ticket, posType, entry, sl, tp);
   else if(StringFind(comment, "BK") >= 0) ManageBk(now, ticket, posType, entry, sl, tp);
   else if(StringFind(comment, "TR") >= 0) ManageTr(now, ticket, posType, entry, sl, tp);
   else if(StringFind(comment, "MR") >= 0) ManageMr(now, ticket, posType, entry, sl, tp);
   else if(StringFind(comment, "WK") >= 0) ManageWk(now, ticket, posType, entry, sl, tp);
   // An unrecognised comment (e.g. a manually-opened position sharing this
   // magic by mistake) is left untouched rather than guessed at.
  }

//+------------------------------------------------------------------+
//| PANEL                                                            |
//+------------------------------------------------------------------+
void DrawPanel()
  {
   double equity  = AccountInfoDouble(ACCOUNT_EQUITY);
   double dayPnl  = g_dayRealisedPnl;
   double dayPct  = (equity > 0.0) ? dayPnl / equity * 100.0 : 0.0;
   datetime now   = TimeCurrent();

   string posTxt = "flat";
   if(HasOpenPosition() && SelectOwnPosition())
      posTxt = StringFormat("open (%s)", posInfo.Comment());

   string pendingTxt = (g_rtPendingSide == 0) ? "none"
                      : StringFormat("%s @ %.2f (age %d/%d)",
                                     (g_rtPendingSide == 1 ? "support-test" : "resistance-test"),
                                     g_rtPendingLevel, g_rtPendingAge, InpRtRetestMaxBars);

   string txt = StringFormat(
      "123 M5  |  %s\n"
      "-----------------------------------------\n"
      "server %s   (GMT%+d)   GMT hour %02d\n"
      "windows  1:%02d-%02d  2:%02d-%02d  3:%02d-%02d GMT      in session: %s\n"
      "spread %.2f  (max %.2f = %d pts)\n"
      "sub-strategies  RT=%s BK=%s TR=%s MR=%s WK=%s\n"
      "RT pending breakout   %s\n"
      "WK profile   POC %.2f  VAH %.2f  VAL %.2f  (age %d/%d bars)\n"
      "-----------------------------------------\n"
      "equity      %.2f\n"
      "day P/L     %.2f  (%+.2f%%)  limit %.1f%%\n"
      "trades today %d / %d      loss streak %d / %d\n"
      "position    %s\n"
      "state       %s%s",
      _Symbol,
      TimeToString(now, TIME_DATE|TIME_MINUTES), g_gmtOffsetHrs, GmtHour(now),
      InpSession1Start, InpSession1End, InpSession2Start, InpSession2End,
      InpSession3Start, InpSession3End, (InSession(now) ? "yes" : "no"),
      CurrentSpread(), g_maxSpreadPrice, InpMaxSpreadPoints,
      (InpEnableRetest?"on":"off"), (InpEnableBreakout?"on":"off"), (InpEnableTrend?"on":"off"),
      (InpEnableMeanReversion?"on":"off"), (InpEnableWyckoff?"on":"off"),
      pendingTxt,
      g_wkPoc, g_wkVah, g_wkVal, g_wkBarsSinceProfile, InpWkProfileUpdateBars,
      equity,
      dayPnl, dayPct, InpMaxDailyLossPct,
      g_tradesToday, InpMaxTradesPerDay, g_consecLosses, InpMaxConsecLosses,
      posTxt,
      g_status,
      (g_halted ? ("\nHALTED: " + g_haltReason) : ""));

   Comment(txt);
  }
//+------------------------------------------------------------------+
