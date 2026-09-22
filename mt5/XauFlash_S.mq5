//+------------------------------------------------------------------+
//|                                                  XauFlash_S.mq5  |
//|        Tick-momentum scalper: in and out within seconds          |
//|                 XAUUSD - any chart - built for Exness MT5        |
//+------------------------------------------------------------------+
//| WHAT THIS IS
//| The other EAs in this folder decide once per closed M5 bar. This one
//| decides on every tick. It watches the last few seconds of price and
//| enters when gold makes a sharp, one-way burst, then gets out within
//| seconds: at the target, at the stop, or when the hold timer runs out.
//|
//|   entry : bid moved >= BurstMinMove within the last BurstWindowMs,
//|           on at least BurstMinTicks ticks, mostly in one direction,
//|           and the move is several times the live spread
//|   exit  : take-profit or stop-loss (both sent to the SERVER with the
//|           order, so they hold even if this terminal freezes), or
//|           MaxHoldSeconds elapsed -> close at market
//|
//| READ THIS BEFORE RUNNING IT ON REAL MONEY
//| A trade that lasts seconds cannot make much, and it pays the full spread
//| every time. On a 0.25 spread and a 1.00 target, a quarter of every win is
//| gone before the trade starts. Speed makes that worse, not better:
//|
//|   * Slippage. Market orders during a burst fill worse than the quote you
//|     saw. The EA logs every fill against its quote; watch that number.
//|   * Latency. From a home PC the round trip to the broker is often 50-200
//|     ms, which is a large part of a 3-second signal. Run it on a VPS near
//|     the broker's server.
//|   * Backtests. The Strategy Tester MUST use "Every tick based on real
//|     ticks". "1 minute OHLC" or "Open prices only" invent the ticks this
//|     EA trades on, and the result is fiction.
//|   * Broker rules. Some brokers and most prop firms forbid or penalise
//|     trades held under a minimum time (often 60 s). Check yours.
//|
//| Demo first. If the demo is not profitable after spread and slippage over
//| a few hundred trades, the live account will not be either.
//+------------------------------------------------------------------+
#property copyright "XauFlash"
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
input double InpRiskPercent        = 0.5;    // Risk per trade (%) if the stop is hit
input double InpMaxDailyLossPct    = 3.0;    // Daily loss limit (%). 0 = OFF
input int    InpMaxTradesPerDay    = 40;     // Max trades per day. 0 = unlimited
input int    InpMaxConsecLosses    = 5;      // Stop for the day after N losses in a row. 0 = OFF
input double InpFixedLots          = 0.0;    // >0 overrides risk sizing (e.g. 0.01)

input group "=== Session (hours are GMT/UTC, not server time) ==="
input ENUM_TZ_MODE InpTzMode       = TZ_AUTO; // How to resolve server time -> GMT
input int    InpServerGmtOffset    = 0;      // Server GMT offset when TZ_MANUAL
input int    InpSessionStartHour   = 7;      // London open. Thin Asian tape = wide spread
input int    InpSessionEndHour     = 20;     // NY afternoon
input bool   InpTradeMonday        = true;
input bool   InpTradeTuesday       = true;
input bool   InpTradeWednesday     = true;
input bool   InpTradeThursday      = true;
input bool   InpTradeFriday        = true;

input group "=== Execution ==="
input long   InpMagicNumber        = 770633; // Identifies this EA's own trades.
                                             // MUST differ from the other EAs:
                                             // 770577 / 770588 / 770599 / 770611 / 770622.
input double InpMaxSpread          = 0.30;   // Skip entries above this spread, in USD/oz (not points)
input int    InpSlippagePoints     = 50;     // Max deviation on market orders (points)
input int    InpTimerMs            = 200;    // How often the hold timer is checked (ms)

input group "=== Signal: the burst ==="
input int    InpBurstWindowMs      = 3000;   // Look back this many milliseconds
input double InpBurstMinMove       = 0.60;   // Bid must move at least this much (USD/oz) in the window
input int    InpBurstMinTicks      = 6;      // ...on at least this many ticks (a real burst, not one gap)
input double InpBurstDirectional   = 0.70;   // Share of tick-to-tick steps in the burst's direction
input double InpBurstSpreadMult    = 3.0;    // The move must be >= N x the live spread

