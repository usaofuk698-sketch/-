//+------------------------------------------------------------------+
//|                                                 XauTrend_M5.mq5  |
//|            Session trend-pullback expert advisor for spot gold   |
//|                        XAUUSD - M5 - built for Exness MT5        |
//+------------------------------------------------------------------+
//| PORT NOTE                                                        |
//| This is a direct port of the Python research engine in this      |
//| repository. The entry conditions, stop placement, sizing formula |
//| and risk limits are identical on purpose: if the live EA and the |
//| backtested system differ, the backtest describes nothing.        |
//|                                                                  |
//| Two rules carried over, because they are what make the two match:|
//|   1. The EA acts ONLY on closed bars. Bar 0 is still forming --  |
//|      its high, low and close are still moving -- so every read   |
//|      here uses shift 1 or older.                                 |
//|   2. Stops are revised only in the position's favour, never      |
//|      loosened.                                                   |
//|                                                                  |
//| BROKER NEUTRALITY                                                |
//| Nothing about contract size, tick value, lot step, stop distance |
//| or fill policy is hardcoded. Everything is read from the symbol  |
//| at run time, so the same file works on an Exness Standard,       |
//| Raw Spread, Zero or Cent account, and on XAUUSD, XAUUSDm or any  |
//| other suffixed gold symbol, without edits.                       |
//+------------------------------------------------------------------+
#property copyright "XauTrend"
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
input double InpMaxDailyLossPct    = 2.0;    // Daily loss limit (%) - stops for the day
input int    InpMaxTradesPerDay    = 5;      // Max trades per day
input int    InpMaxConsecLosses    = 4;      // Cool off after N losses in a row
input double InpMinStopDistance    = 0.50;   // Refuse stops tighter than this (price units)
input double InpMaxStopDistance    = 30.0;   // Refuse stops wider than this (price units)
input double InpFixedLots          = 0.0;    // >0 overrides risk sizing (NOT recommended)

input group "=== Session (hours are GMT/UTC, not server time) ==="
input ENUM_TZ_MODE InpTzMode       = TZ_AUTO; // How to resolve server time -> GMT
input int    InpServerGmtOffset    = 0;      // Server GMT offset when TZ_MANUAL
input int    InpSessionStartHour   = 7;      // Session opens (GMT)
input int    InpSessionEndHour     = 16;     // Session closes (GMT)
input int    InpNoNewTradesAfter   = 16;     // No new entries from this GMT hour
input int    InpFlatByHour         = 20;     // Force flat at this GMT hour
input bool   InpTradeMonday        = true;
input bool   InpTradeTuesday       = true;
input bool   InpTradeWednesday     = true;
input bool   InpTradeThursday      = true;
input bool   InpTradeFriday        = true;

input group "=== Execution ==="
input long   InpMagicNumber        = 770577; // Identifies this EA's own trades
input double InpMaxSpread          = 0.60;   // Skip entries above this spread (price units)
input int    InpSlippagePoints     = 30;     // Max deviation on market orders
input int    InpMaxBarsInTrade     = 96;     // Close a trade older than this (0 = off)

input group "=== Strategy: direction ==="
input int    InpEmaFast            = 21;
input int    InpEmaSlow            = 55;
input int    InpEmaTrend           = 200;

input group "=== Strategy: filters ==="
input int    InpAdxPeriod          = 14;
input double InpAdxMin             = 22.0;   // Chop filter
input int    InpAtrPeriod          = 14;
input int    InpRegimeLookback     = 288;    // Bars for the median-ATR reference
input double InpAtrMinMult         = 0.80;   // Skip dead tape below this x median ATR
input double InpAtrMaxMult         = 2.50;   // Skip post-news expansion above it

input group "=== Strategy: pullback trigger ==="
input int    InpRsiPeriod          = 14;
input double InpRsiPullbackLong    = 45.0;
input double InpRsiPullbackShort   = 55.0;
input int    InpPullbackLookback   = 8;

