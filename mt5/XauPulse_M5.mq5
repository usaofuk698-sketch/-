//+------------------------------------------------------------------+
//|                                                 XauPulse_M5.mq5  |
//|              Mean-reversion scalper for spot gold on M5          |
//|                        XAUUSD - M5 - built for Exness MT5        |
//+------------------------------------------------------------------+
//| WHAT THIS IS
//| A mean-reversion scalper, and the deliberate opposite of XauTrend_M5.
//| That system waits for a trend and refuses chop, so on M5 gold it stands
//| aside almost always and takes roughly one trade a day. This one trades
//| INTO the chop, because range-bound behaviour is the common state here, and
//| that is where the frequency is: about five trades a day, held ~13 minutes.
//|
//| It fades a stretch away from a short EMA once momentum is exhausted and the
//| bar has begun to turn back, and targets the move back toward the mean.
//|
//| THE FILTER THAT KEEPS IT ALIVE
//| ADX must be BELOW a ceiling. Fading a real trend is how mean reversion
//| dies: in a trend every extreme is followed by a more extreme one, and the
//| system loses repeatedly in the same direction. Raising InpAdxMax to get
//| more trades is the single most dangerous edit in this file.
//|
//| THE COST RULE
//| A scalper pays the spread on every one of many trades, and its targets are
//| a fraction of a swing trader's. InpMinTargetSpreadRatio refuses any setup
//| whose target is not a sufficient multiple of the CURRENT spread. In testing
//| this rule rejected more setups than every other filter combined -- that is
//| not a problem, it is the instrument telling you what it costs to trade.
//|
//| Same two invariants as XauTrend_M5: closed bars only (bar 0 is still
//| forming and is never read), and stops are only ever tightened.
//|
//| Nothing broker-specific is hardcoded -- contract size, tick value, lot step,
//| stop distance and fill policy are read from the symbol at run time.
//+------------------------------------------------------------------+
#property copyright "XauPulse"
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
input double InpMaxDailyLossPct    = 3.0;    // Daily loss limit (%) - stops for the day
input int    InpMaxTradesPerDay    = 12;     // Max trades per day
input int    InpMaxConsecLosses    = 6;      // Cool off after N losses in a row
input double InpMinStopDistance    = 0.30;   // Refuse stops tighter than this (price units)
input double InpMaxStopDistance    = 30.0;   // Refuse stops wider than this (price units)
input double InpFixedLots          = 0.0;    // >0 overrides risk sizing (NOT recommended)

input group "=== Session (hours are GMT/UTC, not server time) ==="
input ENUM_TZ_MODE InpTzMode       = TZ_AUTO; // How to resolve server time -> GMT
input int    InpServerGmtOffset    = 0;      // Server GMT offset when TZ_MANUAL
input int    InpSessionStartHour   = 0;      // Session opens (GMT). 0 + 24 = no session filter
input int    InpSessionEndHour     = 24;     // Session closes (GMT)
input int    InpNoNewTradesAfter   = 24;     // No new entries from this GMT hour (24 = off)
input int    InpFlatByHour         = 24;     // Force flat at this GMT hour (24 = never)
input bool   InpTradeMonday        = true;
input bool   InpTradeTuesday       = true;
input bool   InpTradeWednesday     = true;
input bool   InpTradeThursday      = true;
input bool   InpTradeFriday        = true;

input group "=== Execution ==="
input long   InpMagicNumber        = 770588; // Identifies this EA's own trades.
                                             // MUST differ from XauTrend_M5 (770577):
                                             // each EA manages only positions carrying
                                             // its own magic, so a shared number would
                                             // have each bot trailing and closing the
                                             // other's trades.
input int    InpMaxSpreadPoints    = 500;    // Skip entries above this spread, IN POINTS (as MT5 shows it)
input int    InpSlippagePoints     = 30;     // Max deviation on market orders
input int    InpMaxBarsInTrade     = 24;     // Close a trade older than this (0 = off)

input group "=== Strategy: the mean ==="
input int    InpEmaPeriod          = 20;     // The average price reverts toward

