"""パラメータグリッドの総当たりスキャン（multiprocessing）。

  python -m scalplab.backtest.scan [--config config/scan.yaml]

config/scan.yaml の grid を総当たりし、各組み合わせの metrics を集計する。
reports/scan-<ts>/results.csv（全組み合わせ）と top.md（期待値順トップ20, min_signals 未満は除外）を書く。
"""
from __future__ import annotations

import argparse
import itertools
from datetime import datetime
from multiprocessing import Pool
from pathlib import Path

import pandas as pd
import yaml

from scalplab.backtest.engine import DEFAULT_HORIZONS, get_loader, run_backtest
from scalplab.backtest.metrics import summarize
from scalplab.backtest.report import render_scan_top


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _scan_worker(task):
    """1 パラメータ組を実行して {params..., metrics...} の行を返す（Pool から呼ばれる）。

    loader はワーカ内で get_loader() し直す（ピクル不要にするため）。
    """
    date, codes, params, horizon = task
    loader = get_loader()
    entries = run_backtest(date, codes, params=params, loader=loader,
                           horizons=DEFAULT_HORIZONS, slippage_ticks=params.get("slippage_ticks", 0))
    s = summarize(entries, horizon=horizon)
    row = dict(params)
    row.update(s)
    return row


def _expand_grid(grid: dict):
    """{key: [values]} のデカルト積を dict の列として展開する。"""
    keys = sorted(grid)
    value_lists = [grid[k] if isinstance(grid[k], list) else [grid[k]] for k in keys]
    for combo in itertools.product(*value_lists):
        yield dict(zip(keys, combo))


def main(argv=None):
    ap = argparse.ArgumentParser(description="RCI インパルス戦略のパラメータスキャン")
    ap.add_argument("--config", default=None, help="スキャン設定 yaml（既定 config/scan.yaml）")
    ap.add_argument("--processes", type=int, default=None, help="並列プロセス数")
    args = ap.parse_args(argv)

    cfg_path = Path(args.config) if args.config else _project_root() / "config" / "scan.yaml"
    with open(cfg_path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    # YAML は日付を datetime.date にパースするため str に正規化（契約は文字列 date）
    date = str(cfg["date"])
    horizon = int(cfg.get("horizon", 60))
    min_signals = int(cfg.get("min_signals", 0))
    grid = cfg.get("grid", {})

    loader = get_loader()
    codes = cfg.get("codes")
    if not codes:
        codes = loader.list_recorded(date) if hasattr(loader, "list_recorded") else []
    if not codes:
        raise SystemExit(f"対象銘柄なし（date={date}）。config の codes か録画データを確認。")

    param_dicts = list(_expand_grid(grid))
    param_keys = sorted(grid)
    tasks = [(date, codes, p, horizon) for p in param_dicts]
    print(f"=== scan {date} codes={codes} combos={len(tasks)} ===")

    if len(tasks) == 1 or (args.processes is not None and args.processes <= 1):
        rows = [_scan_worker(t) for t in tasks]
    else:
        with Pool(processes=args.processes) as pool:
            rows = pool.map(_scan_worker, tasks)

    results = pd.DataFrame(rows)
    if "expectancy_bp" in results.columns:
        results = results.sort_values("expectancy_bp", ascending=False, na_position="last")

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = _project_root() / "reports" / f"scan-{ts}"
    out_dir.mkdir(parents=True, exist_ok=True)
    results.to_csv(out_dir / "results.csv", index=False)
    top_md = render_scan_top(results, param_keys, top_n=20, min_signals=min_signals, horizon=horizon)
    (out_dir / "top.md").write_text(top_md, encoding="utf-8")

    n_pass = int((results["n_signals"] >= min_signals).sum()) if "n_signals" in results else 0
    print(f"combos={len(results)} pass(min_signals>={min_signals})={n_pass}")
    print(f"saved: {out_dir}/results.csv, top.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
