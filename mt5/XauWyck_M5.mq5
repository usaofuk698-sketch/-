//+------------------------------------------------------------------+
//|                                                  XauWyck_M5.mq5  |
//|     Volume profile + Wyckoff spring/upthrust for spot gold M5    |
//|                        XAUUSD - M5 - built for Exness MT5        |
//+------------------------------------------------------------------+
//| WHAT THIS IS
//| A volume profile is the distribution of activity across PRICE rather than
//| across time. Two levels come out of it:
//|
//|   POC   the price with the most activity. It acts as a magnet: price that
//|         leaves it tends to come back, and that is what this EA targets.
//|   VAH   the edges of the band holding 70% of activity. Where acceptance
//|   VAL   ends, and where failed probes happen.
//|
//| Wyckoff names the trade. A SPRING is a probe below the value area that
//| fails: price dips under VAL, finds no supply, and closes back inside. An
//| UPTHRUST is the mirror above VAH. Both are traps -- the break pulls in
//| breakout traders, the recovery strands them, and their covering fuels the
//| move back toward the POC.
//|
//| WHAT THIS IS NOT
//| It is not order flow. Order flow needs a central order book and trade-side
//| classification; spot gold is decentralised and has neither. Anything a
//| retail terminal shows as "order flow" here is the broker's own book, not
//| the market's.
//|
//| AND ONE HONEST LIMITATION
//| MetaTrader reports TICK volume for gold, not traded volume -- there is no
//| exchange to report real volume. Tick volume counts price CHANGES. It tracks
//| activity closely enough to shape a usable profile, which is why this works
//| at all, but it is a proxy and should be understood as one.
//|
//| The profile is rebuilt every InpProfileUpdateBars, not every bar, which is
//| both far cheaper and closer to how levels are actually used: one that
//| changes shape every five minutes is not a level anyone trades against.
//|
//| Closed bars only; stops only ever tighten; nothing broker-specific is
//| hardcoded.
//+------------------------------------------------------------------+
#property copyright "XauWyck"
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
input int    InpMaxTradesPerDay    = 12;     // Max trades per day. 0 = unlimited
input int    InpMaxConsecLosses    = 6;      // Cool off after N losses in a row. 0 = OFF
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
input long   InpMagicNumber        = 770622; // Identifies this EA's own trades.
                                             // MUST differ from XauTrend (770577) and
                                             // XauPulse (770588): each EA manages only
                                             // positions with its own magic, so a shared
                                             // number would have each bot closing the
                                             // others' trades.
input int    InpMaxSpreadPoints    = 500;    // Skip entries above this spread, IN POINTS (as MT5 shows it)
input int    InpSlippagePoints     = 30;     // Max deviation on market orders
input int    InpMaxBarsInTrade     = 36;     // Close a trade older than this (0 = off)

input group "=== Strategy: the volume profile ==="
input int    InpProfileLookback    = 96;     // Bars in the profile (8h on M5)
input int    InpProfileBins        = 40;     // Price buckets
input double InpValueAreaPct       = 0.70;   // Share of activity inside VAH/VAL
input int    InpProfileUpdateBars  = 12;     // Rebuild every N bars, not every bar

input group "=== Strategy: the failed probe ==="
input double InpProbeDepthAtr      = 0.10;   // How far past the edge counts as a probe
input double InpCloseBackMarginAtr = 0.05;   // How far back inside it must close
input double InpProbeVolumeMax     = 1.40;   // Probe volume vs average: no supply
input double InpClosePositionMin   = 0.55;   // Recovery bar must close strongly

input group "=== Strategy: regime ==="
input int    InpAtrPeriod          = 14;
input int    InpRegimeLookback     = 288;    // Bars for the median-ATR reference
input double InpAtrMinMult         = 0.55;   // Skip dead tape the spread would eat
input double InpAtrMaxMult         = 3.00;   // Skip post-news chaos
input int    InpAdxPeriod          = 14;
input double InpAdxMax             = 45.0;   // A CEILING: do not fade a real trend

input group "=== Strategy: stop and target ==="
input double InpStopAtrBuffer      = 0.30;   // Beyond the probe extreme
input double InpMinStopAtrMult     = 1.00;   // Floor on stop distance (xATR)
input double InpRewardRisk         = 2.20;
input bool   InpTargetPoc          = true;   // Cap the target at the POC when nearer
input double InpMinTargetSpreadRatio = 6.0;  // Target must be >= N x the live spread

