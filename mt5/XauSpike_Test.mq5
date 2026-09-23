//+------------------------------------------------------------------+
//|                                              XauSpike_Test.mq5   |
//|        Spike catcher: two cores, fast trailing exits             |
//|                 XAUUSD - any chart - built for Exness MT5        |
//+------------------------------------------------------------------+
//| *** TEST COPY *** of XauSpike 1.30 for the Strategy Tester.
//| Identical logic; the only differences are FixedLots = 0.01 on both
//| cores, so every trade has the same weight in the report, and its own
//| magic numbers. For live trading use XauSpike, whose lots follow the
//| account.
//|
//| WHAT THIS IS
//| Gold spends most of its day drifting and a few minutes of it moving
//| violently: data releases, the Asian open, liquidity gaps. This EA sits
//| out the drift, joins a violent move once it is underway, and locks the
//| profit in fast with a stop that follows price.
//|
//| Two independent cores, each with its own magic number and position:
//|
//|   Core A  fast spike   trigger: >= 3.00 in 5 s     SL 5   TP 50 (far)
//|                        exit   : trailing stop, starts at +1.50,
//|                                 follows 1.00 behind -> seconds long
//|   Core B  momentum     trigger: >= 6.00 in 60 s    SL 15  TP 15
//|                        exit   : at +5.00 the stop jumps to +1.50,
//|                                 otherwise target or stop -> minutes
//|
//| The exit structure was read off a third-party Strategy Tester report
//| (SL 5 + trail for one core; SL 15, TP ~15 and a +1.5 lock for the
//| other). Its ENTRY rule is not visible in a report, so the triggers
//| above are estimates. Tune them in the Strategy Tester.
//|
//| v1.10 -- after the first real-tick test (Sep 1-17, 126 trades, PF 0.84)
//| Wins were right (72%) but too small against full-stop losses: core A
//| needed 75% to break even, core B 85%. And 126 trades in 12 days against
//| the reference's ~20 a month meant the trigger fired on ordinary moves.
//|   * SpikeRatio   the move must be N x a normal move for its window
//|                  (M1 ATR scaled by sqrt(time)), not a fixed size
//|   * FreshShare   skip a spike that has already stalled
//|   * NoFollow     a spike that has not paid within N seconds is closed
//|                  small instead of waiting for the full stop
//|
//| v1.20 -- after Jul 1 - Sep 17 (310 trades, +935 on 2000, PF 1.51)
//|   core A  +1412   141 trades, avg win 4.97 vs avg loss 2.47: the
//|                   no-follow-through exit halved the average loss
//|   core B  -476    lost in July, August AND September. It is OFF by
//|                   default; its code stays so it can be re-tested.
//|   SkipHours added so an hour filter can be tried without editing
//|   code. Default is empty: 140 trades are too few to pick hours on.
//|
//| v1.30 -- Jan 1 - Sep 17, core A only (418 trades, PF 4.17)
//|   Jan-Jun, never used for tuning: positive every month. Good sign.
//|   But: Jan-Jul won 93-100% a month, Aug 67%, Sep 44% and negative.
//|   The most recent weeks are the weakest. Added a brake: while a
//|   core's last 10 trades net a loss (in USD/oz), its risk is halved.
//|
//| READ THIS BEFORE RUNNING IT ON REAL MONEY
//|   * Test ONLY with "Every tick based on real ticks". Anything else
//|     invents the ticks a spike is made of.
//|   * The tester fills stops and modifies them with zero latency. Live,
//|     during a spike, both slip. Core A's typical win is a couple of
//|     dollars per ounce, so slippage matters. Run it on a VPS.
//|   * Few trades a month is normal: it waits for spikes.
//|   * Demo first, for at least a month.
//+------------------------------------------------------------------+
#property copyright "XauSpike_Test"
#property link      ""
#property version   "1.30"

#include <Trade\Trade.mqh>
#include <Trade\PositionInfo.mqh>

CTrade        trade;
CPositionInfo posInfo;

enum ENUM_TZ_MODE
  {
   TZ_AUTO   = 0,  // Detect the server's GMT offset automatically
   TZ_MANUAL = 1   // Use the ServerGmtOffset input below
  };

//+------------------------------------------------------------------+
//| INPUTS                                                           |
//+------------------------------------------------------------------+
input group "=== Core A: fast spike (seconds) ==="
input bool   InpA_Enable           = true;
input long   InpA_Magic            = 770666; // Must differ from every other EA
input double InpA_RiskPercent      = 1.0;    // Risk per trade (%) if SL is hit
input double InpA_FixedLots        = 0.01;    // >0 overrides risk sizing
input int    InpA_WindowMs         = 5000;   // Spike must happen within this many ms
input double InpA_TriggerMove      = 3.00;   // ...and move at least this much (USD/oz)
input int    InpA_MinTicks         = 8;      // ...on at least this many price changes
input double InpA_Directional      = 0.65;   // Share of steps in the spike's direction
input double InpA_StopLoss         = 5.00;   // Initial stop distance (USD/oz)
input double InpA_TakeProfit       = 50.00;  // Far target; the trail normally exits first
input double InpA_TrailStart       = 1.50;   // Start trailing at this profit (0 = no trail)
input double InpA_TrailDistance    = 1.00;   // Trail this far behind price
input double InpA_LockTrigger      = 0.00;   // At this profit move SL to +LockProfit (0 = off)
input double InpA_LockProfit       = 0.00;
input int    InpA_MaxHoldMinutes   = 30;     // Close at market after this (0 = off)
input int    InpA_CooldownSeconds  = 120;    // Wait after a close before the next entry
input double InpA_SpikeRatio       = 5.0;    // Spike must be N x a normal move for this window (0 = off)
input double InpA_FreshShare       = 0.25;   // Last third of the window must carry >= this share of the move (0 = off)
input int    InpA_NoFollowSeconds  = 20;     // Close if the trade has not reached +NoFollowProfit by then (0 = off)
input double InpA_NoFollowProfit   = 1.00;

