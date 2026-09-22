//+------------------------------------------------------------------+
//|                                                XauRetest_M5.mq5  |
//|        Breakout-then-retest continuation scalper for gold M5     |
//|                        XAUUSD - M5 - built for Exness MT5        |
//+------------------------------------------------------------------+
//| WHAT THIS IS
//| A third temperament, distinct from the other breakout-adjacent EAs here:
//|
//|   XauBreak enters IMMEDIATELY when a level breaks, betting the break is
//|   real. XauWyck enters AGAINST a break, betting it is a trap that snaps
//|   back. This one enters AFTER a break, once price has come back to prove
//|   the broken level and held -- former resistance acting as support (or the
//|   mirror). That is the pattern usually called "break and retest".
//|
//| Two stages, both required:
//|   1. BREAKOUT -- a decisive bar closes beyond a recent range by a clear
//|      margin. This arms a pending level and starts a clock.
//|   2. RETEST -- within a bounded number of bars, price comes back close to
//|      that level without closing back through it, and the bar that does so
//|      closes strongly back in the breakout direction. That bar is the entry.
//|
//| If price closes back through the level first, the breakout is void. If the
//| clock runs out first, the level is stale. Either way there is nothing left
//| to trade until the next fresh breakout.
//|
//| STATE ACROSS BARS
//| Unlike the other EAs here, the pending-breakout level/side/age is state
//| that persists across an unknown number of bars, not a fixed rolling
//| window. It lives in g_pendingLevel/g_pendingSide/g_pendingAge, advanced by
//| AdvancePendingBreakout() on every closed bar -- including bars where a
//| position is already open, exactly mirroring the Python side, which
//| computes this feature once for the whole series with no knowledge of the
//| engine's position state. TryEntry() reads a SNAPSHOT taken before that
//| bar's own advance (g_retestLevel/g_retestSide), so a bar can never retest
//| the very breakout it just made.
//|
//| HONEST LIMITATION: this state lives only in memory. A terminal or VPS
//| restart forgets a pending breakout that has not yet been retested. Given
//| InpRetestMaxBars is a modest window (bars, not days), the practical cost is
//| a handful of missed setups around a restart, not a wrong one taken.
//|
//| QUALITY SCORE, SIZE, NEVER A SECOND TRADE
//| A setup that clears every filter with room to spare is not the same as one
//| that barely qualifies, and a 0-1 quality score (breakout decisiveness +
//| ADX strength + retest precision + rejection strength, averaged so no one
//| of them can carry it alone) says how much better. That score scales the
//| reward:risk floor between InpQualityMinRR and InpQualityMaxRR, optionally
//| extends the target to a measured-move projection of the broken range's own
//| height, and scales CalcLots()'s risk fraction up to InpQualityMaxRiskMult.
//| It never opens a second position: this EA enforces one position at a time
//| by design (HasOpenPosition() below), the same rule as every other EA here,
//| and a signal this confident earns a bigger, farther-reaching version of
//| the ONE trade it is allowed to take -- not an unvalidated second one.
//|
//| Same invariants as the other EAs: closed bars only (bar 0 is still forming
//| and is never read), and stops are only ever tightened. Nothing
//| broker-specific is hardcoded.
//+------------------------------------------------------------------+
#property copyright "XauRetest"
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
input group "=== Risk (read this section first) ==="
input double InpRiskPercent        = 0.5;    // Risk per trade (% of equity)
input double InpMaxDailyLossPct    = 3.0;    // Daily loss limit (%). 0 = OFF
input int    InpMaxTradesPerDay    = 10;     // Max trades per day. 0 = unlimited
input int    InpMaxConsecLosses    = 6;      // Cool off after N losses in a row. 0 = OFF
input double InpMinStopDistance    = 0.30;   // Refuse stops tighter than this (price units)
input double InpMaxStopDistance    = 30.0;   // Refuse stops wider than this (price units)
input double InpFixedLots          = 0.0;    // >0 overrides risk sizing (NOT recommended)