input group "=== Trade: seconds in, seconds out ==="
input double InpTakeProfit         = 0.80;   // Target distance (USD/oz)
input double InpStopLoss           = 0.80;   // Stop distance (USD/oz)
input int    InpMaxHoldSeconds     = 15;     // Close at market after this many seconds
input int    InpCooldownSeconds    = 20;     // Wait this long after a close before the next entry
input double InpMinTargetSpreadRatio = 3.0;  // Target must be >= N x the live spread

input group "=== Display ==="
input bool   InpShowPanel          = true;

//+------------------------------------------------------------------+
//| GLOBALS                                                          |
//+------------------------------------------------------------------+
#define TICK_BUF 512

// Ring buffer of recent ticks: time in ms and bid.
long     g_tickMs[TICK_BUF];
double   g_tickBid[TICK_BUF];
int      g_tickHead      = 0;   // next write slot
int      g_tickCount     = 0;   // valid entries, <= TICK_BUF

int      g_gmtOffsetHrs  = 0;

// --- per-day state
int      g_dayOfYear     = -1;
double   g_dayStartEquity= 0.0;
int      g_tradesToday   = 0;
double   g_dayRealisedPnl= 0.0;
int      g_consecLosses  = 0;
bool     g_halted        = false;
string   g_haltReason    = "";
datetime g_lastCloseTime = 0;
datetime g_lastHistoryScan = 0;

// --- symbol spec, resolved once in OnInit
double   g_maxSpreadPrice= 0.0;
double   g_tickSize      = 0.0;
double   g_tickValue     = 0.0;
double   g_volMin        = 0.0;
double   g_volMax        = 0.0;
double   g_volStep       = 0.0;
int      g_stopsLevelPts = 0;
int      g_freezeLevelPts= 0;

// --- running execution-quality stats, since the EA was attached
int      g_fills         = 0;
double   g_slipSum       = 0.0;  // positive = filled worse than quoted
int      g_timerExits    = 0;    // closed by the hold timer, not SL/TP

string   g_status        = "starting";

// Why entries were skipped, so "no trades" always comes with a reason.
// Counted per tick and printed once a day and when the EA stops.
enum ENUM_BLOCK
  {
   BLK_HALTED = 0, BLK_DAY, BLK_SESSION, BLK_MAXTRADES, BLK_COOLDOWN, BLK_SPREAD,
   BLK_AUTOTRADING, BLK_NO_BURST, BLK_BURST_VS_SPREAD, BLK_TARGET_VS_SPREAD,
   BLK_SIZE, BLK_ORDER_FAILED, BLK_COUNT
  };
// Sized by a literal (12 = BLK_COUNT): an enum value as an array bound is
// not worth the risk in a file that cannot be test-compiled here.
string   g_blockName[12] = {"halted", "not a trading day", "outside session",
                                   "max trades/day", "cooldown", "spread too wide",
                                   "AutoTrading off", "no burst", "burst < N x spread",
                                   "target < N x spread", "lot size below minimum",
                                   "order rejected"};
long     g_blockCount[12];
double   g_maxBurstSeen  = 0.0;   // largest |move| in the window, since the last report
double   g_minSpreadSeen = 0.0;
double   g_maxSpreadSeen = 0.0;
int      g_entriesSinceReport = 0;

// False in a non-visual Strategy Tester run. Building the panel text on every
// tick is harmless live but, over the millions of real ticks a tester run
// replays, it is most of the run time -- and nobody can see it anyway.
bool     g_panel         = true;