input group "=== Core B: momentum (minutes) ==="
input bool   InpB_Enable           = false;  // OFF: lost money in every month tested (see v1.20 notes)
input long   InpB_Magic            = 770677; // Must differ from every other EA
input double InpB_RiskPercent      = 1.0;
input double InpB_FixedLots        = 0.01;
input int    InpB_WindowMs         = 60000;
input double InpB_TriggerMove      = 6.00;
input int    InpB_MinTicks         = 30;
input double InpB_Directional      = 0.58;
input double InpB_StopLoss         = 15.00;
input double InpB_TakeProfit       = 15.00;
input double InpB_TrailStart       = 0.00;   // off: B uses the lock below instead
input double InpB_TrailDistance    = 0.00;
input double InpB_LockTrigger      = 5.00;   // At +5.00 ...
input double InpB_LockProfit       = 1.50;   // ... the stop moves to +1.50
input int    InpB_MaxHoldMinutes   = 90;
input int    InpB_CooldownSeconds  = 300;
input double InpB_SpikeRatio       = 3.0;
input double InpB_FreshShare       = 0.20;
input int    InpB_NoFollowSeconds  = 600;
input double InpB_NoFollowProfit   = 3.00;

input group "=== Volatility reference for SpikeRatio ==="
input int    InpAtrM1Period        = 30;     // "Normal" = M1 ATR over this many minutes

input group "=== Account protection (both cores together) ==="
input double InpMaxDailyLossPct    = 5.0;    // Stop for the day at this realised loss (%). 0 = OFF
input int    InpMaxConsecLosses    = 3;      // Stop for the day after N losses in a row. 0 = OFF
input int    InpMaxTradesPerDay    = 10;     // 0 = unlimited
input int    InpPerfTrades         = 10;     // Watch each core's last N closed trades (0 = off)
input double InpPerfRiskMult       = 0.5;    // If they net a loss, risk is multiplied by this

input group "=== Time filters (hours are GMT/UTC) ==="
input ENUM_TZ_MODE InpTzMode       = TZ_AUTO;
input int    InpServerGmtOffset    = 0;      // Server GMT offset when TZ_MANUAL
input int    InpSessionStartHour   = 0;      // 0 + 24 = trade all hours
input int    InpSessionEndHour     = 24;
input int    InpRolloverSkipStart  = 21;     // No new entries in the daily rollover
input int    InpRolloverSkipEnd    = 22;     // (spread blowout). Equal values = off
input int    InpFridayCloseHour    = 20;     // Flat and no entries from this GMT hour on Friday (24 = off)
input string InpSkipHours          = "";     // Extra GMT hours with no entries, e.g. "16,17,18" (empty = none)
input bool   InpTradeMonday        = true;
input bool   InpTradeTuesday       = true;
input bool   InpTradeWednesday     = true;
input bool   InpTradeThursday      = true;
input bool   InpTradeFriday        = true;

input group "=== Execution ==="
input int    InpMaxSpreadPoints    = 500;    // Skip entries above this spread, IN POINTS
input int    InpSlippagePoints     = 100;    // Max deviation on market orders (points)
input double InpTrailStep          = 0.10;   // Only modify the stop when it moves this much

input group "=== Display ==="
input bool   InpShowPanel          = true;

//+------------------------------------------------------------------+
//| CORE SETTINGS                                                    |
//+------------------------------------------------------------------+
struct CoreCfg
  {
   bool   enable;
   long   magic;
   double riskPct;
   double fixedLots;
   int    windowMs;
   double trigger;
   int    minTicks;
   double directional;
   double sl;
   double tp;
   double trailStart;
   double trailDist;
   double lockTrigger;
   double lockProfit;
   int    maxHoldMin;
   int    cooldownSec;
   double spikeRatio;
   double freshShare;
   int    noFollowSec;
   double noFollowProfit;
  };

CoreCfg  g_core[2];
// Kept out of CoreCfg on purpose: a struct holding a string is not a "simple"
// struct in MQL5, and copying one with = is not something to rely on.
string   g_coreName[2] = {"A", "B"};

//+------------------------------------------------------------------+
//| GLOBALS                                                          |
//+------------------------------------------------------------------+
#define TICK_BUF 4096

long     g_tickMs[TICK_BUF];
double   g_tickBid[TICK_BUF];
int      g_tickHead      = 0;
int      g_tickCount     = 0;

// Each core's look-back window over the tick buffer, kept incrementally so a
// 60-second window costs the same per tick as a 5-second one. Walking the
// window on every tick is fine live and far too slow over the tens of
// millions of real ticks a tester run replays.
int      g_winStart[2];
int      g_winSize[2];      // ticks in the window, newest included
int      g_winUps[2];
int      g_winDowns[2];

int      g_gmtOffsetHrs  = 0;

int      g_dayOfYear     = -1;
double   g_dayStartEquity= 0.0;
int      g_tradesToday   = 0;
double   g_dayRealisedPnl= 0.0;
int      g_consecLosses  = 0;
bool     g_halted        = false;
string   g_haltReason    = "";
datetime g_lastHistoryScan = 0;

// Recent-performance brake: each core's last InpPerfTrades results in
// USD/oz (lot size divided out, so compounding does not distort it).
bool     g_perfDirty     = true;
double   g_perfSum[2];
int      g_perfN[2];
double   g_perfMult[2] = {1.0, 1.0};
datetime g_lastClose[2];

double   g_maxSpreadPrice= 0.0;
double   g_tickSize      = 0.0;
double   g_tickValue     = 0.0;
double   g_volMin        = 0.0;
double   g_volMax        = 0.0;
double   g_volStep       = 0.0;
int      g_stopsLevelPts = 0;
int      g_freezeLevelPts= 0;

bool     g_panel         = true;
string   g_status[2];

// Why entries were skipped, per core, printed daily and when the EA stops.
enum ENUM_BLOCK
  {
   BLK_ACCOUNT = 0, BLK_TIME, BLK_COOLDOWN, BLK_SPREAD, BLK_AUTOTRADING,
   BLK_NO_SPIKE, BLK_SIZE, BLK_ORDER_FAILED, BLK_WEAK_SPIKE, BLK_STALLED, BLK_COUNT
  };
// Sized by a literal (10 = BLK_COUNT): an enum value as an array bound is not
// worth the risk in a file that cannot be test-compiled here.
string   g_blockName[10] = {"account guard", "time filter", "cooldown", "spread too wide",
                            "AutoTrading off", "no spike", "lot below minimum", "order rejected",
                            "spike not unusual", "spike stalled"};
long     g_blockCount[2][10];

int      hAtrM1 = INVALID_HANDLE;
// Best profit each open position has reached, for the no-follow-through exit.
ulong    g_peakTicket[2];
double   g_peakProfit[2];
double   g_maxMoveSeen[2];
int      g_entries[2];

