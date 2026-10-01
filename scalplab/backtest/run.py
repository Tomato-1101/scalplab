"""単一パラメータでのバックテスト実行 CLI。

  python -m scalplab.backtest.run --date 2026-07-16 [--codes JP.7203,...] [--params k=v ...]

コンソールにサマリー表を出し、reports/run-<ts>/ に entries.csv と summary.md を書く。
"""
from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

from scalplab.backtest.engine import (
    DEFAULT_HORIZONS,
    get_loader,
    run_backtest,
)
from scalplab.backtest.metrics import by_hour, summarize
from scalplab.backtest.report import render_run_summary, summary_lines
from scalplab.strategy.rci_impulse import merge_params


def _coerce(value: str):
    """--params の値を可能なら int/float に変換する（文字列はそのまま）。"""
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


def _parse_params(pairs) -> dict:
    out = {}
    for item in pairs or []:
        if "=" not in item:
            raise SystemExit(f"--params は k=v 形式: {item!r}")
        k, v = item.split("=", 1)
        out[k.strip()] = _coerce(v.strip())
    return out


def _reports_root() -> Path:
    return Path(__file__).resolve().parents[2] / "reports"


def main(argv=None):
    ap = argparse.ArgumentParser(description="RCI インパルス戦略のバックテスト")
    ap.add_argument("--date", required=True, help="対象営業日 YYYY-MM-DD（JST）")
    ap.add_argument("--codes", default=None, help="カンマ区切り moomoo コード（省略時は録画済み全銘柄）")
    ap.add_argument("--params", nargs="*", default=None, help="k=v の上書きパラメータ")
    ap.add_argument("--horizon", type=int, default=60, help="主評価ホライズン秒")
    args = ap.parse_args(argv)

    params = merge_params(_parse_params(args.params))
    loader = get_loader()

    if args.codes:
        codes = [c.strip() for c in args.codes.split(",") if c.strip()]
    else:
        codes = loader.list_recorded(args.date) if hasattr(loader, "list_recorded") else []
    if not codes:
        raise SystemExit(f"対象銘柄なし（date={args.date}）。--codes を指定するか録画データを確認。")

    entries = run_backtest(args.date, codes, params=params, loader=loader,
                           horizons=DEFAULT_HORIZONS, slippage_ticks=params.get("slippage_ticks", 0))
    summary = summarize(entries, horizon=args.horizon)
    byhour_df = by_hour(entries, horizon=args.horizon)

    # コンソール表
    print(f"=== backtest {args.date} codes={codes} ===")
    for line in summary_lines(summary, args.horizon):
        print(line)

    # 保存
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = _reports_root() / f"run-{ts}"
    out_dir.mkdir(parents=True, exist_ok=True)
    entries.to_csv(out_dir / "entries.csv", index=False)
    md = render_run_summary(entries, params, summary, byhour_df, args.date, codes, args.horizon)
    (out_dir / "summary.md").write_text(md, encoding="utf-8")
    print(f"saved: {out_dir}/entries.csv, summary.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