//+------------------------------------------------------------------+
//| INIT                                                             |
//+------------------------------------------------------------------+
int OnInit()
  {
   if(!ResolveSymbolSpec())
      return(INIT_FAILED);
   if(!ValidateInputs())
      return(INIT_FAILED);

   trade.SetExpertMagicNumber(InpMagicNumber);
   trade.SetDeviationInPoints(InpSlippagePoints);
   // Picks FOK / IOC / RETURN according to what this symbol actually allows.
   trade.SetTypeFillingBySymbol(_Symbol);

   g_gmtOffsetHrs = ResolveGmtOffset();
   ResetDailyState(true);

   bool tester = (bool)MQLInfoInteger(MQL_TESTER);
   bool visual = (bool)MQLInfoInteger(MQL_VISUAL_MODE);
   g_panel = InpShowPanel && (!tester || visual);

   // The hold timer must fire even when no ticks arrive, or a 15-second trade
   // can sit open for a minute in a quiet patch. In the tester a 200 ms timer
   // means 432,000 simulated events a day; 1 s is accurate enough there, since
   // real-tick data already calls OnTick several times a second.
   int timerMs = tester ? 1000 : MathMax(50, InpTimerMs);
   if(!EventSetMillisecondTimer(timerMs))
     {
      Print("ERROR: could not start the millisecond timer.");
      return(INIT_FAILED);
     }

   // Entered in USD/oz, not points. A points limit means 0.35 USD on a
   // 2-digit gold feed and 0.035 on a 3-digit one (Exness XAUUSDm) -- which
   // is narrower than any real spread and silently blocks every entry.
   g_maxSpreadPrice = InpMaxSpread;
   ArrayInitialize(g_blockCount, 0);

   PrintFormat("XauFlash started on %s | server GMT offset %+d h | tick %.5f, tick value %.5f, "
               "lots %.2f-%.2f step %.2f | stops level %d pts",
               _Symbol, g_gmtOffsetHrs, g_tickSize, g_tickValue,
               g_volMin, g_volMax, g_volStep, g_stopsLevelPts);
   PrintFormat("Session %02d:00-%02d:00 GMT  =  %02d:00-%02d:00 server time",
               InpSessionStartHour, InpSessionEndHour,
               (InpSessionStartHour + g_gmtOffsetHrs + 24) % 24,
               (InpSessionEndHour   + g_gmtOffsetHrs + 24) % 24);
   PrintFormat("Max spread: %.2f USD/oz (symbol digits %d, current spread %.3f)",
               g_maxSpreadPrice, (int)_Digits, CurrentSpread());
   PrintFormat("Signal: >= %.2f move in %d ms on >= %d ticks | TP %.2f  SL %.2f  hold <= %d s",
               InpBurstMinMove, InpBurstWindowMs, InpBurstMinTicks,
               InpTakeProfit, InpStopLoss, InpMaxHoldSeconds);

   double minOff = MinStopOffset();
   if(minOff > 0.0 && (InpStopLoss < minOff || InpTakeProfit < minOff))
      PrintFormat("WARNING: the broker's minimum stop distance is %.2f; SL/TP below that "
                  "will be widened to it.", minOff);

   // Say plainly if the account is too small to size a trade at this risk,
   // instead of leaving the user to wonder why nothing ever opens.
   if(InpFixedLots <= 0.0)
     {
      double eq0     = AccountInfoDouble(ACCOUNT_EQUITY);
      double budget0 = eq0 * InpRiskPercent / 100.0;
      double minLoss = MathMax(InpStopLoss, minOff) / g_tickSize * g_tickValue * g_volMin;
      if(minLoss > budget0)
         PrintFormat("WARNING: NO TRADE CAN OPEN. The smallest lot (%.2f) loses %.2f at the stop, "
                     "but %.2f%% of %.2f equity is only %.2f. Raise RiskPercent, lower StopLoss, "
                     "or set FixedLots = %.2f.",
                     g_volMin, minLoss, InpRiskPercent, eq0, budget0, g_volMin);
     }

   if(tester)
      Print("TESTER: this EA is only meaningful with 'Every tick based on real ticks'. "
            "Any other modelling mode generates synthetic ticks and the result is fiction.");

   if(InpMaxDailyLossPct <= 0.0 || InpMaxConsecLosses <= 0 || InpMaxTradesPerDay <= 0)
      PrintFormat("RISK GUARDS DISABLED -> daily loss: %s | loss streak: %s | trades/day: %s.",
                  (InpMaxDailyLossPct <= 0.0 ? "OFF" : "on"),
                  (InpMaxConsecLosses <= 0   ? "OFF" : "on"),
                  (InpMaxTradesPerDay <= 0   ? "OFF" : "on"));

   return(INIT_SUCCEEDED);
  }