//+------------------------------------------------------------------+
//| INIT                                                             |
//+------------------------------------------------------------------+
void LoadCores()
  {
   g_core[0].enable = InpA_Enable;
   g_core[0].magic = InpA_Magic;         g_core[0].riskPct = InpA_RiskPercent;
   g_core[0].fixedLots = InpA_FixedLots; g_core[0].windowMs = InpA_WindowMs;
   g_core[0].trigger = InpA_TriggerMove; g_core[0].minTicks = InpA_MinTicks;
   g_core[0].directional = InpA_Directional;
   g_core[0].sl = InpA_StopLoss;         g_core[0].tp = InpA_TakeProfit;
   g_core[0].trailStart = InpA_TrailStart; g_core[0].trailDist = InpA_TrailDistance;
   g_core[0].lockTrigger = InpA_LockTrigger; g_core[0].lockProfit = InpA_LockProfit;
   g_core[0].maxHoldMin = InpA_MaxHoldMinutes; g_core[0].cooldownSec = InpA_CooldownSeconds;
   g_core[0].spikeRatio = InpA_SpikeRatio; g_core[0].freshShare = InpA_FreshShare;
   g_core[0].noFollowSec = InpA_NoFollowSeconds; g_core[0].noFollowProfit = InpA_NoFollowProfit;

   g_core[1].enable = InpB_Enable;
   g_core[1].magic = InpB_Magic;         g_core[1].riskPct = InpB_RiskPercent;
   g_core[1].fixedLots = InpB_FixedLots; g_core[1].windowMs = InpB_WindowMs;
   g_core[1].trigger = InpB_TriggerMove; g_core[1].minTicks = InpB_MinTicks;
   g_core[1].directional = InpB_Directional;
   g_core[1].sl = InpB_StopLoss;         g_core[1].tp = InpB_TakeProfit;
   g_core[1].trailStart = InpB_TrailStart; g_core[1].trailDist = InpB_TrailDistance;
   g_core[1].lockTrigger = InpB_LockTrigger; g_core[1].lockProfit = InpB_LockProfit;
   g_core[1].maxHoldMin = InpB_MaxHoldMinutes; g_core[1].cooldownSec = InpB_CooldownSeconds;
   g_core[1].spikeRatio = InpB_SpikeRatio; g_core[1].freshShare = InpB_FreshShare;
   g_core[1].noFollowSec = InpB_NoFollowSeconds; g_core[1].noFollowProfit = InpB_NoFollowProfit;
  }

bool ValidateCore(int i)
  {
   CoreCfg c = g_core[i];
   if(!c.enable) return(true);
   string p = "ERROR: core " + g_coreName[i] + ": ";
   if(c.riskPct <= 0.0 || c.riskPct > 5.0)       { Print(p, "RiskPercent must be in (0, 5]."); return(false); }
   if(c.sl <= 0.0 || c.tp <= 0.0)                { Print(p, "StopLoss and TakeProfit must be > 0."); return(false); }
   if(c.trigger <= 0.0)                          { Print(p, "TriggerMove must be > 0."); return(false); }
   if(c.windowMs < 200 || c.windowMs > 600000)   { Print(p, "WindowMs must be in [200, 600000]."); return(false); }
   if(c.minTicks < 2)                            { Print(p, "MinTicks must be >= 2."); return(false); }
   if(c.directional < 0.5 || c.directional > 1.0){ Print(p, "Directional must be in [0.5, 1]."); return(false); }
   if(c.trailStart > 0.0 && c.trailDist <= 0.0)
     { Print(p, "TrailDistance must be > 0 when TrailStart is set."); return(false); }
   if(c.spikeRatio < 0.0 || c.freshShare < 0.0 || c.freshShare > 1.0)
     { Print(p, "SpikeRatio must be >= 0 and FreshShare in [0, 1]."); return(false); }
   if(c.lockTrigger > 0.0 && c.lockProfit >= c.lockTrigger)
     { Print(p, "LockProfit must be below LockTrigger."); return(false); }
   return(true);
  }

int OnInit()
  {
   LoadCores();
   if(!ResolveSymbolSpec())                      return(INIT_FAILED);
   if(!ValidateCore(0) || !ValidateCore(1))    return(INIT_FAILED);
   if(!g_core[0].enable && !g_core[1].enable)    { Print("ERROR: both cores are disabled."); return(INIT_FAILED); }
   if(g_core[0].magic == g_core[1].magic)        { Print("ERROR: the two cores need different magic numbers."); return(INIT_FAILED); }

   trade.SetDeviationInPoints(InpSlippagePoints);
   trade.SetTypeFillingBySymbol(_Symbol);

   bool tester = (bool)MQLInfoInteger(MQL_TESTER);
   bool visual = (bool)MQLInfoInteger(MQL_VISUAL_MODE);
   g_panel = InpShowPanel && (!tester || visual);

   g_gmtOffsetHrs = ResolveGmtOffset();
   if(!ParseSkipHours())
      return(INIT_FAILED);
   ResetDailyState(true);
   for(int c = 0; c < 2; c++)
     {
      for(int b = 0; b < BLK_COUNT; b++) g_blockCount[c][b] = 0;
      g_maxMoveSeen[c] = 0.0;
      g_entries[c]     = 0;
      g_lastClose[c]   = 0;
      g_winStart[c] = 0; g_winSize[c] = 0; g_winUps[c] = 0; g_winDowns[c] = 0;
     }
   g_status[0] = "watching"; g_status[1] = "watching";

   hAtrM1 = iATR(_Symbol, PERIOD_M1, MathMax(2, InpAtrM1Period));
   if(hAtrM1 == INVALID_HANDLE)
     {
      Print("ERROR: could not create the M1 ATR used by SpikeRatio.");
      return(INIT_FAILED);
     }
   g_peakTicket[0] = 0; g_peakTicket[1] = 0;
   g_peakProfit[0] = 0.0; g_peakProfit[1] = 0.0;

   // The max-hold rule must fire even when no ticks arrive.
   EventSetTimer(1);

   double pointSize = SymbolInfoDouble(_Symbol, SYMBOL_POINT);
   g_maxSpreadPrice = InpMaxSpreadPoints * pointSize;

   PrintFormat("XauSpike started on %s | server GMT%+d | tick %.5f value %.5f | lots %.2f-%.2f step %.2f | stops %d pts, freeze %d pts",
               _Symbol, g_gmtOffsetHrs, g_tickSize, g_tickValue, g_volMin, g_volMax, g_volStep,
               g_stopsLevelPts, g_freezeLevelPts);
   PrintFormat("Max spread: %d points = %.2f USD/oz (digits %d, current spread %.3f)",
               InpMaxSpreadPoints, g_maxSpreadPrice, (int)_Digits, CurrentSpread());
   if(g_maxSpreadPrice < 0.15)
      PrintFormat("WARNING: a %.3f USD/oz spread limit is narrower than gold's normal spread. "
                  "Almost every entry will be skipped. Raise MaxSpreadPoints.", g_maxSpreadPrice);

   for(int i = 0; i < 2; i++)
     {
      if(!g_core[i].enable) { PrintFormat("Core %s: disabled", g_coreName[i]); continue; }
      PrintFormat("Core %s: trigger %.2f in %d ms on %d ticks (%.0f%% one-way) | SL %.2f TP %.2f | trail %.2f/%.2f | lock %.2f->%.2f | magic %I64d",
                  g_coreName[i], g_core[i].trigger, g_core[i].windowMs, g_core[i].minTicks,
                  g_core[i].directional * 100.0, g_core[i].sl, g_core[i].tp,
                  g_core[i].trailStart, g_core[i].trailDist, g_core[i].lockTrigger, g_core[i].lockProfit,
                  g_core[i].magic);
      WarnIfUnaffordable(i);
     }

   PrintFormat("TEST COPY: fixed lots A=%.2f B=%.2f. For live trading use XauSpike.", InpA_FixedLots, InpB_FixedLots);
   if(!tester)
      Print("WARNING: XauSpike_Test is running on a LIVE chart. Its lots do not follow the account. Use XauSpike for live trading.");

   if(tester)
      Print("TESTER: use 'Every tick based on real ticks'. Other modes invent the ticks a spike is made of.");

   return(INIT_SUCCEEDED);
  }