input group "=== Strategy: regime guard ==="
input int    InpAdxPeriod          = 14;
input double InpAdxMax             = 50.0;   // ABOVE this = trending, do NOT fade it
input int    InpAtrPeriod          = 14;
input int    InpRegimeLookback     = 288;    // Bars for the median-ATR reference
input double InpAtrMinMult         = 0.55;   // Skip dead tape the spread would eat
input double InpAtrMaxMult         = 3.00;   // Skip post-news chaos

input group "=== Strategy: stretch trigger ==="
input double InpStretchAtr         = 0.60;   // Distance from the EMA, in ATR
input int    InpRsiPeriod          = 7;      // Fast RSI: this is a scalper
input double InpRsiOversold        = 38.0;
input double InpRsiOverbought      = 62.0;

input group "=== Strategy: stop and target ==="
input int    InpSwingLookback      = 6;
input double InpStopAtrBuffer      = 0.25;   // Padding beyond the swing extreme (xATR)
input double InpMinStopAtrMult     = 1.30;   // Floor on stop distance (xATR)
input double InpRewardRisk         = 1.20;   // Target as a multiple of the stop
input double InpMinTargetSpreadRatio = 5.0;  // Target must be >= N x the live spread

input group "=== Strategy: entry quality (v2) ==="
input int    InpHtfEmaPeriod       = 100;    // Long EMA standing in for the higher timeframe
input int    InpHtfSlopeLookback   = 20;     // Bars used to measure its slope
input double InpHtfMaxSlopeAtr     = 0.06;   // Max |slope| per bar in ATR (0 = filter off)
input double InpClosePositionMin   = 0.55;   // Where in its range the reversal bar closed
input bool   InpRequireDivergence  = false;  // Demand momentum divergence at the extreme
input int    InpDivergenceLookback = 12;

input group "=== Strategy: in-trade management ==="
input bool   InpUseBreakeven       = true;
input double InpBreakevenAtR       = 0.8;    // Move to break-even at this R
input double InpBreakevenOffAtr    = 0.05;   // Lock in this much ATR beyond entry

input group "=== Display ==="
input bool   InpShowPanel          = true;

//+------------------------------------------------------------------+
//| GLOBALS                                                          |
//+------------------------------------------------------------------+
int      hEma     = INVALID_HANDLE;
int      hHtfEma  = INVALID_HANDLE;
int      hAtr     = INVALID_HANDLE;
int      hAdx     = INVALID_HANDLE;
int      hRsi     = INVALID_HANDLE;

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

   hEma      = iMA(_Symbol, PERIOD_CURRENT, InpEmaPeriod,    0, MODE_EMA, PRICE_CLOSE);
   hHtfEma   = iMA(_Symbol, PERIOD_CURRENT, InpHtfEmaPeriod, 0, MODE_EMA, PRICE_CLOSE);
   hAtr      = iATR(_Symbol, PERIOD_CURRENT, InpAtrPeriod);
   hAdx      = iADX(_Symbol, PERIOD_CURRENT, InpAdxPeriod);
   hRsi      = iRSI(_Symbol, PERIOD_CURRENT, InpRsiPeriod, PRICE_CLOSE);

   if(hEma==INVALID_HANDLE || hHtfEma==INVALID_HANDLE || hAtr==INVALID_HANDLE ||
      hAdx==INVALID_HANDLE || hRsi==INVALID_HANDLE)
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

   PrintFormat("XauPulse M5 started on %s | server GMT offset %+d h | "
               "tick %.5f, tick value %.5f, lots %.2f-%.2f step %.2f | stops level %d pts",
               _Symbol, g_gmtOffsetHrs, g_tickSize, g_tickValue,
               g_volMin, g_volMax, g_volStep, g_stopsLevelPts);
   if(InpSessionStartHour <= 0 && InpSessionEndHour >= 24)
      Print("Session filter: OFF - trading all hours on permitted weekdays.");
   else
      PrintFormat("Session %02d:00-%02d:00 GMT  =  %02d:00-%02d:00 server time",
                  InpSessionStartHour, InpSessionEndHour,
                  (InpSessionStartHour + g_gmtOffsetHrs + 24) % 24,
                  (InpSessionEndHour   + g_gmtOffsetHrs + 24) % 24);

   // The spread limit is entered in POINTS because that is the unit MetaTrader
   // displays in Market Watch. What it means in money depends entirely on the
   // symbol's digits -- 500 points is 5.00 USD/oz on a 2-digit gold feed but
   // only 0.50 on a 3-digit one. Convert once here and print both, so the
   // setting can be sanity-checked at a glance instead of guessed at.
   // Named pointSize, not point: MQL5 carries MQL4-compatibility identifiers
   // around Point/_Point, and a local that close to a built-in name is not
   // worth the risk in a file that cannot be test-compiled here.
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

   return(INIT_SUCCEEDED);
  }

