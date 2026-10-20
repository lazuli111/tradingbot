"""Command-line entry point.

    python main.py backtest --since 2025-01-01 [--market perp] [--strategy donchian]
    python main.py walkforward --since 2025-01-01 [--train-days 180 --test-days 60]
    python main.py paper    [--market perp]
    python main.py testnet  [--market perp]      # needs testnet keys in .env
"""
from __future__ import annotations

import argparse
import logging
import sys

from bot.config import load_config


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", help="配置文件路径（默认 config.yaml，不存在则用 config.example.yaml）")
    common.add_argument("--exchange", help="ccxt 交易所 ID，如 binance / okx")
    common.add_argument("--market", choices=["spot", "perp"])
    common.add_argument("--symbol", help="如 BTC/USDT")
    common.add_argument("--timeframe", help="如 15m / 1h / 4h / 1d")
    common.add_argument("--strategy", dest="strategy_name", help="ma_cross / donchian（使用该策略默认参数）")
    common.add_argument("--leverage", type=float, help="合约杠杆")
    common.add_argument("-v", "--verbose", action="store_true")

    parser = argparse.ArgumentParser(description="加密货币趋势跟踪交易机器人")
    sub = parser.add_subparsers(dest="command", required=True)

    bt = sub.add_parser("backtest", parents=[common], help="历史回测")
    bt.add_argument("--since", required=True, help="开始日期，如 2025-01-01")
    bt.add_argument("--until", help="结束日期（默认到最新）")
    bt.add_argument("--out", default="reports", help="报告输出目录")
    bt.add_argument("--no-plot", action="store_true")

    wf = sub.add_parser("walkforward", parents=[common], help="滚动样本外检验（walk-forward）")
    wf.add_argument("--since", required=True, help="开始日期（第一个训练窗口从这里开始）")
    wf.add_argument("--until", help="结束日期（默认到最新）")
    wf.add_argument("--train-days", type=int, default=180, help="训练窗口天数")
    wf.add_argument("--test-days", type=int, default=60, help="测试窗口天数（也是滚动步长）")
    wf.add_argument("--objective", default="sharpe", choices=["sharpe", "return", "calmar"],
                    help="训练窗口里挑参数的标准")
    wf.add_argument("--min-trades", type=int, default=5, help="训练窗口内交易次数少于此值的参数不参与评选")
    wf.add_argument("--grid", type=parse_grid,
                    help='参数网格，如 "fast=10,20,30;slow=50,100,200"（默认用策略自带网格）')
    wf.add_argument("--out", default="reports", help="报告输出目录")
    wf.add_argument("--no-plot", action="store_true")

    for name, help_text in (("paper", "模拟盘：实时行情 + 本地模拟撮合"),
                            ("testnet", "连接交易所测试网 / 模拟交易账户下单")):
        p = sub.add_parser(name, parents=[common], help=help_text)
        p.add_argument("--max-bars", type=int, help="处理多少根新 K 线后退出（默认一直运行）")
    return parser


def parse_grid(text: str) -> dict[str, list]:
    def number(v: str):
        return int(v) if v.strip().lstrip("-").isdigit() else float(v)

    grid = {}
    for part in filter(None, (p.strip() for p in text.split(";"))):
        key, _, values = part.partition("=")
        if not values:
            raise argparse.ArgumentTypeError(f"网格格式应为 name=v1,v2;name2=...，收到 {part!r}")
        grid[key.strip()] = [number(v) for v in values.split(",")]
    return grid


def _load_market_data(cfg, args):
    from bot.data import load_funding, load_ohlcv, make_exchange

    ex = make_exchange(cfg.exchange, cfg.market)
    df = load_ohlcv(cfg, args.since, args.until, ex)
    funding = load_funding(cfg, args.since, args.until, ex) if cfg.is_perp else None
    return df, funding


def _report_name(cfg) -> str:
    return f"{cfg.exchange}_{cfg.market}_{cfg.symbol.replace('/', '')}_{cfg.timeframe}_{cfg.strategy.name}"


def cmd_walkforward(cfg, args) -> None:
    from bot.engine.optimize import walk_forward
    from bot.report import format_walkforward, save_walkforward

    df, funding = _load_market_data(cfg, args)
    wf = walk_forward(cfg, df, args.grid, funding, args.train_days, args.test_days,
                      args.objective, args.min_trades)
    lev = f" {cfg.risk.leverage:g}x" if cfg.is_perp else ""
    title = (f"Walk-forward | {cfg.exchange} {cfg.market_symbol} {cfg.timeframe}{lev} | {cfg.strategy.name} | "
             f"训练 {args.train_days} 天 / 测试 {args.test_days} 天 | 按 {args.objective} 选参")
    print(format_walkforward(wf, title))
    for path in save_walkforward(wf, args.out, _report_name(cfg), plot=not args.no_plot):
        print(f"已保存 {path}")


def cmd_backtest(cfg, args) -> None:
    from bot.engine.backtest import run_backtest
    from bot.report import format_metrics, save_report
    from bot.strategies import create_strategy

    df, funding = _load_market_data(cfg, args)
    result = run_backtest(cfg, df, funding)

    lev = f" {cfg.risk.leverage:g}x" if cfg.is_perp else ""
    strategy = create_strategy(cfg.strategy.name, cfg.strategy.params)
    title = f"{cfg.exchange} {cfg.market_symbol} {cfg.timeframe}{lev} | {strategy} | {result.equity.index.size} 根 K 线"
    print(format_metrics(result.metrics, title))
    for path in save_report(result, args.out, _report_name(cfg), plot=not args.no_plot):
        print(f"已保存 {path}")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    overrides = {k: getattr(args, k) for k in
                 ("exchange", "market", "symbol", "timeframe", "strategy_name", "leverage")}
    try:
        cfg = load_config(args.config, overrides)
    except ValueError as e:
        print(f"配置错误: {e}", file=sys.stderr)
        return 2

    if args.command == "backtest":
        cmd_backtest(cfg, args)
    elif args.command == "walkforward":
        try:
            cmd_walkforward(cfg, args)
        except ValueError as e:
            print(f"错误: {e}", file=sys.stderr)
            return 2
    else:
        from bot.engine.live import LiveRunner

        LiveRunner(cfg, args.command).run(max_bars=args.max_bars)
    return 0


if __name__ == "__main__":
    sys.exit(main())