void OnDeinit(const int reason)
  {
   EventKillTimer();
   ReportBlocks("final");
   if(hAtrM1 != INVALID_HANDLE) IndicatorRelease(hAtrM1);
   Comment("");
  }

bool ResolveSymbolSpec()
  {
   if(!SymbolInfoInteger(_Symbol, SYMBOL_SELECT) && !SymbolSelect(_Symbol, true))
     {
      PrintFormat("ERROR: symbol %s is not available in Market Watch.", _Symbol);
      return(false);
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
      PrintFormat("ERROR: incomplete symbol spec for %s. Open a chart of it once so the terminal downloads it.", _Symbol);
      return(false);
     }
   return(true);
  }

void WarnIfUnaffordable(int i)
  {
   CoreCfg c = g_core[i];
   if(c.fixedLots > 0.0) return;
   double eq      = AccountInfoDouble(ACCOUNT_EQUITY);
   double budget  = eq * c.riskPct / 100.0;
   double minLoss = c.sl / g_tickSize * g_tickValue * g_volMin;
   if(minLoss > budget)
      PrintFormat("WARNING: core %s CANNOT OPEN. The smallest lot (%.2f) loses %.2f at the %.2f stop, "
                  "but %.1f%% of %.2f equity is %.2f. Raise RiskPercent, or set FixedLots = %.2f "
                  "(which risks %.1f%% per trade).",
                  g_coreName[i], g_volMin, minLoss, c.sl, c.riskPct, eq, budget, g_volMin,
                  (eq > 0.0 ? 100.0 * minLoss / eq : 0.0));
  }

//+------------------------------------------------------------------+
//| TIME                                                             |
//+------------------------------------------------------------------+
int ResolveGmtOffset()
  {
   if(InpTzMode == TZ_MANUAL) return(InpServerGmtOffset);
   datetime srv = TimeCurrent(), gmt = TimeGMT();
   if(srv <= 0 || gmt <= 0) return(0);
   return((int)MathRound((double)(srv - gmt) / 3600.0));
  }

datetime ToGmt(datetime serverTime)
  {
   long secs = (long)serverTime - (long)g_gmtOffsetHrs * 3600;
   if(secs < 0) secs = 0;
   return((datetime)secs);
  }

void GmtParts(datetime serverTime, int &hour, int &dow)
  {
   MqlDateTime t;
   TimeToStruct(ToGmt(serverTime), t);
   hour = t.hour;
   dow  = t.day_of_week;
  }

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

bool HourIn(int h, int start, int end)
  {
   if(start == end) return(false);
   if(start < end)  return(h >= start && h < end);
   return(h >= start || h < end);
  }

// Must an open position be closed now? (weekend, disabled day)
bool MustBeFlat(datetime now, string &why)
  {
   int h, dow;
   GmtParts(now, h, dow);
   if(!IsTradingDay(dow))                     { why = "not a trading day"; return(true); }
   if(dow == 5 && h >= InpFridayCloseHour)    { why = "Friday close";      return(true); }
   return(false);
  }

//+------------------------------------------------------------------+
//| "16,17,18" -> g_skipHour[16..18] = true                          |
//+------------------------------------------------------------------+
bool g_skipHour[24];

bool ParseSkipHours()
  {
   ArrayInitialize(g_skipHour, false);
   string txt = InpSkipHours;
   StringTrimLeft(txt);
   StringTrimRight(txt);
   if(txt == "") return(true);

   string parts[];
   int n = StringSplit(txt, ',', parts);
   string list = "";
   for(int k = 0; k < n; k++)
     {
      string one = parts[k];
      StringTrimLeft(one);
      StringTrimRight(one);
      if(one == "") continue;
      int h = (int)StringToInteger(one);
      if(h < 0 || h > 23 || IntegerToString(h) != one)
        {
         PrintFormat("ERROR: SkipHours entry '%s' is not an hour 0-23. Use a list like 16,17,18", one);
         return(false);
        }
      g_skipHour[h] = true;
      list += (list == "" ? "" : ",") + IntegerToString(h);
     }
   PrintFormat("No new entries in these GMT hours: %s", list);
   return(true);
  }

bool TimeAllowsEntry(datetime now)
  {
   int h, dow;
   GmtParts(now, h, dow);
   if(!IsTradingDay(dow))                                    return(false);
   if(dow == 5 && h >= InpFridayCloseHour)                   return(false);
   if(!(InpSessionStartHour <= 0 && InpSessionEndHour >= 24) &&
      !HourIn(h, InpSessionStartHour, InpSessionEndHour))    return(false);
   if(HourIn(h, InpRolloverSkipStart, InpRolloverSkipEnd))   return(false);
   if(g_skipHour[h])                                         return(false);
   return(true);
  }