//+------------------------------------------------------------------+
void OnDeinit(const int reason)
  {
   EventKillTimer();
   ReportBlocks("final");
   if(g_fills > 0)
      PrintFormat("XauFlash stopped. %d fills, average slippage %.3f USD/oz, %d closed by the hold timer.",
                  g_fills, g_slipSum / g_fills, g_timerExits);
   Comment("");
  }

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
   if(InpRiskPercent <= 0.0 || InpRiskPercent > 2.0)
     { Print("ERROR: RiskPercent must be in (0, 2]. A scalper takes many trades a day."); return(false); }
   if(InpTakeProfit <= 0.0 || InpStopLoss <= 0.0)
     { Print("ERROR: TakeProfit and StopLoss must be > 0."); return(false); }
   if(InpMaxHoldSeconds < 1)
     { Print("ERROR: MaxHoldSeconds must be >= 1."); return(false); }
   if(InpBurstWindowMs < 200 || InpBurstWindowMs > 60000)
     { Print("ERROR: BurstWindowMs must be in [200, 60000]."); return(false); }
   if(InpBurstMinTicks < 2)
     { Print("ERROR: BurstMinTicks must be >= 2."); return(false); }
   if(InpBurstMinMove <= 0.0)
     { Print("ERROR: BurstMinMove must be > 0."); return(false); }
   if(InpBurstDirectional < 0.5 || InpBurstDirectional > 1.0)
     { Print("ERROR: BurstDirectional must be in [0.5, 1]."); return(false); }
   if(InpSessionStartHour < 0 || InpSessionStartHour > 23 ||
      InpSessionEndHour   < 1 || InpSessionEndHour   > 24)
     { Print("ERROR: session hours out of range."); return(false); }
   return(true);
  }

