"""Command line interface.

``validate`` is the one that matters. It runs the full gauntlet in the order
that can actually falsify a strategy, and stops at the first stage that fails:

    audit -> in-sample -> walk-forward -> Monte Carlo

Running only the in-sample backtest and liking the number is how people end up
trading a curve fit.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

from . import config as cfg_mod
from . import metrics
from .backtest.engine import BacktestEngine
from .data.loader import load_csv
from .data.synthetic import generate
from .risk import RiskManager
from .validation import lookahead, montecarlo, walkforward


def _load_data(cfg: cfg_mod.AppConfig, args) -> tuple[pd.DataFrame, bool]:
    """Return ``(bars, is_synthetic)``."""
    path = args.data or cfg.data_path
    if path:
        df, report = load_csv(path, tz=cfg.data_timezone)
        print(report.summary())
        print()
        return df, False

    print(
        "!! No data file given -- generating SYNTHETIC bars.\n"
        "!! Synthetic results test the CODE, not the strategy. Any profit here\n"
        "!! is an artefact of the generator. Point --data at real M5 history\n"
        "!! before drawing any conclusion.\n"
    )
    df = generate(start=args.synth_start, end=args.synth_end, seed=args.seed)
    return df, True


def _backtest(cfg: cfg_mod.AppConfig, df: pd.DataFrame, params: dict | None = None):
    strategy = cfg.build_strategy(params)
    risk = RiskManager(cfg.risk, cfg.contract, cfg.initial_capital)
    engine = BacktestEngine(
        strategy, risk, cfg.build_broker(), cfg.contract,
        cfg.initial_capital, max_bars_in_trade=cfg.max_bars_in_trade,
    )
    return engine.run(df)


# ------------------------------------------------------------------ commands
def cmd_backtest(cfg, args) -> int:
    df, synthetic = _load_data(cfg, args)
    res = _backtest(cfg, df)
    st = metrics.compute(res.trades, res.equity, res.initial_capital)
    label = "IN-SAMPLE BACKTEST" + (" (SYNTHETIC DATA)" if synthetic else "")
    print(metrics.report(st, label))
    if res.blocked:
        print("\n signals blocked by risk rules:")
        for reason, n in res.blocked.most_common(10):
            print(f"   {reason:<28} {n:>8,}")
    if args.out and not res.trades.empty:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        res.trades.to_csv(args.out, index=False)
        print(f"\n trades written to {args.out}")
    return 0


def cmd_audit(cfg, args) -> int:
    df, _ = _load_data(cfg, args)
    report = lookahead.audit(cfg.build_strategy(), df, n_checks=args.checks)
    print(report.summary())
    return 0 if report.passed else 1


def cmd_walkforward(cfg, args) -> int:
    df, synthetic = _load_data(cfg, args)
    wf_cfg = cfg.raw.get("walkforward", {}) or {}
    grid = wf_cfg.get("grid", {})
    if not grid:
        print("no walkforward.grid in config; nothing to optimise", file=sys.stderr)
        return 2

    combos = len(walkforward.expand_grid(grid))
    print(f" grid: {combos} combinations x folds -- this takes a while\n")
    result = walkforward.run(
        df,
        strategy_factory=cfg.build_strategy,
        param_grid=grid,
        train_bars=int(wf_cfg.get("train_bars", 30_000)),
        test_bars=int(wf_cfg.get("test_bars", 10_000)),
        risk_cfg=cfg.risk,
        contract=cfg.contract,
        initial_capital=cfg.initial_capital,
        max_bars_in_trade=cfg.max_bars_in_trade,
        anchored=args.anchored,
    )
    print()
    if result.folds:
        print(result.fold_table().to_string(index=False))
        print()
        print(" parameter stability across folds:")
        print(result.parameter_stability().to_string(index=False))
        print()
    print(result.summary())
    if synthetic:
        print("\n (synthetic data -- this validates the procedure, not the strategy)")
    return 0


def cmd_montecarlo(cfg, args) -> int:
    df, _ = _load_data(cfg, args)
    res = _backtest(cfg, df)
    if res.trades.empty:
        print("no trades to resample", file=sys.stderr)
        return 2
    mc = montecarlo.run(
        res.trades, cfg.initial_capital, simulations=args.sims,
        risk_per_trade_pct=cfg.risk.risk_per_trade_pct,
    )
    print(mc.summary())
    return 0


def cmd_validate(cfg, args) -> int:
    """The full gauntlet, in falsification order."""
    df, synthetic = _load_data(cfg, args)

    print("\n[1/4] causality audit")
    audit_report = lookahead.audit(cfg.build_strategy(), df, n_checks=args.checks)
    print(audit_report.summary())
    if not audit_report.passed:
        print("\nSTOPPED: the strategy reads the future. Nothing downstream is meaningful.")
        return 1

    print("\n[2/4] in-sample backtest")
    res = _backtest(cfg, df)
    st = metrics.compute(res.trades, res.equity, res.initial_capital)
    print(metrics.report(st, "IN-SAMPLE"))
    if st.trades < 30 or st.expectancy_r <= 0:
        print("\nSTOPPED: no in-sample edge to test forward.")
        return 1

    print("\n[3/4] walk-forward")
    rc = cmd_walkforward(cfg, args)

    print("\n[4/4] monte carlo (on the in-sample trades)")
    mc = montecarlo.run(
        res.trades, cfg.initial_capital, simulations=args.sims,
        risk_per_trade_pct=cfg.risk.risk_per_trade_pct,
    )
    print(mc.summary())
    if synthetic:
        print(
            "\n REMINDER: every number above came from synthetic bars. They show\n"
            " the pipeline works. They say nothing whatsoever about gold."
        )
    return rc


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="goldbot", description="XAUUSD M5 research and execution toolkit"
    )
    p.add_argument("--config", default="config/default.yaml")
    p.add_argument("--data", default=None, help="path to an M5 OHLCV CSV")
    p.add_argument("--synth-start", default="2022-01-03")
    p.add_argument("--synth-end", default="2025-01-01")
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--checks", type=int, default=30, help="bars probed by the audit")
    p.add_argument("--sims", type=int, default=5000, help="monte carlo paths")
    p.add_argument("--anchored", action="store_true", help="expanding training window")
    p.add_argument("--out", default=None, help="write trades to this CSV")

    sub = p.add_subparsers(dest="command", required=True)
    for name, fn, help_text in [
        ("backtest", cmd_backtest, "single in-sample run"),
        ("audit", cmd_audit, "look-ahead / repaint audit"),
        ("walkforward", cmd_walkforward, "rolling out-of-sample validation"),
        ("montecarlo", cmd_montecarlo, "resample the trade sequence"),
        ("validate", cmd_validate, "run the whole gauntlet (start here)"),
    ]:
        sp = sub.add_parser(name, help=help_text)
        sp.set_defaults(func=fn)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = cfg_mod.load(args.config)
    return args.func(cfg, args)


if __name__ == "__main__":
    raise SystemExit(main())