input group "=== Session (hours are GMT/UTC, not server time) ==="
input ENUM_TZ_MODE InpTzMode       = TZ_AUTO; // How to resolve server time -> GMT
input int    InpServerGmtOffset    = 0;      // Server GMT offset when TZ_MANUAL
// Three separate windows, not one continuous block -- the quiet stretch of
// late-Asian/late-London hours between them is deliberately excluded, not a
// fourth window someone forgot. Set a window's Start == End to disable it.
//
// Window 1 (day open + early Asian) is OFF by default: a real Strategy
// Tester run (2026.09.01-17, XAUUSDm) broke down net P/L by window and found
// -23.53 from this one alone against +23.28 from window 3 over the same
// period -- removing it would have turned the whole run profitable. The
// user independently flagged the same pattern (a double-top reversal right
// at day open) across every version of this EA, not just this one run.
//
// Window 3 was WIDENED to 11-17 for one test to recover opportunity, and
// that widening was then DISPROVEN by the next real run on the same period:
// the untouched 12-16 core repeated the identical +23.28, while the four new
// trades the extra hours (11-12, 16-17) added netted -23.31, cancelling the
// entire gain from dropping window 1. Reverted to the proven 12-16 -- a
// window's edges are not provably good just because its core is.
input int    InpSession1Start      = 0;      // Window 1: day open + early Asian (OFF; wraps midnight if enabled)
input int    InpSession1End        = 0;
input int    InpSession2Start      = 8;      // Window 2: London open
input int    InpSession2End        = 10;
input int    InpSession3Start      = 12;     // Window 3: London/NY overlap (proven window; do not widen on a guess)
input int    InpSession3End        = 16;
input int    InpNoNewTradesAfter   = 24;     // No new entries from this GMT hour (24 = off)
input int    InpFlatByHour         = 24;     // Force flat at this GMT hour (24 = never)
input bool   InpTradeMonday        = true;
input bool   InpTradeTuesday       = true;
input bool   InpTradeWednesday     = true;
input bool   InpTradeThursday      = true;
input bool   InpTradeFriday        = true;

input group "=== Execution ==="
input long   InpMagicNumber        = 770633; // Identifies this EA's own trades.
                                             // MUST differ from XauTrend (770577), Anas
                                             // (770588), XauBreak (770599), XauMicro
                                             // (770611) and XauWyck (770622): each EA
                                             // manages only positions with its own magic,
                                             // else a shared number would have each bot
                                             // closing the others' trades.
input int    InpMaxSpreadPoints    = 500;    // Skip entries above this spread, IN POINTS (as MT5 shows it)
input int    InpSlippagePoints     = 30;     // Max deviation on market orders
input int    InpMaxBarsInTrade     = 24;     // Close a trade older than this (0 = off)

input group "=== Strategy: the level being broken ==="
input int    InpRangeLookback      = 20;     // Bars forming the range, excluding this one
input double InpBreakMarginAtr     = 0.10;   // Close must clear the level by this much
input int    InpAtrPeriod          = 14;
input double InpMinBarRangeAtr     = 0.50;   // The breakout bar must be decisive

input group "=== Strategy: regime ==="
input int    InpRegimeLookback     = 288;    // Bars for the median-ATR reference
input double InpAtrMinMult         = 0.55;   // Skip dead tape the spread would eat
input double InpAtrMaxMult         = 3.00;   // Skip post-news chaos
input int    InpAdxPeriod          = 14;
input double InpAdxMin             = 15.0;   // A continuation trade wants some trend behind it

input group "=== Strategy: the retest ==="
input int    InpRetestMaxBars      = 12;     // A level nobody retests in this long is stale
// Both tightened from an initial 0.25/0.55 after a live Strategy Tester run
// (2026.09.01-17, XAUUSDm, 100% real ticks) showed the dominant failure mode:
// 23 of 36 trades hit a real stop, many within 2-15 minutes of entry. See
// breakout_retest.py's BreakoutRetestParams for the full note.
input double InpRetestToleranceAtr = 0.15;   // How close price must come back, in ATR
input double InpRetestClosePosMin  = 0.68;   // The retest bar must reject the level cleanly
// A screenshot from the same run showed a second failure: a trade entered 35
// minutes (7 bars) after the prior one closed, buying right into the top of
// a fast rally that had already run past the level being "retested" -- the
// run's second-worst loss. InpCooldownBars = InpRetestMaxBars is deliberate,
// not arbitrary: a fresh setup gets the same minimum breathing room a retest
// itself is allowed.
input int    InpCooldownBars       = 12;     // Bars after ANY signal before the next is allowed

input group "=== Strategy: stop and target ==="
input double InpStopAtrBuffer      = 0.20;   // Beyond the retest bar's own extreme
input double InpMinStopAtrMult     = 1.00;   // Floor on stop distance (xATR)
input double InpMinTargetSpreadRatio = 6.0;  // Target must be >= N x the live spread

input group "=== Strategy: quality score (scales size and target, never the entry decision) ==="
input double InpQualityMinRR       = 1.50;   // Reward:risk at quality score 0 (a signal that barely passed)
input double InpQualityMaxRR       = 3.00;   // Reward:risk at quality score 1 (as strong as this gets)
input bool   InpUseMeasuredMove    = true;   // Also let the target reach the broken range's own height
input double InpQualityMaxRiskMult = 1.50;   // Position-size multiplier at quality score 1. NEVER a 2nd trade -- see file header.

