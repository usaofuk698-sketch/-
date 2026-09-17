"""Performance statistics.

Win rate is deliberately buried near the bottom of the report. It is the number
everyone quotes and it says almost nothing on its own: a system winning 90% of
trades at +$10 and losing 10% at -$100 has a *negative* expectancy. The headline
figures here are expectancy per trade in R, and the t-statistic that says
whether the sample is large enough for that expectancy to mean anything.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field

import numpy as np
import pandas as pd

TRADING_DAYS = 252


@dataclass
class Stats:
    # -- headline
    trades: int = 0
    expectancy_r: float = 0.0
    expectancy_usd: float = 0.0
    t_stat: float = 0.0
    p_value: float = 1.0
    trades_needed: float = float("nan")
    # -- returns
    net_profit: float = 0.0
    total_return_pct: float = 0.0
    cagr_pct: float = 0.0
    # -- risk
    max_drawdown_pct: float = 0.0
    max_drawdown_usd: float = 0.0
    max_drawdown_days: float = 0.0
    sharpe: float = 0.0
    sortino: float = 0.0
    calmar: float = 0.0
    # -- trade shape
    win_rate_pct: float = 0.0
    profit_factor: float = 0.0
    avg_win_usd: float = 0.0
    avg_loss_usd: float = 0.0
    payoff_ratio: float = 0.0
    largest_loss_usd: float = 0.0
    max_consecutive_losses: int = 0
    avg_bars_held: float = 0.0
    # -- costs
    gross_profit: float = 0.0
    total_commission: float = 0.0
    total_swap: float = 0.0
    cost_pct_of_gross: float = 0.0
    # -- diagnostics
    exit_reasons: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def _norm_sf(z: float) -> float:
    """Upper-tail probability of the standard normal (no SciPy dependency)."""
    return 0.5 * math.erfc(z / math.sqrt(2.0))


def max_drawdown(equity: pd.Series) -> tuple[float, float, float]:
    """Return ``(depth_usd, depth_pct, longest_underwater_days)``."""
    if equity.empty:
        return 0.0, 0.0, 0.0
    peak = equity.cummax()
    dd = equity - peak
    depth_usd = float(dd.min())
    with np.errstate(divide="ignore", invalid="ignore"):
        depth_pct = float((dd / peak.replace(0, np.nan)).min() * 100.0)

    underwater = equity < peak
    longest = 0.0
    if underwater.any() and isinstance(equity.index, pd.DatetimeIndex):
        start = None
        for ts, wet in underwater.items():
            if wet and start is None:
                start = ts
            elif not wet and start is not None:
                longest = max(longest, (ts - start).total_seconds() / 86400.0)
                start = None
        if start is not None:
            longest = max(longest, (equity.index[-1] - start).total_seconds() / 86400.0)
    return depth_usd, (depth_pct if np.isfinite(depth_pct) else 0.0), longest


def _daily_returns(equity: pd.Series) -> pd.Series:
    if not isinstance(equity.index, pd.DatetimeIndex) or equity.empty:
        return pd.Series(dtype=float)
    daily = equity.resample("1D").last().dropna()
    return daily.pct_change().dropna()


def compute(
    trades: pd.DataFrame,
    equity: pd.Series,
    initial_capital: float,
    risk_free_rate: float = 0.0,
) -> Stats:
    s = Stats()
    s.trades = int(len(trades))
    if equity is not None and len(equity):
        s.net_profit = float(equity.iloc[-1] - initial_capital)
        s.total_return_pct = float(s.net_profit / initial_capital * 100.0)
        s.max_drawdown_usd, s.max_drawdown_pct, s.max_drawdown_days = max_drawdown(equity)

        if isinstance(equity.index, pd.DatetimeIndex) and len(equity) > 1:
            years = (equity.index[-1] - equity.index[0]).total_seconds() / (365.25 * 86400)
            if years > 0 and equity.iloc[-1] > 0:
                s.cagr_pct = float(((equity.iloc[-1] / initial_capital) ** (1 / years) - 1) * 100)

        rets = _daily_returns(equity)
        if len(rets) > 2 and rets.std() > 0:
            excess = rets - risk_free_rate / TRADING_DAYS
            s.sharpe = float(excess.mean() / rets.std() * np.sqrt(TRADING_DAYS))
            downside = rets[rets < 0]
            if len(downside) > 1 and downside.std() > 0:
                s.sortino = float(excess.mean() / downside.std() * np.sqrt(TRADING_DAYS))
        if s.max_drawdown_pct < 0:
            s.calmar = float(s.cagr_pct / abs(s.max_drawdown_pct))

    if s.trades == 0:
        return s

    pnl = trades["net_pnl"].astype(float)
    r = trades["r_multiple"].astype(float)
    wins, losses = pnl[pnl > 0], pnl[pnl < 0]

    s.expectancy_usd = float(pnl.mean())
    s.expectancy_r = float(r.mean())
    s.win_rate_pct = float(len(wins) / s.trades * 100.0)
    s.avg_win_usd = float(wins.mean()) if len(wins) else 0.0
    s.avg_loss_usd = float(losses.mean()) if len(losses) else 0.0
    s.payoff_ratio = float(abs(s.avg_win_usd / s.avg_loss_usd)) if s.avg_loss_usd else 0.0
    s.largest_loss_usd = float(pnl.min())
    gross_win, gross_loss = float(wins.sum()), float(abs(losses.sum()))
    s.profit_factor = gross_win / gross_loss if gross_loss > 0 else float("inf")

    # Is the edge distinguishable from noise? One-sided t-test on mean R.
    if s.trades > 1 and float(r.std(ddof=1)) > 0:
        sd = float(r.std(ddof=1))
        s.t_stat = float(s.expectancy_r / sd * math.sqrt(s.trades))
        s.p_value = _norm_sf(s.t_stat) if s.t_stat > 0 else 1.0 - _norm_sf(-s.t_stat)
        if s.expectancy_r > 0:
            # Trades required for this effect size to clear t = 2.0.
            s.trades_needed = float((2.0 * sd / s.expectancy_r) ** 2)

    streak = best = 0
    for v in pnl:
        streak = streak + 1 if v < 0 else 0
        best = max(best, streak)
    s.max_consecutive_losses = int(best)
    s.avg_bars_held = float(trades["bars_held"].mean())

    s.total_commission = float(trades["commission"].sum())
    s.total_swap = float(trades["swap"].sum())
    s.gross_profit = float(trades["gross_pnl"].sum())
    costs = s.total_commission + s.total_swap
    s.cost_pct_of_gross = float(costs / abs(s.gross_profit) * 100.0) if s.gross_profit else 0.0

    s.exit_reasons = trades["exit_reason"].value_counts().to_dict()
    return s


def report(stats: Stats, title: str = "BACKTEST") -> str:
    """Human-readable summary, ordered by what actually matters."""
    verdict = _verdict(stats)
    # "trades needed" only means something for a positive edge; for a losing
    # system there is no sample size that rescues it.
    needed = (
        f"{stats.trades_needed:,.0f}" if np.isfinite(stats.trades_needed) else "n/a"
    )
    w = 64
    L = [
        "=" * w,
        f" {title}",
        "=" * w,
        "",
        " EDGE (does this work at all?)",
        f"   expectancy / trade   {stats.expectancy_r:>10.4f} R   ({stats.expectancy_usd:+,.2f} USD)",
        f"   t-statistic          {stats.t_stat:>10.2f}      (>2.0 = distinguishable from noise)",
        f"   p-value              {stats.p_value:>10.4f}",
        f"   trades taken         {stats.trades:>10,}",
        f"   trades needed        {needed:>10}   (for t=2 at this effect size)",
        "",
        " RETURN",
        f"   net profit           {stats.net_profit:>10,.2f} USD",
        f"   total return         {stats.total_return_pct:>10.2f} %",
        f"   CAGR                 {stats.cagr_pct:>10.2f} %",
        "",
        " RISK (the number that decides if you can hold on)",
        f"   max drawdown         {stats.max_drawdown_pct:>10.2f} %   ({stats.max_drawdown_usd:,.2f} USD)",
        f"   longest underwater   {stats.max_drawdown_days:>10.1f} days",
        f"   max consec. losses   {stats.max_consecutive_losses:>10d}",
        f"   largest single loss  {stats.largest_loss_usd:>10,.2f} USD",
        f"   Sharpe               {stats.sharpe:>10.2f}",
        f"   Sortino              {stats.sortino:>10.2f}",
        f"   Calmar               {stats.calmar:>10.2f}",
        "",
        " TRADE SHAPE (informational -- win rate alone proves nothing)",
        f"   win rate             {stats.win_rate_pct:>10.2f} %",
        f"   profit factor        {stats.profit_factor:>10.2f}",
        f"   payoff ratio         {stats.payoff_ratio:>10.2f}   (avg win / avg loss)",
        f"   avg win / avg loss   {stats.avg_win_usd:>10,.2f} / {stats.avg_loss_usd:,.2f}",
        f"   avg bars held        {stats.avg_bars_held:>10.1f}",
        "",
        " COSTS",
        f"   gross profit         {stats.gross_profit:>10,.2f} USD",
        f"   commission + swap    {stats.total_commission + stats.total_swap:>10,.2f} USD",
        f"   costs / gross        {stats.cost_pct_of_gross:>10.2f} %",
        "",
        " EXITS",
    ]
    for reason, count in sorted(stats.exit_reasons.items(), key=lambda kv: -kv[1]):
        share = count / stats.trades * 100 if stats.trades else 0
        L.append(f"   {reason:<20} {count:>10,}   ({share:.1f}%)")
    L += ["", " VERDICT", f"   {verdict}", "=" * w]
    return "\n".join(L)


def _verdict(s: Stats) -> str:
    if s.trades < 30:
        return f"NOT ENOUGH DATA - {s.trades} trades proves nothing either way."
    if s.expectancy_r <= 0:
        return "NEGATIVE EDGE - this loses money. Do not trade it."
    if s.t_stat < 2.0:
        return (
            f"NOT SIGNIFICANT - positive but indistinguishable from luck "
            f"(t={s.t_stat:.2f}, needs ~{s.trades_needed:,.0f} trades)."
        )
    if s.max_drawdown_pct < -35:
        return "SIGNIFICANT BUT UNTRADEABLE - the drawdown would stop you out first."
    return "Positive and statistically significant IN SAMPLE. Now prove it walk-forward."