//+------------------------------------------------------------------+
//| MAIN LOOP                                                        |
//+------------------------------------------------------------------+
void OnTick()
  {
   MqlTick tk;
   if(!SymbolInfoTick(_Symbol, tk)) return;
   RecordTick(tk);

   RollDailyStateIfNeeded();
   ScanHistoryIfNeeded();

   datetime now = TimeCurrent();
   for(int i = 0; i < 2; i++)
     {
      if(!g_core[i].enable) continue;
      if(SelectCorePosition(g_core[i]))
         ManagePosition(i, now, tk);
      else
         TryEntry(i, now, tk);
     }
   if(g_panel) DrawPanel();
  }

void OnTimer()
  {
   MqlTick tk;
   if(!SymbolInfoTick(_Symbol, tk)) return;
   datetime now = TimeCurrent();
   for(int i = 0; i < 2; i++)
      if(g_core[i].enable && SelectCorePosition(g_core[i]))
         ManagePosition(i, now, tk);
  }

void OnTradeTransaction(const MqlTradeTransaction &trans,
                        const MqlTradeRequest &request,
                        const MqlTradeResult &result)
  {
   if(trans.type != TRADE_TRANSACTION_DEAL_ADD || trans.symbol != _Symbol) return;
   if(!HistoryDealSelect(trans.deal)) return;
   long magic = HistoryDealGetInteger(trans.deal, DEAL_MAGIC);
   long entry = HistoryDealGetInteger(trans.deal, DEAL_ENTRY);
   if(entry != DEAL_ENTRY_OUT && entry != DEAL_ENTRY_OUT_BY) return;
   for(int i = 0; i < 2; i++)
      if(magic == g_core[i].magic)
        {
         g_lastClose[i] = TimeCurrent();
         g_lastHistoryScan = 0;
         g_perfDirty = true;
        }
  }

//+------------------------------------------------------------------+
//| The brake. A spike edge depends on the market having real spikes;|
//| when it stops having them the strategy does not know, and keeps  |
//| trading full size into a regime that no longer pays. In testing  |
//| Jan-Jul won 93-100% of trades and Aug-Sep 67% then 44%. This     |
//| halves risk while a core's last N trades net a loss, and restores|
//| it once they net a gain again. It keeps trading so it can see    |
//| the recovery; it only trades smaller until then.                 |
//+------------------------------------------------------------------+
void UpdatePerformance()
  {
   g_perfDirty = false;
   if(InpPerfTrades <= 0) return;

   datetime now = TimeCurrent();
   if(!HistorySelect(now - 120 * 86400, now + 60)) { g_perfDirty = true; return; }

   double perOzPerLot = g_tickValue / g_tickSize;   // money per 1.00 price move per lot
   for(int c = 0; c < 2; c++) { g_perfSum[c] = 0.0; g_perfN[c] = 0; }

   for(int k = HistoryDealsTotal() - 1; k >= 0; k--)
     {
      ulong tk = HistoryDealGetTicket(k);
      if(tk == 0) continue;
      if(HistoryDealGetString(tk, DEAL_SYMBOL) != _Symbol) continue;
      long e = HistoryDealGetInteger(tk, DEAL_ENTRY);
      if(e != DEAL_ENTRY_OUT && e != DEAL_ENTRY_OUT_BY) continue;
      long magic = HistoryDealGetInteger(tk, DEAL_MAGIC);
      double vol = HistoryDealGetDouble(tk, DEAL_VOLUME);
      if(vol <= 0.0 || perOzPerLot <= 0.0) continue;
      double money = HistoryDealGetDouble(tk, DEAL_PROFIT) + HistoryDealGetDouble(tk, DEAL_SWAP)
                   + HistoryDealGetDouble(tk, DEAL_COMMISSION);
      for(int c = 0; c < 2; c++)
         if(magic == g_core[c].magic && g_perfN[c] < InpPerfTrades)
           {
            g_perfSum[c] += money / (vol * perOzPerLot);
            g_perfN[c]++;
           }
     }

   for(int c = 0; c < 2; c++)
     {
      // Only judge on a full window: three trades say nothing.
      double mult = (g_perfN[c] >= InpPerfTrades && g_perfSum[c] < 0.0) ? InpPerfRiskMult : 1.0;
      if(mult != g_perfMult[c])
         PrintFormat("Core %s: last %d trades net %+.2f USD/oz -> risk x%.2f",
                     g_coreName[c], g_perfN[c], g_perfSum[c], mult);
      g_perfMult[c] = mult;
     }
  }

//+------------------------------------------------------------------+
//| TICK BUFFER AND SPIKE DETECTION                                  |
//+------------------------------------------------------------------+
void RecordTick(const MqlTick &tk)
  {
   if(tk.bid <= 0.0) return;
   long ms = (long)tk.time_msc;
   if(ms <= 0) ms = (long)tk.time * 1000;
   int prev = (g_tickHead - 1 + TICK_BUF) % TICK_BUF;
   if(g_tickCount > 0 && g_tickBid[prev] == tk.bid) return;   // unchanged quote: no information

   int j = g_tickHead;
   g_tickMs[j]  = ms;
   g_tickBid[j] = tk.bid;
   g_tickHead = (g_tickHead + 1) % TICK_BUF;
   if(g_tickCount < TICK_BUF) g_tickCount++;

   for(int i = 0; i < 2; i++)
     {
      if(g_winSize[i] == 0)
        {
         g_winStart[i] = j;
         g_winSize[i]  = 1;
         continue;
        }
      if(tk.bid > g_tickBid[prev]) g_winUps[i]++;
      else                         g_winDowns[i]++;
      g_winSize[i]++;
      // Never let the window reach round the ring onto the slot written next.
      while(g_winSize[i] >= TICK_BUF - 1) DropOldest(i);
      TrimWindow(i, ms);
     }
  }

void DropOldest(int i)
  {
   int s0 = g_winStart[i];
   int s1 = (s0 + 1) % TICK_BUF;
   if(g_tickBid[s1] > g_tickBid[s0])      g_winUps[i]--;
   else if(g_tickBid[s1] < g_tickBid[s0]) g_winDowns[i]--;
   g_winStart[i] = s1;
   g_winSize[i]--;
  }