input group "=== Strategy: in-trade management ==="
input bool   InpUseBreakeven       = true;
input double InpBreakevenAtR       = 1.0;    // Move to break-even at this R
input double InpBreakevenOffAtr    = 0.05;   // Lock in this much ATR beyond entry
input bool   InpUseTrail           = true;   // Beyond InpTrailAtR, follow price at InpTrailAtrMult x ATR
input double InpTrailAtR           = 1.5;    // Start trailing at this R (after break-even)
input double InpTrailAtrMult       = 1.20;   // Trailing distance behind price, in ATR

input group "=== Display ==="
input bool   InpShowPanel          = true;

//+------------------------------------------------------------------+
//| GLOBALS                                                          |
//+------------------------------------------------------------------+
int      hAtr     = INVALID_HANDLE;
int      hAdx     = INVALID_HANDLE;

datetime g_lastBarTime   = 0;
int      g_gmtOffsetHrs  = 0;

// --- per-day state
int      g_dayOfYear     = -1;
double   g_dayStartEquity= 0.0;
int      g_tradesToday   = 0;
double   g_dayRealisedPnl = 0.0;  // from closed deals, not equity
int      g_consecLosses  = 0;
bool     g_halted        = false;
string   g_haltReason    = "";

// --- pending-breakout state, advanced once per closed bar (see file header)
double   g_pendingLevel    = 0.0;
int      g_pendingSide     = 0;      // 0 none, 1 bullish (support test), -1 bearish (resistance test)
int      g_pendingAge      = 0;
double   g_pendingStrength = 0.0;    // breakout decisiveness (0-1), frozen at breakout time
double   g_pendingWidth    = 0.0;    // the broken range's own height, for the measured move
// --- snapshot taken before each bar's own advance; TryEntry() reads THIS
double   g_retestLevel     = 0.0;
int      g_retestSide      = 0;
double   g_retestStrength  = 0.0;
double   g_retestWidth     = 0.0;

// --- cooldown: the CLOSED-bar time whose raw conditions last passed (0 =
// never). Live trading processes each bar exactly once in order, so unlike
// the Python side's precomputed array (needed there because the causality
// audit calls entry() twice at the same bar), simple persistent state is
// safe and sufficient here.
datetime g_lastFireBarTime = 0;

// --- symbol spec, resolved once in OnInit
double   g_maxSpreadPrice = 0.0;  // InpMaxSpreadPoints converted to price units
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

   hAtr = iATR(_Symbol, PERIOD_CURRENT, InpAtrPeriod);
   hAdx = iADX(_Symbol, PERIOD_CURRENT, InpAdxPeriod);

   if(hAtr==INVALID_HANDLE || hAdx==INVALID_HANDLE)
     {
      Print("ERROR: failed to create one or more indicator handles.");
      return(INIT_FAILED);
     }

   trade.SetExpertMagicNumber(InpMagicNumber);
   trade.SetDeviationInPoints(InpSlippagePoints);
   // Picks FOK / IOC / RETURN according to what this symbol actually allows.
   // Guessing here is a common cause of "Unsupported filling mode" rejections.
   trade.SetTypeFillingBySymbol(_Symbol);

   g_gmtOffsetHrs = ResolveGmtOffset();
   ResetDailyState(true);

   PrintFormat("XauRetest M5 started on %s | server GMT offset %+d h | "
               "tick %.5f, tick value %.5f, lots %.2f-%.2f step %.2f | stops level %d pts",
               _Symbol, g_gmtOffsetHrs, g_tickSize, g_tickValue,
               g_volMin, g_volMax, g_volStep, g_stopsLevelPts);
   PrintSessionWindow("Window 1 (day open/Asian)", InpSession1Start, InpSession1End);
   PrintSessionWindow("Window 2 (London open)",    InpSession2Start, InpSession2End);
   PrintSessionWindow("Window 3 (London/NY)",       InpSession3Start, InpSession3End);

   // The spread limit is entered in POINTS because that is the unit MetaTrader
   // displays in Market Watch. What it means in money depends entirely on the
   // symbol's digits -- 500 points is 5.00 USD/oz on a 2-digit gold feed but
   // only 0.50 on a 3-digit one. Convert once here and print both, so the
   // setting can be sanity-checked at a glance instead of guessed at.
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

   return(INIT_SUCCEEDED);
  }

