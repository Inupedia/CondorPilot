"""Test a no-lookahead volatility-risk-premium entry hypothesis on real SPY data.

The Iron Condor strategy remains frozen. The only entry condition is whether current ATM
implied volatility exceeds trailing realized volatility by a predeclared margin.

ATM IV is the same nearest-45-DTE / nearest-to-spot proxy used by real_spy_iv_regime.py.
Trailing realized volatility uses the prior 20 close-to-close log returns and excludes the
current day's return. Candidate VRP thresholds are fixed before execution at 0, 2, and 5
volatility points.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
from datetime import date
from pathlib import Path

from scripts.real_spy_baseline import _prepare_database
from scripts.real_spy_iv_regime import (
    DATA_START,
    DISCOVERY_END,
    DISCOVERY_START,
    MAX_DISCOVERY_DRAWDOWN,
    MIN_DISCOVERY_PF,
    MIN_DISCOVERY_TRADES,
    MIN_VALIDATION_PF,
    MIN_VALIDATION_TRADES,
    VALIDATION_END,
    VALIDATION_START,
    RegimeCandidate,
    _daily_atm_iv,
    _discovery_pass,
    _run_candidate,
    _validation_pass,
)

RV_LOOKBACK = 20
VRP_THRESHOLDS = (0.00, 0.02, 0.05)


def _vrp_signals(con) -> dict[date, tuple[float, float]]:
    iv_by_day = dict(_daily_atm_iv(con))
    rows = con.execute(
        """
        SELECT quote_date, close
        FROM underlying
        WHERE quote_date BETWEEN ? AND ?
        ORDER BY quote_date
        """,
        [DATA_START, VALIDATION_END],
    ).fetchall()
    days = [row[0] for row in rows]
    closes = [float(row[1]) for row in rows]
    returns = [math.log(closes[index] / closes[index - 1]) for index in range(1, len(closes))]

    signals: dict[date, tuple[float, float]] = {}
    for index, day in enumerate(days):
        if index <= RV_LOOKBACK:
            continue
        current_iv = iv_by_day.get(day)
        if current_iv is None:
            continue
        prior_returns = returns[index - RV_LOOKBACK - 1 : index - 1]
        if len(prior_returns) != RV_LOOKBACK:
            continue
        realized_vol = statistics.pstdev(prior_returns) * math.sqrt(252)
        vrp = current_iv - realized_vol
        signals[day] = (current_iv, vrp)
    return signals


def _rename_result(result: dict[str, object], threshold: float) -> dict[str, object]:
    renamed = dict(result)
    renamed["minimum_vrp"] = threshold
    renamed.pop("min_iv_percentile", None)
    return renamed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    con = _prepare_database(args.data_dir, DATA_START, VALIDATION_END)
    signals = _vrp_signals(con)
    candidates = tuple(
        RegimeCandidate(f"vrp_{int(threshold * 100):02d}", threshold)
        for threshold in VRP_THRESHOLDS
    )

    discovery: list[dict[str, object]] = []
    for candidate in candidates:
        raw = _run_candidate(
            con,
            candidate,
            signals,
            start=DISCOVERY_START,
            end=DISCOVERY_END,
        )
        result = _rename_result(raw, candidate.min_percentile)
        passed, reasons = _discovery_pass(result)
        result["discovery_pass"] = passed
        result["discovery_fail_reasons"] = reasons
        discovery.append(result)
        pf = result["profit_factor"]
        pf_text = "inf" if pf is None else f"{float(pf):.2f}"
        print(
            f"VRP {candidate.name:>6} trades={result['trade_count']:>3} "
            f"ret={float(result['total_return']):>+7.2%} "
            f"dd={float(result['max_drawdown']):>6.2%} "
            f"win={float(result['win_rate']):>6.1%} pf={pf_text:>5} pass={passed}"
        )

    eligible = [item for item in discovery if item["discovery_pass"]]
    selected_name: str | None = None
    validation: dict[str, object] | None = None
    if eligible:
        selected = max(
            eligible,
            key=lambda item: (float(item["profit_factor"]), float(item["total_return"])),
        )
        selected_name = str(selected["candidate"])
        candidate = next(item for item in candidates if item.name == selected_name)
        raw_validation = _run_candidate(
            con,
            candidate,
            signals,
            start=VALIDATION_START,
            end=VALIDATION_END,
        )
        validation = _rename_result(raw_validation, candidate.min_percentile)
        passed, reasons = _validation_pass(validation)
        validation["validation_pass"] = passed
        validation["validation_fail_reasons"] = reasons

    output = {
        "method": {
            "strategy": "frozen baseline; only VRP entry filter changes",
            "atm_iv_proxy": (
                "median call/put IV at nearest-to-45-DTE expiration and nearest-to-spot strike"
            ),
            "realized_volatility": (
                "annualized population standard deviation of the prior 20 close-to-close log "
                "returns, excluding the current day's return"
            ),
            "vrp_definition": "current ATM IV minus prior-20-day annualized realized volatility",
            "no_lookahead": True,
            "candidate_thresholds": list(VRP_THRESHOLDS),
            "discovery_period": [DISCOVERY_START.isoformat(), DISCOVERY_END.isoformat()],
            "validation_period": [VALIDATION_START.isoformat(), VALIDATION_END.isoformat()],
            "discovery_gates": {
                "minimum_trades": MIN_DISCOVERY_TRADES,
                "minimum_profit_factor_exclusive": MIN_DISCOVERY_PF,
                "minimum_total_return_exclusive": 0.0,
                "maximum_drawdown_exclusive": MAX_DISCOVERY_DRAWDOWN,
            },
            "selection": "highest discovery profit factor among candidates passing all gates",
            "validation_gates": {
                "minimum_trades": MIN_VALIDATION_TRADES,
                "minimum_profit_factor_exclusive": MIN_VALIDATION_PF,
                "minimum_total_return_exclusive": 0.0,
            },
            "external_holdout_rule": (
                "QQQ is evaluated only if a selected SPY VRP candidate passes temporal validation"
            ),
        },
        "signal_observation_count": len(signals),
        "discovery": discovery,
        "selected_candidate": selected_name,
        "validation": validation,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, indent=2, sort_keys=True), encoding="utf-8")

    print(f"SELECTED_VRP={selected_name}")
    if validation is not None:
        pf = validation["profit_factor"]
        pf_text = "inf" if pf is None else f"{float(pf):.2f}"
        print(
            f"VRP_TEMPORAL_VALIDATION trades={validation['trade_count']} "
            f"ret={float(validation['total_return']):+.2%} "
            f"dd={float(validation['max_drawdown']):.2%} "
            f"win={float(validation['win_rate']):.1%} pf={pf_text} "
            f"pass={validation['validation_pass']}"
        )
    print(f"RESULT_PATH={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