void TrimWindow(int i, long nowMs)
  {
   while(g_winSize[i] > 1 && nowMs - g_tickMs[g_winStart[i]] > g_core[i].windowMs)
      DropOldest(i);
  }

// +1 up spike, -1 down spike, 0 none. move/ticks/share describe the window.
int DetectSpike(int i, long nowMs, double &move, int &ticks, double &share)
  {
   move = 0.0; ticks = 0; share = 0.0;
   if(g_winSize[i] < 2) return(0);
   TrimWindow(i, nowMs);

   int newest = (g_tickHead - 1 + TICK_BUF) % TICK_BUF;
   ticks = g_winSize[i];
   move  = g_tickBid[newest] - g_tickBid[g_winStart[i]];
   int steps = g_winUps[i] + g_winDowns[i];
   if(steps == 0) return(0);
   share = (move > 0.0) ? (double)g_winUps[i] / steps : (double)g_winDowns[i] / steps;

   CoreCfg c = g_core[i];
   if(ticks < c.minTicks)           return(0);
   if(MathAbs(move) < c.trigger)    return(0);
   if(share < c.directional)        return(0);
   return(move > 0.0 ? 1 : -1);
  }

//+------------------------------------------------------------------+
//| DAILY STATE (both cores together)                                |
//+------------------------------------------------------------------+
void ResetDailyState(bool firstRun)
  {
   MqlDateTime t;
   TimeToStruct(ToGmt(TimeCurrent()), t);
   g_dayOfYear       = t.day_of_year;
   g_dayStartEquity  = AccountInfoDouble(ACCOUNT_EQUITY);
   g_dayRealisedPnl  = 0.0;
   g_tradesToday     = 0;
   g_consecLosses    = 0;
   g_halted          = false;
   g_haltReason      = "";
   g_lastHistoryScan = 0;
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

void ScanHistoryIfNeeded()
  {
   if(g_perfDirty) UpdatePerformance();
   datetime now = TimeCurrent();
   if(g_lastHistoryScan != 0 && now - g_lastHistoryScan < 1) return;
   g_lastHistoryScan = now;

   MqlDateTime t;
   TimeToStruct(ToGmt(now), t);
   t.hour = 0; t.min = 0; t.sec = 0;
   datetime dayStart = (datetime)((long)StructToTime(t) + (long)g_gmtOffsetHrs * 3600);
   if(!HistorySelect(dayStart, now + 60)) return;

   int trades = 0, streak = 0;
   double realised = 0.0;
   int total = HistoryDealsTotal();
   for(int i = 0; i < total; i++)
     {
      ulong tk = HistoryDealGetTicket(i);
      if(tk == 0) continue;
      if(HistoryDealGetString(tk, DEAL_SYMBOL) != _Symbol) continue;
      long magic = HistoryDealGetInteger(tk, DEAL_MAGIC);
      if(magic != g_core[0].magic && magic != g_core[1].magic) continue;
      long e = HistoryDealGetInteger(tk, DEAL_ENTRY);
      if(e != DEAL_ENTRY_OUT && e != DEAL_ENTRY_OUT_BY) continue;

      trades++;
      double p = HistoryDealGetDouble(tk, DEAL_PROFIT) + HistoryDealGetDouble(tk, DEAL_SWAP)
               + HistoryDealGetDouble(tk, DEAL_COMMISSION);
      realised += p;
      if(p < 0.0) streak++; else streak = 0;

      datetime dt = (datetime)HistoryDealGetInteger(tk, DEAL_TIME);
      for(int c = 0; c < 2; c++)
         if(magic == g_core[c].magic && dt > g_lastClose[c]) g_lastClose[c] = dt;
     }
   g_tradesToday    = trades;
   g_consecLosses   = streak;
   g_dayRealisedPnl = realised;

   double equity = AccountInfoDouble(ACCOUNT_EQUITY);
   double limit  = -MathAbs((equity > 0.0 ? equity : g_dayStartEquity) * InpMaxDailyLossPct / 100.0);
   if(!g_halted && InpMaxDailyLossPct > 0.0 && realised <= limit)
     {
      g_halted = true; g_haltReason = "daily loss limit";
      PrintFormat("HALT: realised %.2f today, past the %.1f%% limit. No more entries today.", realised, InpMaxDailyLossPct);
     }
   else if(!g_halted && InpMaxConsecLosses > 0 && streak >= InpMaxConsecLosses)
     {
      g_halted = true; g_haltReason = "loss streak";
      PrintFormat("HALT: %d losses in a row. No more entries today.", streak);
     }
  }

//+------------------------------------------------------------------+
//| HELPERS                                                          |
//+------------------------------------------------------------------+
double CurrentSpread()
  {
   return(SymbolInfoDouble(_Symbol, SYMBOL_ASK) - SymbolInfoDouble(_Symbol, SYMBOL_BID));
  }

double MinStopOffset()
  {
   double pointSize = SymbolInfoDouble(_Symbol, SYMBOL_POINT);
   return(MathMax(g_stopsLevelPts, g_freezeLevelPts) * pointSize);
  }

bool SelectCorePosition(const CoreCfg &c)
  {
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      if(!posInfo.SelectByIndex(i)) continue;
      if(posInfo.Symbol() != _Symbol) continue;
      if(posInfo.Magic() != c.magic) continue;
      return(true);
     }
   return(false);
  }

double NormalizeLots(double lots)
  {
   if(lots <= 0.0) return(0.0);
   lots = MathFloor(lots / g_volStep) * g_volStep;   // floor: never risk more than asked
   lots = MathMin(lots, g_volMax);
   if(lots < g_volMin) return(0.0);
   return(NormalizeDouble(lots, 2));
  }

double CalcLots(int i, double slDist)
  {
   CoreCfg c = g_core[i];
   if(c.fixedLots > 0.0) return(NormalizeLots(c.fixedLots));
   if(slDist <= 0.0) return(0.0);
   double riskCash   = AccountInfoDouble(ACCOUNT_EQUITY) * c.riskPct * g_perfMult[i] / 100.0;
   double lossPerLot = slDist / g_tickSize * g_tickValue;
   if(lossPerLot <= 0.0) return(0.0);
   return(NormalizeLots(riskCash / lossPerLot));
  }

void Block(int core, ENUM_BLOCK b)
  {
   g_blockCount[core][b]++;
  }