//+------------------------------------------------------------------+
void OnDeinit(const int reason)
  {
   IndicatorRelease(hAtr);
   IndicatorRelease(hAdx);
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
   if(InpQualityMinRR <= 0.0)        { Print("ERROR: QualityMinRR must be > 0"); return(false); }
   if(InpQualityMaxRR < InpQualityMinRR)
     { Print("ERROR: QualityMaxRR must be >= QualityMinRR"); return(false); }
   if(InpQualityMaxRiskMult < 1.0)   { Print("ERROR: QualityMaxRiskMult must be >= 1.0"); return(false); }
   if(InpRangeLookback < 2)          { Print("ERROR: RangeLookback must be >= 2"); return(false); }
   if(InpRetestMaxBars < 1)          { Print("ERROR: RetestMaxBars must be >= 1"); return(false); }
   if(InpRetestClosePosMin < 0.0 || InpRetestClosePosMin > 1.0)
     { Print("ERROR: RetestClosePosMin must be in [0, 1]"); return(false); }
   if(InpRegimeLookback < 20)        { Print("ERROR: RegimeLookback must be >= 20"); return(false); }
   if(InpMinStopDistance <= 0.0)     { Print("ERROR: MinStopDistance must be > 0"); return(false); }
   if(InpMinStopDistance >= InpMaxStopDistance)
     { Print("ERROR: MinStopDistance must be < MaxStopDistance"); return(false); }
   return(true);
  }

//+------------------------------------------------------------------+
//| Server clock -> GMT.                                             |
//|                                                                  |
//| Session hours are specified in GMT so that the EA trades the same|
//| hours as the backtest regardless of which server it runs on.     |
//| Broker server time is NOT GMT (it is commonly GMT+2/+3, and it   |
//| shifts with daylight saving), so this offset has to be applied   |
//| or the session filter silently slides by hours.                  |
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
   // Round to the nearest hour: the two clocks are never sampled at the same instant.
   return((int)MathRound((double)(srv - gmt) / 3600.0));
  }

//--- GMT hour/day derived from the server clock.
//--- The subtraction is done in signed 64-bit and only then cast back to
//--- datetime: casting a negative offset straight to datetime (servers west of
//--- Greenwich) is not well defined and shifts the session by a whole day.
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
   return(t.day_of_week); // 0 = Sunday
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

   // Snapshot the pending-breakout state as it stood BEFORE this bar, for
   // TryEntry() below. Must happen before AdvancePendingBreakout() touches it.
   g_retestLevel    = g_pendingLevel;
   g_retestSide     = g_pendingSide;
   g_retestStrength = g_pendingStrength;
   g_retestWidth    = g_pendingWidth;

   // Advance the pending-breakout state using the bar that just closed.
   // Unconditional -- runs whether or not a position is open, mirroring the
   // Python side's prepare(), which computes this feature for the whole
   // series with no knowledge of the engine's position state. A breakout that
   // occurs while a trade is open must still arm a retest for later.
   AdvancePendingBreakout();

   // 1. Manage an open position first. Protecting open risk always outranks
   //    looking for new risk.
   if(HasOpenPosition())
     {
      ManageOpenPosition(now);
      if(InpShowPanel) DrawPanel();
      return; // one position at a time, by design
     }

   // 2. Consider a new entry.
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
//| Fires once per completed bar.                                    |
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
   // Rolled on the GMT day, matching the session definition. Using the server
   // day instead would reset the daily loss limit part-way through a session
   // on any broker whose clock is not GMT.
   MqlDateTime t;
   TimeToStruct(ToGmt(TimeCurrent()), t);
   if(t.day_of_year != g_dayOfYear)
      ResetDailyState(false);
  }

//+------------------------------------------------------------------+
//| Count today's closed trades and the current losing streak from   |
//| deal history, so a terminal restart does not reset the breakers. |
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

   // The day's loss is summed from CLOSED DEALS, not from an equity
   // difference, and the limit is sized against LIVE equity rather than the
   // equity captured at the start of the day. Both details matter the moment
   // money moves in or out of the account: an equity difference counts a
   // deposit as profit and a withdrawal as loss, and a start-of-day baseline
   // goes stale. Deals only ever reflect trading, and live equity always
   // reflects the account as it is now.
   double equity = AccountInfoDouble(ACCOUNT_EQUITY);
   double base   = (equity > 0.0) ? equity : g_dayStartEquity;
   double limit  = -MathAbs(base * InpMaxDailyLossPct / 100.0);

   // A limit of zero means OFF, not "halt at zero loss". Without this guard the
   // obvious way to disable the rule does the opposite of what it looks like.
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
   return(false); // weekend
  }