//+------------------------------------------------------------------+
//| Server clock -> GMT (same approach as the M5 EAs)                |
//+------------------------------------------------------------------+
int ResolveGmtOffset()
  {
   if(InpTzMode == TZ_MANUAL)
      return(InpServerGmtOffset);

   datetime srv = TimeCurrent();
   datetime gmt = TimeGMT();
   if(srv <= 0 || gmt <= 0)
     {
      Print("WARNING: could not read server/GMT time; assuming GMT+0. "
            "Set TzMode=TZ_MANUAL if the session hours look wrong.");
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

//+------------------------------------------------------------------+
//| MAIN LOOP -- every tick                                          |
//+------------------------------------------------------------------+
void OnTick()
  {
   MqlTick tk;
   if(!SymbolInfoTick(_Symbol, tk))
      return;
   RecordTick(tk);

   RollDailyStateIfNeeded();
   ScanHistoryIfNeeded();

   datetime now = TimeCurrent();

   // One position at a time. While it is open, the only job is getting out.
   if(HasOpenPosition())
     {
      ManageOpenPosition(now);
      if(g_panel) DrawPanel();
      return;
     }

   string why = "";
   if(!MayOpen(now, why))
     {
      g_status = "no entry: " + why;
      if(g_panel) DrawPanel();
      return;
     }

   TryEntry(tk);
   if(g_panel) DrawPanel();
  }

//+------------------------------------------------------------------+
//| Timer: enforces the hold limit between ticks.                    |
//+------------------------------------------------------------------+
void OnTimer()
  {
   if(HasOpenPosition())
      ManageOpenPosition(TimeCurrent());
  }

//+------------------------------------------------------------------+
//| Fires when our position is closed by SL/TP/timer, so the history |
//| is re-read straight away instead of on the next scan.            |
//+------------------------------------------------------------------+
void OnTradeTransaction(const MqlTradeTransaction &trans,
                        const MqlTradeRequest &request,
                        const MqlTradeResult &result)
  {
   if(trans.type != TRADE_TRANSACTION_DEAL_ADD) return;
   if(trans.symbol != _Symbol) return;
   if(!HistoryDealSelect(trans.deal)) return;
   if(HistoryDealGetInteger(trans.deal, DEAL_MAGIC) != InpMagicNumber) return;

   long entry = HistoryDealGetInteger(trans.deal, DEAL_ENTRY);
   if(entry == DEAL_ENTRY_OUT || entry == DEAL_ENTRY_OUT_BY)
     {
      g_lastCloseTime   = TimeCurrent();
      g_lastHistoryScan = 0;           // force a rescan on the next tick
     }
  }

//+------------------------------------------------------------------+
//| TICK BUFFER                                                      |
//+------------------------------------------------------------------+
void RecordTick(const MqlTick &tk)
  {
   if(tk.bid <= 0.0) return;
   long ms = (long)tk.time_msc;
   if(ms <= 0) ms = (long)tk.time * 1000;

   // Ignore ticks that did not change the bid; they carry no momentum and
   // would let a frozen quote pass the tick-count filter.
   if(g_tickCount > 0)
     {
      int last = (g_tickHead - 1 + TICK_BUF) % TICK_BUF;
      if(g_tickBid[last] == tk.bid) return;
     }

   g_tickMs[g_tickHead]  = ms;
   g_tickBid[g_tickHead] = tk.bid;
   g_tickHead = (g_tickHead + 1) % TICK_BUF;
   if(g_tickCount < TICK_BUF) g_tickCount++;
  }

//+------------------------------------------------------------------+
//| Burst detection over the last InpBurstWindowMs.                  |
//| Returns +1 (up burst), -1 (down burst) or 0, and the move size.  |
//+------------------------------------------------------------------+
int DetectBurst(long nowMs, double &move, int &ticks, double &dirShare)
  {
   move = 0.0; ticks = 0; dirShare = 0.0;
   if(g_tickCount < 2) return(0);

   int newest = (g_tickHead - 1 + TICK_BUF) % TICK_BUF;
   double last = g_tickBid[newest];

   // Walk back from the newest tick until we leave the window.
   int    oldest = newest;
   int    ups = 0, downs = 0, n = 1;
   int    idx = newest;
   for(int k = 1; k < g_tickCount; k++)
     {
      int prev = (idx - 1 + TICK_BUF) % TICK_BUF;
      if(nowMs - g_tickMs[prev] > InpBurstWindowMs) break;
      if(g_tickBid[idx] > g_tickBid[prev]) ups++;
      else if(g_tickBid[idx] < g_tickBid[prev]) downs++;
      oldest = prev;
      idx = prev;
      n++;
     }

   ticks = n;
   move  = last - g_tickBid[oldest];
   int steps = ups + downs;
   if(steps == 0) return(0);

   if(move > 0.0) dirShare = (double)ups / steps;
   else           dirShare = (double)downs / steps;

   if(ticks < InpBurstMinTicks)            return(0);
   if(MathAbs(move) < InpBurstMinMove)     return(0);
   if(dirShare < InpBurstDirectional)      return(0);
   return(move > 0.0 ? 1 : -1);
  }

//+------------------------------------------------------------------+
//| Why nothing happened: counts of every blocked entry, by reason.  |
//+------------------------------------------------------------------+
void Block(ENUM_BLOCK b)
  {
   g_blockCount[b]++;
  }

void ReportBlocks(string label)
  {
   long total = 0;
   for(int i = 0; i < BLK_COUNT; i++) total += g_blockCount[i];
   if(total == 0 && g_entriesSinceReport == 0) return;

   PrintFormat("---- XauFlash %s report: %d entries | biggest burst %.2f (need %.2f) | "
               "spread %.3f-%.3f (max %.2f) ----",
               label, g_entriesSinceReport, g_maxBurstSeen, InpBurstMinMove,
               g_minSpreadSeen, g_maxSpreadSeen, g_maxSpreadPrice);
   for(int i = 0; i < BLK_COUNT; i++)
      if(g_blockCount[i] > 0)
         PrintFormat("   skipped %-22s %9I64d ticks  (%.1f%%)",
                     g_blockName[i], g_blockCount[i], 100.0 * g_blockCount[i] / total);

   ArrayInitialize(g_blockCount, 0);
   g_maxBurstSeen = 0.0;
   g_minSpreadSeen = 0.0;
   g_maxSpreadSeen = 0.0;
   g_entriesSinceReport = 0;
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
   g_lastHistoryScan= 0;
   if(!firstRun)
      PrintFormat("New trading day. Equity %.2f, counters reset.", g_dayStartEquity);
  }

void RollDailyStateIfNeeded()
  {
   MqlDateTime t;
   TimeToStruct(ToGmt(TimeCurrent()), t);
   if(t.day_of_year != g_dayOfYear)
     {
      ReportBlocks("daily");
      ResetDailyState(false);
     }
  }

//+------------------------------------------------------------------+
//| Today's closed trades, P/L and loss streak from deal history.    |
//| Scanning history on every tick is expensive, so this runs at most|
//| once a second unless a close has just happened.                  |
//+------------------------------------------------------------------+
void ScanHistoryIfNeeded()
  {
   datetime now = TimeCurrent();
   if(g_lastHistoryScan != 0 && now - g_lastHistoryScan < 1)
      return;
   g_lastHistoryScan = now;

   // Start of the GMT day, expressed in server time.
   MqlDateTime t;
   TimeToStruct(ToGmt(now), t);
   t.hour = 0; t.min = 0; t.sec = 0;
   datetime dayStart = (datetime)((long)StructToTime(t) + (long)g_gmtOffsetHrs * 3600);

   if(!HistorySelect(dayStart, now + 60))
      return;

   int    trades = 0, streak = 0;
   double realised = 0.0;
   int    total = HistoryDealsTotal();

   for(int i = 0; i < total; i++)
     {
      ulong ticket = HistoryDealGetTicket(i);
      if(ticket == 0) continue;
      if(HistoryDealGetString(ticket, DEAL_SYMBOL) != _Symbol) continue;
      if(HistoryDealGetInteger(ticket, DEAL_MAGIC) != InpMagicNumber) continue;
      long e = HistoryDealGetInteger(ticket, DEAL_ENTRY);
      if(e != DEAL_ENTRY_OUT && e != DEAL_ENTRY_OUT_BY) continue;

      trades++;
      double profit = HistoryDealGetDouble(ticket, DEAL_PROFIT)
                    + HistoryDealGetDouble(ticket, DEAL_SWAP)
                    + HistoryDealGetDouble(ticket, DEAL_COMMISSION);
      realised += profit;
      if(profit < 0.0) streak++;
      else             streak = 0;

      datetime dt = (datetime)HistoryDealGetInteger(ticket, DEAL_TIME);
      if(dt > g_lastCloseTime) g_lastCloseTime = dt;
     }

   g_tradesToday    = trades;
   g_consecLosses   = streak;
   g_dayRealisedPnl = realised;

   double equity = AccountInfoDouble(ACCOUNT_EQUITY);
   double base   = (equity > 0.0) ? equity : g_dayStartEquity;
   double limit  = -MathAbs(base * InpMaxDailyLossPct / 100.0);

   if(InpMaxDailyLossPct > 0.0 && g_dayRealisedPnl <= limit && !g_halted)
     {
      g_halted = true;
      g_haltReason = "daily loss limit";
      PrintFormat("HALT: realised %.2f today, past the %.1f%% limit. No more trades today.",
                  g_dayRealisedPnl, InpMaxDailyLossPct);
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

bool InSession(datetime serverTime)
  {
   int h = GmtHour(serverTime);
   if(InpSessionStartHour <= InpSessionEndHour)
      return(h >= InpSessionStartHour && h < InpSessionEndHour);
   return(h >= InpSessionStartHour || h < InpSessionEndHour);
  }

double CurrentSpread()
  {
   return(SymbolInfoDouble(_Symbol, SYMBOL_ASK) - SymbolInfoDouble(_Symbol, SYMBOL_BID));
  }

bool MayOpen(datetime now, string &reason)
  {
   if(g_halted)                              { Block(BLK_HALTED);  reason = g_haltReason;        return(false); }
   if(!IsTradingDay(GmtDayOfWeek(now)))      { Block(BLK_DAY);     reason = "not a trading day"; return(false); }
   if(!InSession(now))                       { Block(BLK_SESSION); reason = "outside session";   return(false); }
   if(InpMaxTradesPerDay > 0 && g_tradesToday >= InpMaxTradesPerDay)
      { Block(BLK_MAXTRADES); reason = "max trades/day"; return(false); }
   if(InpCooldownSeconds > 0 && g_lastCloseTime > 0 && now - g_lastCloseTime < InpCooldownSeconds)
      { Block(BLK_COOLDOWN); reason = "cooldown"; return(false); }

   double spread = CurrentSpread();
   if(g_minSpreadSeen <= 0.0 || spread < g_minSpreadSeen) g_minSpreadSeen = spread;
   if(spread > g_maxSpreadSeen) g_maxSpreadSeen = spread;
   if(spread > g_maxSpreadPrice)
      { Block(BLK_SPREAD); reason = StringFormat("spread %.3f > %.2f", spread, g_maxSpreadPrice); return(false); }
   if(!TerminalInfoInteger(TERMINAL_TRADE_ALLOWED)) { Block(BLK_AUTOTRADING); reason = "AutoTrading off"; return(false); }
   if(!AccountInfoInteger(ACCOUNT_TRADE_EXPERT))    { Block(BLK_AUTOTRADING); reason = "EA trading off";  return(false); }
   return(true);
  }

//+------------------------------------------------------------------+
//| POSITION HELPERS                                                 |
//+------------------------------------------------------------------+
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

bool HasOpenPosition()
  {
   return(SelectOwnPosition());
  }

double MinStopOffset()
  {
   double pointSize = SymbolInfoDouble(_Symbol, SYMBOL_POINT);
   int    pts       = MathMax(g_stopsLevelPts, g_freezeLevelPts);
   return(pts * pointSize);
  }

//+------------------------------------------------------------------+
//| SIZING: a stop-out costs InpRiskPercent of equity. Floored.      |
//+------------------------------------------------------------------+
double NormalizeLots(double lots)
  {
   if(lots <= 0.0) return(0.0);
   lots = MathFloor(lots / g_volStep) * g_volStep;
   lots = MathMin(lots, g_volMax);
   if(lots < g_volMin) return(0.0);
   return(NormalizeDouble(lots, 2));
  }

double CalcLots(double distance)
  {
   if(InpFixedLots > 0.0)
      return(NormalizeLots(InpFixedLots));
   if(distance <= 0.0) return(0.0);

   double equity     = AccountInfoDouble(ACCOUNT_EQUITY);
   double riskCash   = equity * InpRiskPercent / 100.0;
   double lossPerLot = distance / g_tickSize * g_tickValue;
   if(lossPerLot <= 0.0) return(0.0);
   return(NormalizeLots(riskCash / lossPerLot));
  }

//+------------------------------------------------------------------+
//| ENTRY                                                            |
//+------------------------------------------------------------------+
void TryEntry(const MqlTick &tk)
  {
   long nowMs = (long)tk.time_msc;
   if(nowMs <= 0) nowMs = (long)tk.time * 1000;

   double move, dirShare;
   int    ticks;
   int    dir = DetectBurst(nowMs, move, ticks, dirShare);
   if(MathAbs(move) > g_maxBurstSeen) g_maxBurstSeen = MathAbs(move);
   if(dir == 0)
     {
      Block(BLK_NO_BURST);
      if(g_panel)
         g_status = StringFormat("watching: %+.2f in %d ticks (%.0f%% one-way)",
                              move, ticks, dirShare * 100.0);
      return;
     }

   double spread = CurrentSpread();
   if(spread > 0.0 && MathAbs(move) < InpBurstSpreadMult * spread)
     {
      Block(BLK_BURST_VS_SPREAD);
      g_status = StringFormat("burst %.2f < %.1fx spread %.2f", MathAbs(move), InpBurstSpreadMult, spread);
      return;
     }
   if(spread > 0.0 && InpTakeProfit < InpMinTargetSpreadRatio * spread)
     {
      Block(BLK_TARGET_VS_SPREAD);
      g_status = StringFormat("target %.2f < %.1fx spread %.2f", InpTakeProfit, InpMinTargetSpreadRatio, spread);
      return;
     }

   double minOff = MinStopOffset();
   double slDist = MathMax(InpStopLoss,   minOff);
   double tpDist = MathMax(InpTakeProfit, minOff);

   double lots = CalcLots(slDist);
   if(lots <= 0.0)
     {
      Block(BLK_SIZE);
      g_status = "size below broker minimum - equity too small for this risk %";
      return;
     }

   double price, sl, tp;
   bool   ok;
   if(dir > 0)
     {
      price = tk.ask;
      sl    = NormalizeDouble(price - slDist, _Digits);
      tp    = NormalizeDouble(price + tpDist, _Digits);
      ok    = trade.Buy(lots, _Symbol, 0.0, sl, tp, "XauFlash");
     }
   else
     {
      price = tk.bid;
      sl    = NormalizeDouble(price + slDist, _Digits);
      tp    = NormalizeDouble(price - tpDist, _Digits);
      ok    = trade.Sell(lots, _Symbol, 0.0, sl, tp, "XauFlash");
     }

   if(!ok || (trade.ResultRetcode() != TRADE_RETCODE_DONE &&
              trade.ResultRetcode() != TRADE_RETCODE_PLACED))
     {
      Block(BLK_ORDER_FAILED);
      g_status = StringFormat("order failed: %d %s", trade.ResultRetcode(), trade.ResultRetcodeDescription());
      PrintFormat("ORDER FAILED retcode=%d (%s) lots=%.2f", trade.ResultRetcode(),
                  trade.ResultRetcodeDescription(), lots);
      return;
     }

   // Slippage: how much worse than the quote we acted on did we fill.
   double fill = trade.ResultPrice();
   double slip = 0.0;
   if(fill > 0.0)
     {
      slip = (dir > 0) ? fill - price : price - fill;
      g_fills++;
      g_slipSum += slip;
     }

   g_entriesSinceReport++;
   g_status = StringFormat("opened %s %.2f lots", (dir > 0 ? "BUY" : "SELL"), lots);
   PrintFormat("%s %.2f @ %.2f (quote %.2f, slip %+.2f) sl=%.2f tp=%.2f | burst %+.2f in %d ticks, spread %.2f",
               (dir > 0 ? "BUY" : "SELL"), lots, fill, price, slip, sl, tp, move, ticks, spread);
  }

//+------------------------------------------------------------------+
//| MANAGEMENT: the hold timer and the forced-flat rules.            |
//| SL/TP themselves live on the server and need nothing from here.  |
//+------------------------------------------------------------------+
void ManageOpenPosition(datetime now)
  {
   if(!SelectOwnPosition()) return;

   ulong ticket = posInfo.Ticket();
   long  openedMs = (long)PositionGetInteger(POSITION_TIME_MSC);
   long  nowMs    = (long)TimeCurrent() * 1000;
   MqlTick tk;
   if(SymbolInfoTick(_Symbol, tk) && tk.time_msc > 0)
      nowMs = (long)tk.time_msc;
   // The last tick can be older than the wall clock in a quiet market; the
   // timer must still fire, so take whichever clock is further along.
   nowMs = MathMax(nowMs, (long)TimeTradeServer() * 1000);

   double heldSec = (openedMs > 0) ? (nowMs - openedMs) / 1000.0
                                   : (double)(now - (datetime)posInfo.Time());

   string reason = "";
   if(g_halted)                                  reason = g_haltReason;
   else if(!IsTradingDay(GmtDayOfWeek(now)))     reason = "not a trading day";
   else if(!InSession(now))                      reason = "session over";
   else if(heldSec >= InpMaxHoldSeconds)         reason = StringFormat("held %.1f s", heldSec);

   if(reason == "")
     {
      if(g_panel)
         g_status = StringFormat("in trade %.1f / %d s  P/L %.2f", heldSec, InpMaxHoldSeconds,
                              posInfo.Profit());
      return;
     }

   // Read before closing: once the position is gone there is nothing to read.
   double pnl = posInfo.Profit();
   if(trade.PositionClose(ticket))
     {
      if(heldSec >= InpMaxHoldSeconds) g_timerExits++;
      g_lastCloseTime = now;
      g_lastHistoryScan = 0;
      PrintFormat("Closed #%I64u: %s, P/L ~%.2f", ticket, reason, pnl);
      g_status = "closed: " + reason;
     }
   else
     {
      PrintFormat("PositionClose #%I64u failed: %d (%s)", ticket,
                  trade.ResultRetcode(), trade.ResultRetcodeDescription());
     }
  }

//+------------------------------------------------------------------+
//| PANEL                                                            |
//+------------------------------------------------------------------+
void DrawPanel()
  {
   double equity = AccountInfoDouble(ACCOUNT_EQUITY);
   datetime now  = TimeCurrent();
   double avgSlip = (g_fills > 0) ? g_slipSum / g_fills : 0.0;

   string txt = StringFormat(
      "XauFlash (seconds scalper)  |  %s\n"
      "-----------------------------------------\n"
      "server %s   (GMT%+d)   GMT hour %02d\n"
      "session %02d-%02d GMT      in session: %s\n"
      "spread %.3f  (max %.2f)\n"
      "-----------------------------------------\n"
      "equity      %.2f      risk/trade %.2f%%\n"
      "day P/L     %.2f      limit %.1f%%\n"
      "trades today %d / %d      loss streak %d / %d\n"
      "fills %d   avg slippage %.3f\n"
      "state       %s%s",
      _Symbol,
      TimeToString(now, TIME_DATE|TIME_SECONDS), g_gmtOffsetHrs, GmtHour(now),
      InpSessionStartHour, InpSessionEndHour, (InSession(now) ? "yes" : "no"),
      CurrentSpread(), g_maxSpreadPrice,
      equity, InpRiskPercent,
      g_dayRealisedPnl, InpMaxDailyLossPct,
      g_tradesToday, InpMaxTradesPerDay, g_consecLosses, InpMaxConsecLosses,
      g_fills, avgSlip,
      g_status,
      (g_halted ? ("\nHALTED: " + g_haltReason) : ""));

   Comment(txt);
  }
//+------------------------------------------------------------------+