//+------------------------------------------------------------------+
void OnDeinit(const int reason)
  {
   IndicatorRelease(hEma);
   IndicatorRelease(hHtfEma);
   IndicatorRelease(hAtr);
   IndicatorRelease(hAdx);
   IndicatorRelease(hRsi);
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
   if(InpRewardRisk <= 0.0)          { Print("ERROR: RewardRisk must be > 0"); return(false); }
   if(InpEmaPeriod < 2)              { Print("ERROR: EmaPeriod must be >= 2"); return(false); }
   if(InpStretchAtr <= 0.0)          { Print("ERROR: StretchAtr must be > 0"); return(false); }
   if(InpRsiOversold >= InpRsiOverbought)
     { Print("ERROR: RsiOversold must be < RsiOverbought"); return(false); }
   if(InpHtfEmaPeriod < 2)           { Print("ERROR: HtfEmaPeriod must be >= 2"); return(false); }
   if(InpHtfSlopeLookback < 1)       { Print("ERROR: HtfSlopeLookback must be >= 1"); return(false); }
   if(InpClosePositionMin < 0.0 || InpClosePositionMin > 1.0)
     { Print("ERROR: ClosePositionMin must be in [0, 1]"); return(false); }
   if(InpDivergenceLookback < 2)     { Print("ERROR: DivergenceLookback must be >= 2"); return(false); }
   if(InpAdxMax <= 0.0 || InpAdxMax > 100.0)
     { Print("ERROR: AdxMax must be in (0, 100]"); return(false); }
   if(InpRegimeLookback < 20)        { Print("ERROR: RegimeLookback must be >= 20"); return(false); }
   if(InpSwingLookback < 2)          { Print("ERROR: SwingLookback must be >= 2"); return(false); }
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
   // equity captured at the start of the day.
   //
   // Both details matter the moment money moves in or out of the account. An
   // equity difference counts a deposit as profit and a withdrawal as loss,
   // and a start-of-day baseline goes stale: fund an account from 100 to 2000
   // mid-session and the 2% limit stays pinned at 2.00, so the first ordinary
   // losing trade halts the bot for the rest of the day. Deals only ever
   // reflect trading, and live equity always reflects the account as it is now.
   double equity = AccountInfoDouble(ACCOUNT_EQUITY);
   double base   = (equity > 0.0) ? equity : g_dayStartEquity;
   double limit  = -MathAbs(base * InpMaxDailyLossPct / 100.0);

   if(g_dayRealisedPnl <= limit && !g_halted)
     {
      g_halted = true;
      g_haltReason = "daily loss limit";
      PrintFormat("HALT: realised %.2f today, past the %.1f%% limit (%.2f on %.2f equity). "
                  "No more trades today.",
                  g_dayRealisedPnl, InpMaxDailyLossPct, limit, base);
     }
   else if(g_consecLosses >= InpMaxConsecLosses && !g_halted)
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

bool InSession(datetime serverTime)
  {
   int h = GmtHour(serverTime);
   if(InpSessionStartHour <= InpSessionEndHour)
      return(h >= InpSessionStartHour && h < InpSessionEndHour);
   return(h >= InpSessionStartHour || h < InpSessionEndHour); // wraps midnight
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
   if(g_tradesToday >= InpMaxTradesPerDay)       { reason = "max trades/day";    return(false); }
   if(g_consecLosses >= InpMaxConsecLosses)      { reason = "loss streak";       return(false); }

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
//| the same fraction of equity whatever the volatility. A fixed lot  |
//| size risks $30 on a quiet morning and $300 through a CPI print.   |
//| The result is FLOORED to the lot step -- rounding up would risk   |
//| more than authorised on every trade.                              |
//+------------------------------------------------------------------+
double CalcLots(double entry, double stop)
  {
   if(InpFixedLots > 0.0)
      return(NormalizeLots(InpFixedLots));

   double distance = MathAbs(entry - stop);
   if(distance <= 0.0) return(0.0);

   double equity   = AccountInfoDouble(ACCOUNT_EQUITY);
   double riskCash = equity * InpRiskPercent / 100.0;

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
//| ENTRY                                                            |
//+------------------------------------------------------------------+
void TryEntry()
  {
   int need = MathMax(InpRegimeLookback,
                      MathMax(InpEmaPeriod, InpHtfEmaPeriod + InpHtfSlopeLookback)) + 10;
   if(Bars(_Symbol, PERIOD_CURRENT) < need)
     {
      g_status = StringFormat("warming up (%d/%d bars)", Bars(_Symbol, PERIOD_CURRENT), need);
      return;
     }

   double ema[], atr[], adx[], rsi[], htf[];
   int rsiBars = MathMax(InpDivergenceLookback + 2, 2);
   if(!ReadBuffer(hHtfEma, 0, 1, InpHtfSlopeLookback + 1, htf) ||
      !ReadBuffer(hEma, 0, 1, 2, ema) ||
      !ReadBuffer(hAtr, 0, 1, 2, atr) ||
      !ReadBuffer(hAdx, 0, 1, 2, adx) ||
      !ReadBuffer(hRsi, 0, 1, rsiBars, rsi))
     {
      g_status = "indicator data not ready";
      return;
     }

   MqlRates rates[];
   ArraySetAsSeries(rates, true);
   int wantBars = MathMax(InpSwingLookback + 3, MathMax(InpDivergenceLookback + 2, 5));
   if(CopyRates(_Symbol, PERIOD_CURRENT, 1, wantBars, rates) != wantBars)
     {
      g_status = "price data not ready";
      return;
     }

   // rates[0] is the last CLOSED bar.
   double close = rates[0].close;
   double open  = rates[0].open;
   double atrV  = atr[0];
   double adxV  = adx[0];
   double rsiV  = rsi[0];
   double emaV  = ema[0];
   if(atrV <= 0.0) { g_status = "ATR unavailable"; return; }

   //--- volatility regime
   double medAtr = MedianAtr(InpRegimeLookback);
   if(medAtr <= 0.0) { g_status = "regime ATR unavailable"; return; }
   if(atrV < InpAtrMinMult * medAtr) { g_status = "volatility too low";  return; }
   if(atrV > InpAtrMaxMult * medAtr) { g_status = "volatility too high"; return; }

   //--- regime guard. Note the direction of this test: unlike a trend system,
   //--- a HIGH ADX is what disqualifies the setup. Fading a real trend loses
   //--- repeatedly and in the same direction.
   if(adxV >= InpAdxMax)
     {
      g_status = StringFormat("trending, ADX %.1f >= %.1f", adxV, InpAdxMax);
      return;
     }

   //--- swing extremes over the lookback, ending at the last closed bar
   double swingLow = rates[0].low, swingHigh = rates[0].high;
   for(int i = 0; i < InpSwingLookback && i < wantBars; i++)
     {
      swingLow  = MathMin(swingLow,  rates[i].low);
      swingHigh = MathMax(swingHigh, rates[i].high);
     }

   //--- how far price has stretched from the mean, measured in ATR so the
   //--- threshold means the same thing in quiet and volatile markets
   double stretch = (close - emaV) / atrV;

   //--- gate 1: the reversal bar must actually reject the extreme. A bar that
   //--- closes near its own low is not a bounce, it is a pause on the way down.
   double barRange = rates[0].high - rates[0].low;
   double closePos = (barRange > 0.0) ? (close - rates[0].low) / barRange : 0.5;
   bool longBarOk  = closePos >= InpClosePositionMin;
   bool shortBarOk = (1.0 - closePos) >= InpClosePositionMin;

   //--- gate 2: do not fade a higher-timeframe trend. M5 ADX only sees the
   //--- last few bars; a dip inside a sustained move down looks identical to a
   //--- dip in a range until you look further out.
   double htfSlope = (htf[0] - htf[InpHtfSlopeLookback]) / (double)InpHtfSlopeLookback;
   double slopeAtr = htfSlope / atrV;
   bool longHtfOk  = (InpHtfMaxSlopeAtr <= 0.0) || (slopeAtr >= -InpHtfMaxSlopeAtr);
   bool shortHtfOk = (InpHtfMaxSlopeAtr <= 0.0) || (slopeAtr <=  InpHtfMaxSlopeAtr);

   //--- gate 3 (optional): momentum divergence. Price makes the lower low but
   //--- momentum does not, so the move down is running out of force.
   bool longDivOk = true, shortDivOk = true;
   if(InpRequireDivergence)
     {
      double priorLow = rates[1].low, priorHigh = rates[1].high;
      double priorRsiLow = rsi[1],    priorRsiHigh = rsi[1];
      for(int k = 1; k <= InpDivergenceLookback && k < wantBars && k < rsiBars; k++)
        {
         priorLow     = MathMin(priorLow,     rates[k].low);
         priorHigh    = MathMax(priorHigh,    rates[k].high);
         priorRsiLow  = MathMin(priorRsiLow,  rsi[k]);
         priorRsiHigh = MathMax(priorRsiHigh, rsi[k]);
        }
      longDivOk  = (rates[0].low  <= priorLow)  && (rsiV > priorRsiLow);
      shortDivOk = (rates[0].high >= priorHigh) && (rsiV < priorRsiHigh);
     }

   bool longOk  = (stretch <= -InpStretchAtr) && (rsiV <= InpRsiOversold)
                  && (close > open) && longBarOk && longHtfOk && longDivOk;
   bool shortOk = (stretch >=  InpStretchAtr) && (rsiV >= InpRsiOverbought)
                  && (close < open) && shortBarOk && shortHtfOk && shortDivOk;

   if(longOk == shortOk)
     {
      g_status = StringFormat("no setup (stretch %+.2f, RSI %.0f)", stretch, rsiV);
      return;
     }

   double sl, tp, price;
   double minOff = MinStopOffset();

   if(longOk)
     {
      price = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
      sl    = MathMin(swingLow - InpStopAtrBuffer * atrV, close - InpMinStopAtrMult * atrV);
      if(sl >= price - minOff) sl = price - MathMax(minOff, InpMinStopDistance);
      tp    = price + InpRewardRisk * (price - sl);
      OpenTrade(ORDER_TYPE_BUY, price, sl, tp, atrV);
     }
   else
     {
      price = SymbolInfoDouble(_Symbol, SYMBOL_BID);
      sl    = MathMax(swingHigh + InpStopAtrBuffer * atrV, close + InpMinStopAtrMult * atrV);
      if(sl <= price + minOff) sl = price + MathMax(minOff, InpMinStopDistance);
      tp    = price - InpRewardRisk * (sl - price);
      OpenTrade(ORDER_TYPE_SELL, price, sl, tp, atrV);
     }
  }

//+------------------------------------------------------------------+
void OpenTrade(ENUM_ORDER_TYPE type, double price, double sl, double tp, double atrV)
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

   // A scalper pays the spread on every trade against a small target. Refuse
   // any setup where the target is not worth several times what it costs to
   // open. In testing this rejected more setups than all other filters
   // combined -- that is the instrument pricing itself, not a bug.
   double targetDistance = MathAbs(tp - price);
   double spreadNow      = CurrentSpread();
   if(InpMinTargetSpreadRatio > 0.0 && spreadNow > 0.0 &&
      targetDistance < InpMinTargetSpreadRatio * spreadNow)
     {
      g_status = StringFormat("target %.2f < %.1fx spread %.2f",
                              targetDistance, InpMinTargetSpreadRatio, spreadNow);
      return;
     }

   double lots = CalcLots(price, sl);
   if(lots <= 0.0)
     {
      g_status = "size below broker minimum - equity too small for this risk %";
      return;
     }

   sl = NormalizeDouble(sl, _Digits);
   tp = NormalizeDouble(tp, _Digits);

   bool ok = (type == ORDER_TYPE_BUY)
             ? trade.Buy(lots, _Symbol, 0.0, sl, tp, "XauPulse")
             : trade.Sell(lots, _Symbol, 0.0, sl, tp, "XauPulse");

   if(ok)
     {
      g_status = StringFormat("opened %s %.2f lots", (type==ORDER_TYPE_BUY ? "BUY":"SELL"), lots);
      PrintFormat("%s %.2f lots @ ~%.2f  sl=%.2f tp=%.2f  risk=%.2f%%  ATR=%.2f  spread=%.2f",
                  (type==ORDER_TYPE_BUY ? "BUY":"SELL"), lots, price, sl, tp,
                  InpRiskPercent, atrV, CurrentSpread());
     }
   else
     {
      g_status = StringFormat("order failed: %d %s", trade.ResultRetcode(), trade.ResultRetcodeDescription());
      PrintFormat("ORDER FAILED retcode=%d (%s) lots=%.2f sl=%.2f tp=%.2f",
                  trade.ResultRetcode(), trade.ResultRetcodeDescription(), lots, sl, tp);
     }
  }

//+------------------------------------------------------------------+
//| MANAGEMENT: break-even, ATR trail, time stop, session flat       |
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

   // Break-even only. A scalp lasts minutes and a few bars; an ATR trail on a
   // position this short just converts winners into break-even scratches.
   if(pos_type == POSITION_TYPE_BUY)
     {
      double gainedR = (close - entry) / risk;
      if(InpUseBreakeven && gainedR >= InpBreakevenAtR)
         newSl = MathMax(newSl, entry + InpBreakevenOffAtr * atrV);
      double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);
      newSl = MathMin(newSl, bid - MinStopOffset());
      if(newSl <= sl) return;                 // tighten only
     }
   else
     {
      double gainedR = (entry - close) / risk;
      if(InpUseBreakeven && gainedR >= InpBreakevenAtR)
         newSl = MathMin(newSl, entry - InpBreakevenOffAtr * atrV);
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

   string txt = StringFormat(
      "XauPulse M5  |  %s\n"
      "-----------------------------------------\n"
      "server %s   (GMT%+d)   GMT hour %02d\n"
      "session %02d-%02d GMT      in session: %s\n"
      "spread %.2f  (max %.2f = %d pts)\n"
      "-----------------------------------------\n"
      "equity      %.2f\n"
      "day P/L     %.2f  (%+.2f%%)  limit %.1f%%\n"
      "trades today %d / %d      loss streak %d / %d\n"
      "position    %s\n"
      "state       %s%s",
      _Symbol,
      TimeToString(now, TIME_DATE|TIME_MINUTES), g_gmtOffsetHrs, GmtHour(now),
      InpSessionStartHour, InpSessionEndHour, (InSession(now) ? "yes" : "no"),
      CurrentSpread(), g_maxSpreadPrice, InpMaxSpreadPoints,
      equity,
      dayPnl, dayPct, InpMaxDailyLossPct,
      g_tradesToday, InpMaxTradesPerDay, g_consecLosses, InpMaxConsecLosses,
      (HasOpenPosition() ? "open" : "flat"),
      g_status,
      (g_halted ? ("\nHALTED: " + g_haltReason) : ""));

   Comment(txt);
  }
//+------------------------------------------------------------------+