void ReportBlocks(string label)
  {
   for(int c = 0; c < 2; c++)
     {
      if(!g_core[c].enable) continue;
      long total = 0;
      for(int b = 0; b < BLK_COUNT; b++) total += g_blockCount[c][b];
      if(total == 0 && g_entries[c] == 0) continue;

      PrintFormat("---- XauSpike core %s %s report: %d entries | biggest move in window %.2f (trigger %.2f) ----",
                  g_coreName[c], label, g_entries[c], g_maxMoveSeen[c], g_core[c].trigger);
      for(int b = 0; b < BLK_COUNT; b++)
         if(g_blockCount[c][b] > 0)
            PrintFormat("   skipped %-18s %10I64d ticks (%.1f%%)", g_blockName[b], g_blockCount[c][b],
                        100.0 * g_blockCount[c][b] / (double)MathMax(total, 1));
      for(int b = 0; b < BLK_COUNT; b++) g_blockCount[c][b] = 0;
      g_maxMoveSeen[c] = 0.0;
      g_entries[c] = 0;
     }
  }

//+------------------------------------------------------------------+
//| How unusual is a move of this size over this window, right now?  |
//| "Normal" is the M1 ATR scaled to the window by sqrt(time), the   |
//| usual random-walk scaling. A fixed 3.00 trigger fires all day in |
//| a fast market and never in a slow one; a ratio asks the question |
//| that matters -- is this move out of character for the moment.   |
//+------------------------------------------------------------------+
double SpikeRatio(int i, double move)
  {
   double atr[];
   ArraySetAsSeries(atr, true);
   if(CopyBuffer(hAtrM1, 0, 1, 1, atr) != 1 || atr[0] <= 0.0) return(-1.0);
   double normal = atr[0] * MathSqrt(g_core[i].windowMs / 60000.0);
   if(normal <= 0.0) return(-1.0);
   return(MathAbs(move) / normal);
  }

//+------------------------------------------------------------------+
//| Share of the move made in the last third of the window. A spike  |
//| that did all its work early and has gone flat is exhausted: that |
//| is where the losing entries in testing came from. Walked only    |
//| once a spike has been found, so its cost does not matter.        |
//+------------------------------------------------------------------+
double FreshShare(int i, long nowMs, double move)
  {
   if(move == 0.0) return(0.0);
   long cutoff = nowMs - g_core[i].windowMs / 3;
   int  newest = (g_tickHead - 1 + TICK_BUF) % TICK_BUF;
   int  idx    = newest;
   for(int k = 1; k < g_winSize[i]; k++)
     {
      int prev = (idx - 1 + TICK_BUF) % TICK_BUF;
      if(g_tickMs[prev] < cutoff) break;
      idx = prev;
     }
   double recent = g_tickBid[newest] - g_tickBid[idx];
   return(recent / move);   // negative if the last third ran against the spike
  }

//+------------------------------------------------------------------+
//| ENTRY                                                            |
//+------------------------------------------------------------------+
void TryEntry(int i, datetime now, const MqlTick &tk)
  {
   CoreCfg c = g_core[i];

   if(g_halted || (InpMaxTradesPerDay > 0 && g_tradesToday >= InpMaxTradesPerDay))
     { Block(i, BLK_ACCOUNT); g_status[i] = (g_halted ? g_haltReason : "max trades/day"); return; }
   if(!TimeAllowsEntry(now))
     { Block(i, BLK_TIME); g_status[i] = "time filter"; return; }
   if(c.cooldownSec > 0 && g_lastClose[i] > 0 && now - g_lastClose[i] < c.cooldownSec)
     { Block(i, BLK_COOLDOWN); g_status[i] = "cooldown"; return; }

   long nowMs = (long)tk.time_msc;
   if(nowMs <= 0) nowMs = (long)tk.time * 1000;
   double move, share;
   int ticks;
   int dir = DetectSpike(i, nowMs, move, ticks, share);
   if(MathAbs(move) > g_maxMoveSeen[i]) g_maxMoveSeen[i] = MathAbs(move);
   if(dir == 0)
     {
      Block(i, BLK_NO_SPIKE);
      if(g_panel)
         g_status[i] = StringFormat("watching %+.2f / %.2f (%d ticks, %.0f%%)", move, c.trigger, ticks, share * 100.0);
      return;
     }

   double ratio = SpikeRatio(i, move);
   if(c.spikeRatio > 0.0 && ratio >= 0.0 && ratio < c.spikeRatio)
     {
      Block(i, BLK_WEAK_SPIKE);
      if(g_panel) g_status[i] = StringFormat("spike %.2f only %.1fx normal", move, ratio);
      return;
     }
   double fresh = FreshShare(i, nowMs, move);
   if(c.freshShare > 0.0 && fresh < c.freshShare)
     {
      Block(i, BLK_STALLED);
      if(g_panel) g_status[i] = StringFormat("spike %.2f stalled (%.0f%% recent)", move, fresh * 100.0);
      return;
     }

   // Checked only once a spike is found, so the report shows how often the
   // spread actually cost a trade rather than how wide it was all day.
   double spread = tk.ask - tk.bid;
   if(spread > g_maxSpreadPrice)
     { Block(i, BLK_SPREAD); g_status[i] = StringFormat("spike, but spread %.3f", spread); return; }
   if(!TerminalInfoInteger(TERMINAL_TRADE_ALLOWED) || !AccountInfoInteger(ACCOUNT_TRADE_EXPERT))
     { Block(i, BLK_AUTOTRADING); g_status[i] = "AutoTrading off"; return; }

   double minOff = MinStopOffset();
   double slDist = MathMax(c.sl, minOff);
   double tpDist = MathMax(c.tp, minOff);
   double lots   = CalcLots(i, slDist);
   if(lots <= 0.0)
     { Block(i, BLK_SIZE); g_status[i] = "lot below broker minimum"; return; }

   trade.SetExpertMagicNumber(c.magic);
   string comment = "XauSpikeT " + g_coreName[i];
   double price, sl, tp;
   bool ok;
   if(dir > 0)
     {
      price = tk.ask;
      sl = NormalizeDouble(price - slDist, _Digits);
      tp = NormalizeDouble(price + tpDist, _Digits);
      ok = trade.Buy(lots, _Symbol, 0.0, sl, tp, comment);
     }
   else
     {
      price = tk.bid;
      sl = NormalizeDouble(price + slDist, _Digits);
      tp = NormalizeDouble(price - tpDist, _Digits);
      ok = trade.Sell(lots, _Symbol, 0.0, sl, tp, comment);
     }

   uint rc = trade.ResultRetcode();
   if(!ok || (rc != TRADE_RETCODE_DONE && rc != TRADE_RETCODE_PLACED))
     {
      Block(i, BLK_ORDER_FAILED);
      g_status[i] = StringFormat("order failed %d", rc);
      PrintFormat("Core %s ORDER FAILED retcode=%d (%s) lots=%.2f", g_coreName[i], rc, trade.ResultRetcodeDescription(), lots);
      return;
     }

   g_entries[i]++;
   double fill = trade.ResultPrice();
   double slip = (fill > 0.0) ? ((dir > 0) ? fill - price : price - fill) : 0.0;
   g_status[i] = StringFormat("opened %s %.2f", (dir > 0 ? "BUY" : "SELL"), lots);
   PrintFormat("Core %s %s %.2f @ %.3f (quote %.3f, slip %+.3f) SL %.3f TP %.3f | spike %+.2f in %d ticks, %.0f%% one-way, %.1fx normal, %.0f%% in last third, spread %.3f",
               g_coreName[i], (dir > 0 ? "BUY" : "SELL"), lots, fill, price, slip, sl, tp, move, ticks,
               share * 100.0, ratio, fresh * 100.0, spread);
  }