input group "=== Strategy: stop and target ==="
input int    InpSwingLookback      = 12;
input double InpStopAtrBuffer      = 0.35;   // Padding beyond the swing extreme (xATR)
input double InpMinStopAtrMult     = 1.00;   // Floor on stop distance (xATR)
input double InpRewardRisk         = 1.8;    // Target as a multiple of the stop

input group "=== Strategy: in-trade management ==="
input bool   InpUseBreakeven       = true;
input double InpBreakevenAtR       = 1.0;    // Move to break-even at this R
input double InpBreakevenOffAtr    = 0.10;   // Lock in this much ATR beyond entry
input bool   InpUseTrailing        = true;
input double InpTrailAtR           = 1.5;    // Start trailing at this R
input double InpTrailAtrMult       = 1.5;    // Trail this far behind price (xATR)

input group "=== Display ==="
input bool   InpShowPanel          = true;

//+------------------------------------------------------------------+
//| GLOBALS                                                          |
//+------------------------------------------------------------------+
int      hEmaFast = INVALID_HANDLE;
int      hEmaSlow = INVALID_HANDLE;
int      hEmaTrend= INVALID_HANDLE;
int      hAtr     = INVALID_HANDLE;
int      hAdx     = INVALID_HANDLE;
int      hRsi     = INVALID_HANDLE;

datetime g_lastBarTime   = 0;
int      g_gmtOffsetHrs  = 0;

// --- per-day state
int      g_dayOfYear     = -1;
double   g_dayStartEquity= 0.0;
int      g_tradesToday   = 0;
int      g_consecLosses  = 0;
bool     g_halted        = false;
string   g_haltReason    = "";

// --- symbol spec, resolved once in OnInit
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

   hEmaFast  = iMA(_Symbol, PERIOD_CURRENT, InpEmaFast,  0, MODE_EMA, PRICE_CLOSE);
   hEmaSlow  = iMA(_Symbol, PERIOD_CURRENT, InpEmaSlow,  0, MODE_EMA, PRICE_CLOSE);
   hEmaTrend = iMA(_Symbol, PERIOD_CURRENT, InpEmaTrend, 0, MODE_EMA, PRICE_CLOSE);
   hAtr      = iATR(_Symbol, PERIOD_CURRENT, InpAtrPeriod);
   hAdx      = iADX(_Symbol, PERIOD_CURRENT, InpAdxPeriod);
   hRsi      = iRSI(_Symbol, PERIOD_CURRENT, InpRsiPeriod, PRICE_CLOSE);

   if(hEmaFast==INVALID_HANDLE || hEmaSlow==INVALID_HANDLE || hEmaTrend==INVALID_HANDLE ||
      hAtr==INVALID_HANDLE || hAdx==INVALID_HANDLE || hRsi==INVALID_HANDLE)
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

   PrintFormat("XauTrend M5 started on %s | server GMT offset %+d h | "
               "tick %.5f, tick value %.5f, lots %.2f-%.2f step %.2f | stops level %d pts",
               _Symbol, g_gmtOffsetHrs, g_tickSize, g_tickValue,
               g_volMin, g_volMax, g_volStep, g_stopsLevelPts);
   PrintFormat("Session %02d:00-%02d:00 GMT  =  %02d:00-%02d:00 server time",
               InpSessionStartHour, InpSessionEndHour,
               (InpSessionStartHour + g_gmtOffsetHrs + 24) % 24,
               (InpSessionEndHour   + g_gmtOffsetHrs + 24) % 24);
   return(INIT_SUCCEEDED);
  }