input group "=== Strategy: in-trade management ==="
input bool   InpUseBreakeven       = false;
input double InpBreakevenAtR       = 1.5;    // Only if enabled
input double InpBreakevenOffAtr    = 0.05;   // Lock in this much ATR beyond entry

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

// --- symbol spec, resolved once in OnInit
double   g_maxSpreadPrice = 0.0;  // InpMaxSpreadPoints converted to price units
double   g_tickSize      = 0.0;
double   g_tickValue     = 0.0;
double   g_volMin        = 0.0;
double   g_volMax        = 0.0;
double   g_volStep       = 0.0;
int      g_stopsLevelPts = 0;
int      g_freezeLevelPts= 0;

double   g_poc = 0.0, g_vah = 0.0, g_val = 0.0;   // cached profile levels
int      g_barsSinceProfile = 999999;

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

   hAtr      = iATR(_Symbol, PERIOD_CURRENT, InpAtrPeriod);
   hAdx      = iADX(_Symbol, PERIOD_CURRENT, InpAdxPeriod);

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

   PrintFormat("XauWyck M5 started on %s | server GMT offset %+d h | "
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
   if(InpRewardRisk <= 0.0)          { Print("ERROR: RewardRisk must be > 0"); return(false); }
   if(InpProfileLookback < 20)       { Print("ERROR: ProfileLookback must be >= 20"); return(false); }
   if(InpProfileBins < 5)            { Print("ERROR: ProfileBins must be >= 5"); return(false); }
   if(InpValueAreaPct <= 0.0 || InpValueAreaPct >= 1.0)
     { Print("ERROR: ValueAreaPct must be in (0, 1)"); return(false); }
   if(InpProfileUpdateBars < 1)      { Print("ERROR: ProfileUpdateBars must be >= 1"); return(false); }
   if(InpClosePositionMin < 0.0 || InpClosePositionMin > 1.0)
     { Print("ERROR: ClosePositionMin must be in [0, 1]"); return(false); }
   if(InpAdxMax <= 0.0 || InpAdxMax > 100.0)
     { Print("ERROR: AdxMax must be in (0, 100]. It is a CEILING: above it the "
             "market is trending and a failed-probe trade must not be taken.");
       return(false); }
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

   // A limit of zero means OFF, not "halt at zero loss". Without this guard the
   // obvious way to disable the rule does the opposite of what it looks like:
   // the threshold becomes 0.00, and the first losing trade -- or even a
   // scratch -- trips it and stops the bot for the rest of the day.
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
//+------------------------------------------------------------------+
//| Build the volume profile over the bars BEFORE this one.          |
//|                                                                  |
//| Each bar spreads its volume evenly across the price range it     |
//| covered. That is the standard approximation when only OHLC is    |
//| available: the true within-bar distribution is unknowable, and   |
//| putting it all at the close would weight a price the market may  |
//| have passed through in a second.                                 |
//|                                                                  |
//| The window starts at shift 1. A level built partly from the bar  |
//| being judged is not a level the market could have traded against.|
//+------------------------------------------------------------------+
bool BuildProfile(double &poc, double &vah, double &val)
  {
   int lookback = InpProfileLookback;
   int bins     = InpProfileBins;
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
      double v = (double)r[k].tick_volume;
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

   // Grow outward from the POC, always taking the heavier neighbour, until the
   // value-area share is covered. This is the conventional construction.
   int loIdx = pocIdx, hiIdx = pocIdx;
   double covered = hist[pocIdx];
   double target  = total * InpValueAreaPct;
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

void TryEntry()
  {
   int need = MathMax(InpRegimeLookback, InpProfileLookback) + 10;
   if(Bars(_Symbol, PERIOD_CURRENT) < need)
     {
      g_status = StringFormat("warming up (%d/%d bars)", Bars(_Symbol, PERIOD_CURRENT), need);
      return;
     }

   double atr[], adx[];
   if(!ReadBuffer(hAtr, 0, 1, 2, atr) || !ReadBuffer(hAdx, 0, 1, 2, adx))
     {
      g_status = "indicator data not ready";
      return;
     }

   MqlRates rates[];
   ArraySetAsSeries(rates, true);
   if(CopyRates(_Symbol, PERIOD_CURRENT, 1, 3, rates) != 3)
     {
      g_status = "price data not ready";
      return;
     }

   double close = rates[0].close;
   double highV = rates[0].high;
   double lowV  = rates[0].low;
   double atrV  = atr[0];
   double adxV  = adx[0];
   if(atrV <= 0.0) { g_status = "ATR unavailable"; return; }

   double medAtr = MedianAtr(InpRegimeLookback);
   if(medAtr <= 0.0) { g_status = "regime ATR unavailable"; return; }
   if(atrV < InpAtrMinMult * medAtr) { g_status = "volatility too low";  return; }
   if(atrV > InpAtrMaxMult * medAtr) { g_status = "volatility too high"; return; }

   //--- a ceiling, not a floor: a real trend must not be faded
   if(adxV >= InpAdxMax)
     {
      g_status = StringFormat("trending, ADX %.1f >= %.1f", adxV, InpAdxMax);
      return;
     }

   //--- refresh the cached profile on schedule
   g_barsSinceProfile++;
   if(g_barsSinceProfile >= InpProfileUpdateBars || g_vah <= g_val)
     {
      if(BuildProfile(g_poc, g_vah, g_val))
         g_barsSinceProfile = 0;
      else
        {
         g_status = "profile unavailable";
         return;
        }
     }
   if(g_vah <= g_val) { g_status = "profile invalid"; return; }

   //--- average volume for the "no supply" test
   double volAvg = 0.0;
   int volBars = MathMin(InpProfileLookback, 200);
   MqlRates vr[];
   ArraySetAsSeries(vr, true);
   if(CopyRates(_Symbol, PERIOD_CURRENT, 1, volBars, vr) == volBars)
     {
      double sum = 0.0;
      for(int k = 0; k < volBars; k++) sum += (double)vr[k].tick_volume;
      volAvg = sum / volBars;
     }
   double volNow = (double)rates[0].tick_volume;
   bool volOk = (volAvg <= 0.0) || (volNow <= InpProbeVolumeMax * volAvg);

   double barRange = highV - lowV;
   double closePos = (barRange > 0.0) ? (close - lowV) / barRange : 0.5;

   double probe  = InpProbeDepthAtr * atrV;
   double margin = InpCloseBackMarginAtr * atrV;

   //--- SPRING: probed below the value area and was rejected back inside
   bool spring = (lowV  <= g_val - probe) && (close >= g_val + margin)
                 && (closePos >= InpClosePositionMin) && volOk;
   //--- UPTHRUST: the mirror above
   bool upthrust = (highV >= g_vah + probe) && (close <= g_vah - margin)
                 && ((1.0 - closePos) >= InpClosePositionMin) && volOk;

   if(spring == upthrust)
     {
      g_status = StringFormat("no setup (VAL %.2f POC %.2f VAH %.2f)", g_val, g_poc, g_vah);
      return;
     }

   double sl, tp, price;
   double minOff = MinStopOffset();

   if(spring)
     {
      price = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
      sl    = MathMin(lowV - InpStopAtrBuffer * atrV, close - InpMinStopAtrMult * atrV);
      if(sl >= price - minOff) sl = price - MathMax(minOff, InpMinStopDistance);
      tp    = price + InpRewardRisk * (price - sl);
      if(InpTargetPoc && g_poc > price) tp = MathMin(tp, g_poc);   // the magnet
      if(tp <= price) { g_status = "target below entry"; return; }
      OpenTrade(ORDER_TYPE_BUY, price, sl, tp, atrV);
     }
   else
     {
      price = SymbolInfoDouble(_Symbol, SYMBOL_BID);
      sl    = MathMax(highV + InpStopAtrBuffer * atrV, close + InpMinStopAtrMult * atrV);
      if(sl <= price + minOff) sl = price + MathMax(minOff, InpMinStopDistance);
      tp    = price - InpRewardRisk * (sl - price);
      if(InpTargetPoc && g_poc < price) tp = MathMax(tp, g_poc);
      if(tp >= price) { g_status = "target above entry"; return; }
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
             ? trade.Buy(lots, _Symbol, 0.0, sl, tp, "XauWyck")
             : trade.Sell(lots, _Symbol, 0.0, sl, tp, "XauWyck");

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
      "XauWyck M5  |  %s\n"
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