//+------------------------------------------------------------------+
//| MANAGEMENT: forced flat, max hold, lock, trail. Stops only       |
//| ever move in the trade's favour.                                 |
//+------------------------------------------------------------------+
void ManagePosition(int i, datetime now, const MqlTick &tk)
  {
   CoreCfg c = g_core[i];
   ulong  ticket = posInfo.Ticket();
   long   type   = posInfo.PositionType();
   double openPx   = posInfo.PriceOpen();
   double sl     = posInfo.StopLoss();
   double tp     = posInfo.TakeProfit();
   trade.SetExpertMagicNumber(c.magic);

   bool   isBuy  = (type == POSITION_TYPE_BUY);
   double price  = isBuy ? tk.bid : tk.ask;          // the side the position closes on
   double profit = isBuy ? price - openPx : openPx - price;

   if(g_peakTicket[i] != ticket) { g_peakTicket[i] = ticket; g_peakProfit[i] = profit; }
   if(profit > g_peakProfit[i]) g_peakProfit[i] = profit;

   string why = "";
   bool   flat = g_halted && g_haltReason == "daily loss limit";
   if(flat) why = "daily loss limit";
   if(!flat) flat = MustBeFlat(now, why);
   if(!flat && c.maxHoldMin > 0 && now - (datetime)posInfo.Time() >= c.maxHoldMin * 60)
     {
      flat = true;
      why = StringFormat("held %d min", c.maxHoldMin);
     }
   // A real spike keeps going straight away. One that has not paid
   // NoFollowProfit within NoFollowSeconds has usually failed, and waiting
   // for the full stop turns a small loss into a full one.
   if(!flat && c.noFollowSec > 0 && now - (datetime)posInfo.Time() >= c.noFollowSec
      && g_peakProfit[i] < c.noFollowProfit)
     {
      flat = true;
      why = StringFormat("no follow-through (best %+.2f in %d s)", g_peakProfit[i], c.noFollowSec);
     }
   if(flat)
     {
      double pnl = posInfo.Profit();
      if(trade.PositionClose(ticket))
        {
         g_lastClose[i] = now;
         g_lastHistoryScan = 0;
         PrintFormat("Core %s closed #%I64u: %s, P/L ~%.2f", g_coreName[i], ticket, why, pnl);
        }
      return;
     }

   double newSl  = sl;

   if(c.lockTrigger > 0.0 && profit >= c.lockTrigger)
     {
      double lockSl = isBuy ? openPx + c.lockProfit : openPx - c.lockProfit;
      newSl = isBuy ? MathMax(newSl, lockSl) : (newSl == 0.0 ? lockSl : MathMin(newSl, lockSl));
     }
   if(c.trailStart > 0.0 && profit >= c.trailStart)
     {
      double trailSl = isBuy ? price - c.trailDist : price + c.trailDist;
      newSl = isBuy ? MathMax(newSl, trailSl) : (newSl == 0.0 ? trailSl : MathMin(newSl, trailSl));
     }

   // Respect the broker's minimum distance from the current price.
   double minOff = MinStopOffset();
   if(minOff > 0.0)
      newSl = isBuy ? MathMin(newSl, price - minOff) : MathMax(newSl, price + minOff);

   newSl = NormalizeDouble(newSl, _Digits);
   bool better = isBuy ? (newSl > sl + InpTrailStep - 1e-9) : (sl == 0.0 || newSl < sl - InpTrailStep + 1e-9);
   if(g_panel)
      g_status[i] = StringFormat("in trade %+.2f  SL %.3f", profit, sl);
   if(!better) return;

   if(trade.PositionModify(ticket, newSl, tp))
     {
      if(g_panel) g_status[i] = StringFormat("in trade %+.2f  SL -> %.3f", profit, newSl);
     }
   else
      PrintFormat("Core %s modify #%I64u -> %.3f failed: %d (%s)", g_coreName[i], ticket, newSl,
                  trade.ResultRetcode(), trade.ResultRetcodeDescription());
  }

//+------------------------------------------------------------------+
//| PANEL                                                            |
//+------------------------------------------------------------------+
void DrawPanel()
  {
   double equity = AccountInfoDouble(ACCOUNT_EQUITY);
   string txt = StringFormat(
      "XauSpike TEST (fixed lots)  |  %s   spread %.3f (max %.2f)\n"
      "-----------------------------------------\n"
      "equity %.2f   day P/L %.2f   trades %d/%d   streak %d/%d\n"
      "core A: %s   (risk x%.2f, last %d: %+.2f/oz)\n"
      "core B: %s   (risk x%.2f, last %d: %+.2f/oz)%s",
      _Symbol, CurrentSpread(), g_maxSpreadPrice,
      equity, g_dayRealisedPnl, g_tradesToday, InpMaxTradesPerDay, g_consecLosses, InpMaxConsecLosses,
      (g_core[0].enable ? g_status[0] : "disabled"), g_perfMult[0], g_perfN[0], g_perfSum[0],
      (g_core[1].enable ? g_status[1] : "disabled"), g_perfMult[1], g_perfN[1], g_perfSum[1],
      (g_halted ? ("\nHALTED: " + g_haltReason) : ""));
   Comment(txt);
  }
//+------------------------------------------------------------------+