//+------------------------------------------------------------------+
void OnDeinit(const int reason)
  {
   IndicatorRelease(hEmaFast);
   IndicatorRelease(hEmaSlow);
   IndicatorRelease(hEmaTrend);
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
   if(InpEmaFast >= InpEmaSlow)      { Print("ERROR: EmaFast must be < EmaSlow"); return(false); }
   if(InpRegimeLookback < 20)        { Print("ERROR: RegimeLookback must be >= 20"); return(false); }
   if(InpSwingLookback < 2)          { Print("ERROR: SwingLookback must be >= 2"); return(false); }
   if(InpPullbackLookback < 2)       { Print("ERROR: PullbackLookback must be >= 2"); return(false); }
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

   int trades = 0;
   int streak = 0;
   int total  = HistoryDealsTotal();

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
      if(profit < 0.0) streak++;
      else             streak = 0;
     }

   g_tradesToday  = trades;
   g_consecLosses = streak;

   double equity = AccountInfoDouble(ACCOUNT_EQUITY);
   double dayPnl = equity - g_dayStartEquity;
   double limit  = -MathAbs(g_dayStartEquity * InpMaxDailyLossPct / 100.0);

   if(dayPnl <= limit && !g_halted)
     {
      g_halted = true;
      g_haltReason = "daily loss limit";
      PrintFormat("HALT: daily loss %.2f reached the %.1f%% limit (%.2f). No more trades today.",
                  dayPnl, InpMaxDailyLossPct, limit);
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
   if(spread > InpMaxSpread)
     {
      reason = StringFormat("spread %.2f > %.2f", spread, InpMaxSpread);
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
   int need = MathMax(InpRegimeLookback, InpEmaTrend) + 10;
   if(Bars(_Symbol, PERIOD_CURRENT) < need)
     {
      g_status = StringFormat("warming up (%d/%d bars)", Bars(_Symbol, PERIOD_CURRENT), need);
      return;
     }

   double emaF[], emaS[], emaT[], atr[], adx[], rsi[];
   int rsiCount = InpPullbackLookback + 2;

   if(!ReadBuffer(hEmaFast, 0, 1, 2, emaF) ||
      !ReadBuffer(hEmaSlow, 0, 1, 2, emaS) ||
      !ReadBuffer(hEmaTrend,0, 1, 2, emaT) ||
      !ReadBuffer(hAtr,     0, 1, 2, atr)  ||
      !ReadBuffer(hAdx,     0, 1, 2, adx)  ||
      !ReadBuffer(hRsi,     0, 1, rsiCount, rsi))
     {
      g_status = "indicator data not ready";
      return;
     }

   MqlRates rates[];
   ArraySetAsSeries(rates, true);
   int wantBars = MathMax(InpSwingLookback + 3, 5);
   if(CopyRates(_Symbol, PERIOD_CURRENT, 1, wantBars, rates) != wantBars)
     {
      g_status = "price data not ready";
      return;
     }

   // rates[0] is the last CLOSED bar; rates[1] the one before it.
   double close   = rates[0].close;
   double open    = rates[0].open;
   double prevHi  = rates[1].high;
   double prevLo  = rates[1].low;

   double atrV    = atr[0];
   double adxV    = adx[0];
   double rsiV    = rsi[0];
   if(atrV <= 0.0) { g_status = "ATR unavailable"; return; }

   //--- volatility regime gate
   double medAtr = MedianAtr(InpRegimeLookback);
   if(medAtr <= 0.0) { g_status = "regime ATR unavailable"; return; }
   if(atrV < InpAtrMinMult * medAtr) { g_status = "volatility too low";  return; }
   if(atrV > InpAtrMaxMult * medAtr) { g_status = "volatility too high"; return; }

   //--- trend strength gate
   if(adxV < InpAdxMin) { g_status = StringFormat("ADX %.1f < %.1f", adxV, InpAdxMin); return; }

   //--- swing extremes over the lookback, ending at the last closed bar
   double swingLow = rates[0].low;
   double swingHigh= rates[0].high;
   for(int i = 0; i < InpSwingLookback && i < wantBars; i++)
     {
      swingLow  = MathMin(swingLow,  rates[i].low);
      swingHigh = MathMax(swingHigh, rates[i].high);
     }

   //--- did RSI visit pullback territory recently, and has it now recovered?
   double rsiMin = rsi[0], rsiMax = rsi[0];
   for(int i = 0; i < InpPullbackLookback && i < rsiCount; i++)
     {
      rsiMin = MathMin(rsiMin, rsi[i]);
      rsiMax = MathMax(rsiMax, rsi[i]);
     }

   bool longOk  = (emaF[0] > emaS[0]) && (close > emaT[0]) &&
                  (rsiMin <= InpRsiPullbackLong)  && (rsiV > InpRsiPullbackLong) &&
                  (close > prevHi) && (close > open);

   bool shortOk = (emaF[0] < emaS[0]) && (close < emaT[0]) &&
                  (rsiMax >= InpRsiPullbackShort) && (rsiV < InpRsiPullbackShort) &&
                  (close < prevLo) && (close < open);

   if(longOk == shortOk) { g_status = "no setup"; return; }   // neither, or both

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

   double lots = CalcLots(price, sl);
   if(lots <= 0.0)
     {
      g_status = "size below broker minimum - equity too small for this risk %";
      return;
     }

   sl = NormalizeDouble(sl, _Digits);
   tp = NormalizeDouble(tp, _Digits);

   bool ok = (type == ORDER_TYPE_BUY)
             ? trade.Buy(lots, _Symbol, 0.0, sl, tp, "XauTrend")
             : trade.Sell(lots, _Symbol, 0.0, sl, tp, "XauTrend");

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
   long   type   = posInfo.PositionType();
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

   if(type == POSITION_TYPE_BUY)
     {
      double gainedR = (close - entry) / risk;
      if(InpUseBreakeven && gainedR >= InpBreakevenAtR)
         newSl = MathMax(newSl, entry + InpBreakevenOffAtr * atrV);
      if(InpUseTrailing && gainedR >= InpTrailAtR)
         newSl = MathMax(newSl, close - InpTrailAtrMult * atrV);

      // Never place a stop at or through the live price.
      double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);
      newSl = MathMin(newSl, bid - MinStopOffset());
      if(newSl <= sl) return;                 // tighten only
     }
   else
     {
      double gainedR = (entry - close) / risk;
      if(InpUseBreakeven && gainedR >= InpBreakevenAtR)
         newSl = MathMin(newSl, entry - InpBreakevenOffAtr * atrV);
      if(InpUseTrailing && gainedR >= InpTrailAtR)
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
   double dayPnl  = equity - g_dayStartEquity;
   double dayPct  = (g_dayStartEquity > 0.0) ? dayPnl / g_dayStartEquity * 100.0 : 0.0;
   datetime now   = TimeCurrent();

   string txt = StringFormat(
      "XauTrend M5  |  %s\n"
      "-----------------------------------------\n"
      "server %s   (GMT%+d)   GMT hour %02d\n"
      "session %02d-%02d GMT      in session: %s\n"
      "spread %.2f  (max %.2f)\n"
      "-----------------------------------------\n"
      "equity      %.2f\n"
      "day P/L     %.2f  (%+.2f%%)  limit %.1f%%\n"
      "trades today %d / %d      loss streak %d / %d\n"
      "position    %s\n"
      "state       %s%s",
      _Symbol,
      TimeToString(now, TIME_DATE|TIME_MINUTES), g_gmtOffsetHrs, GmtHour(now),
      InpSessionStartHour, InpSessionEndHour, (InSession(now) ? "yes" : "no"),
      CurrentSpread(), InpMaxSpread,
      equity,
      dayPnl, dayPct, InpMaxDailyLossPct,
      g_tradesToday, InpMaxTradesPerDay, g_consecLosses, InpMaxConsecLosses,
      (HasOpenPosition() ? "open" : "flat"),
      g_status,
      (g_halted ? ("\nHALTED: " + g_haltReason) : ""));

   Comment(txt);
  }
//+------------------------------------------------------------------+