//--- A window with Start == End is disabled, not "the whole day": treating
//--- equal bounds as 24 hours (as a single legacy window used to for 0/24)
//--- would make the obvious way to turn a window off do the opposite.
bool WindowContainsHour(int h, int start, int end)
  {
   if(start == end) return(false);
   if(start < end)  return(h >= start && h < end);
   return(h >= start || h < end); // wraps midnight
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

//--- Median of ATR over the regime window. MQL5 has no rolling median,
//--- and a median (not a mean) is used deliberately: it is not dragged
//--- around by the handful of news bars that dominate an average.
double MedianAtr(int lookback)
  {
   double buf[];
   if(!ReadBuffer(hAtr, 0, 1, lookback, buf))
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
//| SIZING                                                           |
//|                                                                  |
//| Lots are derived from the stop distance so that a stop-out costs  |
//| the same fraction of equity whatever the volatility. The result   |
//| is FLOORED to the lot step -- rounding up would risk more than    |
//| authorised on every trade.                                        |
//+------------------------------------------------------------------+
double CalcLots(double entry, double stop, double riskMult = 1.0)
  {
   if(InpFixedLots > 0.0)
      return(NormalizeLots(InpFixedLots));

   double distance = MathAbs(entry - stop);
   if(distance <= 0.0) return(0.0);

   double equity   = AccountInfoDouble(ACCOUNT_EQUITY);
   // riskMult scales this ONE trade's size by its quality score (see the file
   // header) -- it never opens a second position, so the daily-loss and
   // trades-per-day limits below still bound the worst case exactly as they
   // do for every other EA here.
   double riskCash = equity * InpRiskPercent / 100.0 * MathMax(riskMult, 0.0);

   // Loss for one lot if the stop is hit, in account currency. Derived from the
   // symbol's own tick value, so it is correct on Standard, Cent and Raw
   // accounts alike without knowing the contract size.
   double ticks       = distance / g_tickSize;
   double lossPerLot  = ticks * g_tickValue;
   if(lossPerLot <= 0.0) return(0.0);

   return(NormalizeLots(riskCash / lossPerLot));
  }

double NormalizeLots(double lots)
  {
   if(lots <= 0.0) return(0.0);
   lots = MathFloor(lots / g_volStep) * g_volStep;   // floor, never round
   lots = MathMin(lots, g_volMax);
   if(lots < g_volMin) return(0.0);
   // Guard against binary dust like 0.0999999999 turning into a rejected volume.
   return(NormalizeDouble(lots, 2));
  }

//+------------------------------------------------------------------+
//| Broker minimum distance for SL/TP, in price units.               |
//+------------------------------------------------------------------+
double MinStopOffset()
  {
   double point = SymbolInfoDouble(_Symbol, SYMBOL_POINT);
   int    pts   = MathMax(g_stopsLevelPts, g_freezeLevelPts);
   return(pts * point);
  }

//+------------------------------------------------------------------+
//| PENDING-BREAKOUT STATE (see file header)                         |
//+------------------------------------------------------------------+
void AdvancePendingBreakout()
  {
   MqlRates rates[];
   ArraySetAsSeries(rates, true);
   int wantBars = InpRangeLookback + 2;
   if(CopyRates(_Symbol, PERIOD_CURRENT, 1, wantBars, rates) != wantBars)
      return;

   double close = rates[0].close;

   //--- age out / invalidate using the bar that just closed
   if(g_pendingSide != 0)
     {
      g_pendingAge++;
      bool closedThrough = (g_pendingSide == 1 && close < g_pendingLevel)
                         || (g_pendingSide == -1 && close > g_pendingLevel);
      if(g_pendingAge > InpRetestMaxBars || closedThrough)
        {
         g_pendingSide     = 0;
         g_pendingLevel    = 0.0;
         g_pendingStrength = 0.0;
         g_pendingWidth    = 0.0;
        }
     }

   double atr[];
   if(!ReadBuffer(hAtr, 0, 1, 2, atr)) return;
   double atrV = atr[0];
   if(atrV <= 0.0) return;

   // rates[0] is the last CLOSED bar; the range is built from rates[1..N],
   // i.e. the bars BEFORE it -- same exclusion as XauBreak, for the same reason.
   double rangeHigh = rates[1].high, rangeLow = rates[1].low;
   for(int k = 1; k <= InpRangeLookback && k < wantBars; k++)
     {
      rangeHigh = MathMax(rangeHigh, rates[k].high);
      rangeLow  = MathMin(rangeLow,  rates[k].low);
     }

   double barRange = rates[0].high - rates[0].low;
   double closePos = (barRange > 0.0) ? (close - rates[0].low) / barRange : 0.5;
   double margin   = InpBreakMarginAtr * atrV;
   bool   decisive = barRange >= InpMinBarRangeAtr * atrV;

   bool brokeUp   = decisive && (close > rangeHigh + margin) && (closePos >= 0.5);
   bool brokeDown = decisive && (close < rangeLow  - margin) && ((1.0 - closePos) >= 0.5);

   // How far the breakout bar's own range exceeded the bare minimum required
   // to call it decisive, as a 0-1 fraction -- frozen for the eventual retest.
   double barStrength = MathMin(MathMax((barRange / atrV - InpMinBarRangeAtr) / InpMinBarRangeAtr, 0.0), 1.0);

   if(brokeUp && !brokeDown)
     {
      g_pendingSide     = 1;
      g_pendingLevel    = rangeHigh;
      g_pendingAge      = 0;
      g_pendingStrength = barStrength;
      g_pendingWidth    = rangeHigh - rangeLow;
     }
   else if(brokeDown && !brokeUp)
     {
      g_pendingSide     = -1;
      g_pendingLevel    = rangeLow;
      g_pendingAge      = 0;
      g_pendingStrength = barStrength;
      g_pendingWidth    = rangeHigh - rangeLow;
     }
  }

//+------------------------------------------------------------------+
//| ENTRY                                                            |
//+------------------------------------------------------------------+
void TryEntry()
  {
   int need = MathMax(InpRegimeLookback, InpRangeLookback) + InpRetestMaxBars + 10;
   if(Bars(_Symbol, PERIOD_CURRENT) < need)
     {
      g_status = StringFormat("warming up (%d/%d bars)", Bars(_Symbol, PERIOD_CURRENT), need);
      return;
     }

   //--- TryEntry reads the SNAPSHOT taken before this bar's own advance, never
   //--- the pending state that AdvancePendingBreakout() just mutated -- see
   //--- the file header.
   if(g_retestSide == 0)
     {
      g_status = "no pending breakout to retest";
      return;
     }

   datetime thisBarTime = iTime(_Symbol, PERIOD_CURRENT, 1);   // the last CLOSED bar
   if(g_lastFireBarTime != 0)
     {
      int barsSinceFire = iBarShift(_Symbol, PERIOD_CURRENT, g_lastFireBarTime, false);
      if(barsSinceFire < InpCooldownBars)
        {
         g_status = StringFormat("cooldown (%d/%d bars)", barsSinceFire, InpCooldownBars);
         return;
        }
     }

   double atr[], adx[];
   if(!ReadBuffer(hAtr, 0, 1, 2, atr) || !ReadBuffer(hAdx, 0, 1, 2, adx))
     {
      g_status = "indicator data not ready";
      return;
     }

   MqlRates rates[];
   ArraySetAsSeries(rates, true);
   if(CopyRates(_Symbol, PERIOD_CURRENT, 1, 2, rates) != 2)
     {
      g_status = "price data not ready";
      return;
     }

   double close = rates[0].close, high_ = rates[0].high, low_ = rates[0].low;
   double atrV = atr[0], adxV = adx[0];
   if(atrV <= 0.0) { g_status = "ATR unavailable"; return; }

   double medAtr = MedianAtr(InpRegimeLookback);
   if(medAtr <= 0.0) { g_status = "regime ATR unavailable"; return; }
   if(atrV < InpAtrMinMult * medAtr) { g_status = "volatility too low";  return; }
   if(atrV > InpAtrMaxMult * medAtr) { g_status = "volatility too high"; return; }
   if(InpAdxMin > 0.0 && adxV < InpAdxMin)
     {
      g_status = StringFormat("ADX %.1f < %.1f", adxV, InpAdxMin);
      return;
     }

   double barRange = high_ - low_;
   double closePos = (barRange > 0.0) ? (close - low_) / barRange : 0.5;
   double tol       = InpRetestToleranceAtr * atrV;
   double minOff    = MinStopOffset();
   double sl, tp, price;

   double quality, riskMult;

   if(g_retestSide == 1)
     {
      //--- former resistance, now expected to hold as support
      bool retestOk = (low_ <= g_retestLevel + tol) && (close > g_retestLevel)
                      && (closePos >= InpRetestClosePosMin);
      if(!retestOk) { g_status = "no valid retest (support)"; return; }
      g_lastFireBarTime = thisBarTime;

      double precision = (tol > 0.0) ? (1.0 - MathAbs(low_ - g_retestLevel) / tol) : 0.5;
      double rejection = (InpRetestClosePosMin < 1.0)
                          ? (closePos - InpRetestClosePosMin) / (1.0 - InpRetestClosePosMin)
                          : 1.0;
      quality = QualityScore(g_retestStrength, adxV, precision, rejection);

      price = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
      sl    = MathMin(low_ - InpStopAtrBuffer * atrV, close - InpMinStopAtrMult * atrV);
      if(sl >= price - minOff) sl = price - MathMax(minOff, InpMinStopDistance);
      double rr     = InpQualityMinRR + quality * (InpQualityMaxRR - InpQualityMinRR);
      double tpDist = rr * (price - sl);
      if(InpUseMeasuredMove && g_retestWidth > 0.0) tpDist = MathMax(tpDist, g_retestWidth);
      tp       = price + tpDist;
      riskMult = 1.0 + quality * (InpQualityMaxRiskMult - 1.0);
      OpenTrade(ORDER_TYPE_BUY, price, sl, tp, atrV, riskMult, quality);
     }
   else
     {
      //--- former support, now expected to hold as resistance
      bool retestOk = (high_ >= g_retestLevel - tol) && (close < g_retestLevel)
                      && ((1.0 - closePos) >= InpRetestClosePosMin);
      if(!retestOk) { g_status = "no valid retest (resistance)"; return; }
      g_lastFireBarTime = thisBarTime;

      double precision = (tol > 0.0) ? (1.0 - MathAbs(high_ - g_retestLevel) / tol) : 0.5;
      double rejection = (InpRetestClosePosMin < 1.0)
                          ? ((1.0 - closePos) - InpRetestClosePosMin) / (1.0 - InpRetestClosePosMin)
                          : 1.0;
      quality = QualityScore(g_retestStrength, adxV, precision, rejection);

      price = SymbolInfoDouble(_Symbol, SYMBOL_BID);
      sl    = MathMax(high_ + InpStopAtrBuffer * atrV, close + InpMinStopAtrMult * atrV);
      if(sl <= price + minOff) sl = price + MathMax(minOff, InpMinStopDistance);
      double rr     = InpQualityMinRR + quality * (InpQualityMaxRR - InpQualityMinRR);
      double tpDist = rr * (sl - price);
      if(InpUseMeasuredMove && g_retestWidth > 0.0) tpDist = MathMax(tpDist, g_retestWidth);
      tp       = price - tpDist;
      riskMult = 1.0 + quality * (InpQualityMaxRiskMult - 1.0);
      OpenTrade(ORDER_TYPE_SELL, price, sl, tp, atrV, riskMult, quality);
     }
  }

//+------------------------------------------------------------------+
//| Quality score (0-1): breakout decisiveness + ADX strength + retest|
//| precision + rejection strength, averaged so no one signal alone   |
//| can push it near 1.0. See the file header for what this scales.   |
//+------------------------------------------------------------------+
double Clamp01(double x)
  {
   if(x < 0.0) return(0.0);
   if(x > 1.0) return(1.0);
   return(x);
  }

double QualityScore(double breakoutStrength, double adxV, double precision, double rejection)
  {
   double adxComponent = (InpAdxMin > 0.0 && MathIsValidNumber(adxV))
                          ? Clamp01((adxV - InpAdxMin) / InpAdxMin)
                          : 0.5;  // filter is off: no gradient to read, stay neutral
   double bs = MathIsValidNumber(breakoutStrength) ? Clamp01(breakoutStrength) : 0.5;
   return((bs + adxComponent + Clamp01(precision) + Clamp01(rejection)) / 4.0);
  }

//+------------------------------------------------------------------+
void OpenTrade(ENUM_ORDER_TYPE type, double price, double sl, double tp, double atrV,
                double riskMult, double quality)
  {
   double distance = MathAbs(price - sl);

   if(distance < InpMinStopDistance)
     {
      g_status = StringFormat("stop too tight (%.2f)", distance);
      return;
     }
   if(distance > InpMaxStopDistance)
     {
      g_status = StringFormat("stop too wide (%.2f)", distance);
      return;
     }

   // A scalper pays the spread on every trade against a modest target. Refuse
   // any setup where the target is not worth several times what it costs to
   // open.
   double targetDistance = MathAbs(tp - price);
   double spreadNow      = CurrentSpread();
   if(InpMinTargetSpreadRatio > 0.0 && spreadNow > 0.0 &&
      targetDistance < InpMinTargetSpreadRatio * spreadNow)
     {
      g_status = StringFormat("target %.2f < %.1fx spread %.2f",
                              targetDistance, InpMinTargetSpreadRatio, spreadNow);
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

   bool ok = (type == ORDER_TYPE_BUY)
             ? trade.Buy(lots, _Symbol, 0.0, sl, tp, "XauRetest")
             : trade.Sell(lots, _Symbol, 0.0, sl, tp, "XauRetest");

   if(ok)
     {
      g_status = StringFormat("opened %s %.2f lots (quality %.2f)", (type==ORDER_TYPE_BUY ? "BUY":"SELL"), lots, quality);
      PrintFormat("%s %.2f lots @ ~%.2f  sl=%.2f tp=%.2f  risk=%.2f%% x%.2f  quality=%.2f  ATR=%.2f  spread=%.2f",
                  (type==ORDER_TYPE_BUY ? "BUY":"SELL"), lots, price, sl, tp,
                  InpRiskPercent, riskMult, quality, atrV, CurrentSpread());
     }
   else
     {
      g_status = StringFormat("order failed: %d %s", trade.ResultRetcode(), trade.ResultRetcodeDescription());
      PrintFormat("ORDER FAILED retcode=%d (%s) lots=%.2f sl=%.2f tp=%.2f",
                  trade.ResultRetcode(), trade.ResultRetcodeDescription(), lots, sl, tp);
     }
  }

//+------------------------------------------------------------------+
//| MANAGEMENT: break-even, time stop, session flat                  |
//+------------------------------------------------------------------+
void ManageOpenPosition(datetime now)
  {
   if(!SelectOwnPosition()) return;

   ulong  ticket = posInfo.Ticket();
   long   pos_type = posInfo.PositionType();
   double entry  = posInfo.PriceOpen();
   double sl     = posInfo.StopLoss();
   double tp     = posInfo.TakeProfit();

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

   //--- time stop
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

   double atrBuf[];
   if(!ReadBuffer(hAtr, 0, 1, 2, atrBuf)) return;
   double atrV = atrBuf[0];
   if(atrV <= 0.0) return;

   double close  = iClose(_Symbol, PERIOD_CURRENT, 1);   // last CLOSED bar
   double risk   = MathAbs(entry - sl);
   if(risk <= 0.0) return;

   double newSl = sl;

   if(pos_type == POSITION_TYPE_BUY)
     {
      double gainedR = (close - entry) / risk;
      if(InpUseBreakeven && gainedR >= InpBreakevenAtR)
         newSl = MathMax(newSl, entry + InpBreakevenOffAtr * atrV);
      // Past InpTrailAtR the stop follows price instead of sitting still at
      // break-even -- a trade up 2R that gives it ALL back to break-even is
      // a scratch, not a win, and this is what turns that into a partial win.
      if(InpUseTrail && gainedR >= InpTrailAtR)
         newSl = MathMax(newSl, close - InpTrailAtrMult * atrV);
      double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);
      newSl = MathMin(newSl, bid - MinStopOffset());
      if(newSl <= sl) return;                 // tighten only
     }
   else
     {
      double gainedR = (entry - close) / risk;
      if(InpUseBreakeven && gainedR >= InpBreakevenAtR)
         newSl = MathMin(newSl, entry - InpBreakevenOffAtr * atrV);
      if(InpUseTrail && gainedR >= InpTrailAtR)
         newSl = MathMin(newSl, close + InpTrailAtrMult * atrV);
      double ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
      newSl = MathMax(newSl, ask + MinStopOffset());
      if(newSl >= sl) return;                 // tighten only
     }

   newSl = NormalizeDouble(newSl, _Digits);
   if(MathAbs(newSl - sl) < g_tickSize) return;   // not worth a server round trip

   if(trade.PositionModify(ticket, newSl, tp))
      g_status = StringFormat("stop -> %.2f", newSl);
   else
      PrintFormat("PositionModify #%I64u failed: %d (%s)",
                  ticket, trade.ResultRetcode(), trade.ResultRetcodeDescription());
  }

//+------------------------------------------------------------------+
//| PANEL                                                            |
//+------------------------------------------------------------------+
void DrawPanel()
  {
   double equity  = AccountInfoDouble(ACCOUNT_EQUITY);
   double dayPnl  = g_dayRealisedPnl;                       // closed trades only
   double dayPct  = (equity > 0.0) ? dayPnl / equity * 100.0 : 0.0;
   datetime now   = TimeCurrent();

   string pendingTxt = (g_pendingSide == 0) ? "none"
                      : StringFormat("%s @ %.2f (age %d/%d, strength %.2f)",
                                     (g_pendingSide == 1 ? "support-test" : "resistance-test"),
                                     g_pendingLevel, g_pendingAge, InpRetestMaxBars, g_pendingStrength);

   int h = GmtHour(now);
   string txt = StringFormat(
      "XauRetest M5  |  %s\n"
      "-----------------------------------------\n"
      "server %s   (GMT%+d)   GMT hour %02d\n"
      "windows  1:%02d-%02d  2:%02d-%02d  3:%02d-%02d GMT      in session: %s\n"
      "spread %.2f  (max %.2f = %d pts)\n"
      "pending breakout   %s\n"
      "-----------------------------------------\n"
      "equity      %.2f\n"
      "day P/L     %.2f  (%+.2f%%)  limit %.1f%%\n"
      "trades today %d / %d      loss streak %d / %d\n"
      "position    %s\n"
      "state       %s%s",
      _Symbol,
      TimeToString(now, TIME_DATE|TIME_MINUTES), g_gmtOffsetHrs, h,
      InpSession1Start, InpSession1End, InpSession2Start, InpSession2End,
      InpSession3Start, InpSession3End, (InSession(now) ? "yes" : "no"),
      CurrentSpread(), g_maxSpreadPrice, InpMaxSpreadPoints,
      pendingTxt,
      equity,
      dayPnl, dayPct, InpMaxDailyLossPct,
      g_tradesToday, InpMaxTradesPerDay, g_consecLosses, InpMaxConsecLosses,
      (HasOpenPosition() ? "open" : "flat"),
      g_status,
      (g_halted ? ("\nHALTED: " + g_haltReason) : ""));

   Comment(txt);
  }
//+------------------------------------------------------------------+
