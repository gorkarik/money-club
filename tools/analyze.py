#!/usr/bin/env python3
"""Analyze recorded minute data and suggest config.yaml thresholds.

Reads the CSV written by the built-in recorder (logs/minutes.csv by default)
and prints:

  * the premium distribution (midline candidates),
  * how often each candidate upper/lower band would have fired,
  * a ready-to-paste `thresholds:` snippet.

Анализ поминутных данных стаканов, которые бот записывает сам: распределение
премии, частота срабатывания порогов и готовые значения thresholds для
config.yaml. В меню MONEY CLUB — пункт «Анализ / калибровка».

Usage:
    python3 tools/analyze.py                    # logs/minutes.csv
    python3 tools/analyze.py --csv path.csv --hours 24 --min-samples 10
"""
from __future__ import annotations

import argparse
import csv
import math
import sys
import time

CANDIDATES = [1.0, 1.5, 2.0, 3.0, 4.0, 5.0, 6.0, 8.0, 10.0, 15.0, 20.0]


def pctl(sorted_vals: list, q: float) -> float:
    """Linear-interpolated percentile of a pre-sorted list, q in [0, 100]."""
    if not sorted_vals:
        return float("nan")
    k = (len(sorted_vals) - 1) * q / 100.0
    lo = math.floor(k)
    hi = math.ceil(k)
    if lo == hi:
        return sorted_vals[int(k)]
    return sorted_vals[lo] * (hi - k) + sorted_vals[hi] * (k - lo)


def load_rows(path: str, hours: float, min_samples: int) -> list:
    cutoff = time.time() - hours * 3600 if hours > 0 else 0.0
    rows = []
    with open(path, newline="") as fh:
        for r in csv.DictReader(fh):
            try:
                if float(r["minute_ts"]) < cutoff:
                    continue
                if int(r["samples"]) < min_samples:
                    continue
                rows.append({
                    "ts": float(r["minute_ts"]),
                    "prem": float(r["premium_close_bps"]),
                    "prem_mean": float(r["premium_mean_bps"]),
                    "sell_max": float(r["sell_edge_max_bps"]),
                    "buy_max": float(r["buy_edge_max_bps"]),
                })
            except (KeyError, ValueError):
                continue
    return rows


def main() -> None:
    p = argparse.ArgumentParser(description="suggest thresholds from recorded "
                                            "minute data")
    p.add_argument("--csv", default="logs/minutes.csv")
    p.add_argument("--hours", type=float, default=0.0,
                   help="only use the last N hours (0 = all data)")
    p.add_argument("--min-samples", type=int, default=10,
                   help="skip minutes with fewer fresh samples than this")
    p.add_argument("--fees-bps", type=float, default=0.0,
                   help="SUM of both venues' taker fees in bps (each crossing "
                        "pays both legs); recorded edges are pre-fee, so this "
                        "is subtracted before counting firings (default 0.0 — "
                        "pass ~1.0 with a tradexyz hedge)")
    args = p.parse_args()

    try:
        rows = load_rows(args.csv, args.hours, args.min_samples)
    except FileNotFoundError:
        print(f"Файл {args.csv} не найден — сначала запустите бота "
              f"(хотя бы в тестовой записи), чтобы собрать данные.",
              file=sys.stderr)
        sys.exit(1)
    if len(rows) < 30:
        print(f"Пригодных минут в {args.csv}: всего {len(rows)} — этого мало, "
              f"соберите хотя бы несколько часов данных, прежде чем "
              f"доверять цифрам.", file=sys.stderr)
        if not rows:
            sys.exit(1)

    span_h = (rows[-1]["ts"] - rows[0]["ts"]) / 3600.0 + 1 / 60.0
    prem = sorted(r["prem"] for r in rows)
    mean = sum(prem) / len(prem)
    var = sum((x - mean) ** 2 for x in prem) / len(prem)
    median = pctl(prem, 50)

    print(f"\n=== {args.csv}: {len(rows)} мин. за {span_h:.1f} ч ===\n")
    print("Премия Entropy относительно хеджа, закрытие минуты (bps):")
    print(f"  среднее {mean:+.2f}   разброс (std) {math.sqrt(var):.2f}   "
          f"медиана {median:+.2f}")
    print(f"  p5 {pctl(prem, 5):+.2f}   p25 {pctl(prem, 25):+.2f}   "
          f"p75 {pctl(prem, 75):+.2f}   p95 {pctl(prem, 95):+.2f}")

    midline = round(median, 1) or 0.0   # normalize -0.0
    # room beyond the midline that was actually executable each minute, net
    # of taker fees (config thresholds are net-of-fee: the engine adds fees
    # on top, and recorded edges are pre-fee)
    fees = args.fees_bps
    sell_room = sorted((r["sell_max"] - midline - fees for r in rows),
                       reverse=True)
    buy_room = sorted((r["buy_max"] + midline - fees for r in rows),
                      reverse=True)

    print(f"\nСколько минут срабатывал бы каждый порог (центр midline_bps = "
          f"{midline:+.1f} по медиане, комиссии {fees:.1f} bps):")
    print(f"  {'порог bps':>9} | {'ПРОДАЖА entropy':>17} | {'ПОКУПКА entropy':>17}")
    print(f"  {'':>9} | {'минут':>8} {'в сутки':>8} | "
          f"{'минут':>8} {'в сутки':>8}")
    per_day = 24.0 / span_h if span_h > 0 else 0.0
    for t in CANDIDATES:
        s_hits = sum(1 for x in sell_room if x >= t)
        b_hits = sum(1 for x in buy_room if x >= t)
        print(f"  {t:>9.1f} | {s_hits:>8} {s_hits * per_day:>8.1f} | "
              f"{b_hits:>8} {b_hits * per_day:>8.1f}")

    # default suggestion: the band that fired in ~10% of minutes (p90 of the
    # fee-adjusted executable room), floored at 1 bps — tune from the table
    sug_upper = max(round(pctl(sorted(sell_room), 90) * 2) / 2, 1.0)
    sug_lower = max(round(pctl(sorted(buy_room), 90) * 2) / 2, 1.0)
    print(f"""
Рекомендуемые пороги (срабатывают ~в 10% минут; комиссии {fees:.1f} bps
уже учтены; полный цикл вход+выход даёт >= upper+lower bps после комиссий):

thresholds:
  midline_bps: {midline}
  upper_bps: {sug_upper}
  lower_bps: {sug_lower}

Премия со временем дрейфует — повторяйте анализ регулярно
(удобнее всего за последние 48 ч или за неделю).
""")


if __name__ == "__main__":
    main()
