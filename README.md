# 加密货币趋势跟踪交易机器人

同一套策略和风控代码，依次跑 **回测 → 模拟盘 → 交易所测试网**。支持现货和 USDT 永续合约。
第一版**不支持实盘**（`CcxtBroker` 只接受测试网）。

> 本项目只是软件工具，不构成投资建议；回测结果不代表未来收益。

## 安装

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp config.example.yaml config.yaml     # 按需修改
```

Windows（PowerShell）下解释器在 `.venv\Scripts\`：

```powershell
py -3.12 -m venv .venv; .\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item config.example.yaml config.yaml
```

## 使用

```bash
# 回测：结果打印到终端，并在 reports/ 生成权益曲线 PNG 与交易明细 CSV
.venv/bin/python main.py backtest --since 2025-01-01
.venv/bin/python main.py backtest --since 2025-01-01 --market perp --leverage 2 --strategy donchian --timeframe 4h

# 样本外检验：每 60 天用前 180 天的数据重新挑一次参数，只统计没参与挑选的那 60 天
.venv/bin/python main.py walkforward --since 2025-01-01 --market perp --timeframe 4h
.venv/bin/python main.py walkforward --since 2025-01-01 --grid "fast=10,20,30;slow=50,100,200" --objective calmar

# 模拟盘：实时行情 + 本地模拟撮合，状态与成交写入 data/trades.db，重启后自动恢复
.venv/bin/python main.py paper --market perp

# 测试网：先在 .env 填入测试网 / Demo Trading 的 API Key
.venv/bin/python main.py testnet --market perp
```

常用参数：`--exchange`（任意 ccxt 交易所 ID，默认 binance）、`--symbol`、`--timeframe`、`--strategy`、`--config`。

## 工作流程

```
已收盘 K 线 ──> Strategy.signals()  只决定方向：多 / 空 / 空仓
                     │
                     ▼
             RiskManager          仓位 = 权益×单笔风险 ÷ 止损距离(ATR×倍数)
                     │            日亏损限制、最大回撤熔断、杠杆上限
                     ▼
             Broker               SimBroker（回测/模拟盘）或 CcxtBroker（测试网）
```

- 信号在第 i 根 K 线收盘时计算，在第 i+1 根开盘价执行，避免未来函数。
- 止损固定按 ATR 设置；同一根 K 线里既碰到止损也碰到止盈时，按先止损处理（保守）。
- 现货只做多；永续合约可做空，回测里计入资金费率、逐仓强平。
- 被止损后，要等信号翻转一次才会重新入场。

## 怎么看 walk-forward 结果

输出里有三条线，都基于同一段样本外区间：

- **walk-forward**：每个窗口只用过去的数据挑参数，最接近“真实上线后”的表现。
- **固定参数**：配置文件里的参数一直不变。注意：如果这组参数是看过回测结果后才定下来的，它同样带有事后偏差。
- **事后最优**：知道整段行情后才挑出的最好参数，现实中拿不到，只用来衡量过拟合程度。

walk-forward 和事后最优差距越大，说明“回测里挑出来的好参数”越靠不住。

## 过滤器实验

`experiments/compare_filters.py` 比较入场过滤器的效果：BTC / ETH / SOL，4h 永续，2023-01 起，分前后两半，阈值事先固定（不在数据上调）：

```bash
.venv/bin/python experiments/compare_filters.py --exchange okx --since 2023-01-01
```

结论（OKX 数据，2026-10 运行）：

- **均线交叉 + ADX≥20 有效**：12 个“币种×策略×半段”格子里，均线交叉的 6 格全部变好；阈值 18~22 都稳定，不是只有一个点好。样本外同区间（2023-07 起）对比：

  | | 无过滤 夏普 / 回撤 | ADX≥20 夏普 / 回撤 |
  |---|---|---|
  | BTC | 0.74 / −16.9% | 0.99 / −12.7% |
  | ETH | 0.04 / −17.8% | 0.36 / −18.0% |
  | SOL | 0.92 / −16.5% | 1.05 / −15.2% |

  因此 `config.example.yaml` 默认开启 `adx_min: 20`。
- **唐奇安突破 + ADX 无效**：突破本身已经要求强势行情，叠加 ADX 是重复过滤。用 `--strategy donchian` 时参数会重置，ADX 默认关闭。
- **波动率过滤（高位 / 低位都试过）变差**：12 格里只有 2 格改善，没有默认开启。
- **不要把 adx_min 放进 walk-forward 网格**：三个币种的样本外结果都比不放更差——可选项越多越容易过拟合训练期。固定一个合理值比滚动优化更好；同样地，固定的 20/50 在 BTC、SOL 上也胜过滚动选参。
- 注意：以上结论只基于 3 个高度相关的币种、约 3.8 年数据，属于“一致的迹象”，不是证明。
- 2026-10 在 Binance 完整数据（含 2023 起完整资金费）上复核：ADX≥20 仍是 12 格里 9 格变好（均线交叉 6 格全好），ADX≥25 只有 5/12；波动率过滤依旧 2/12。同区间 Binance 永续 BTC 默认参数为 +104.45%、夏普 1.28、回撤 −13.37%，其中资金费净支出 519.62——OKX 只覆盖近 3 个月资金费，会把永续收益高估约 12pp。
- **但 ADX 的收益贡献依赖区间**：把窗口从 2023-01 拉到 2021-01（含 2022 熊市），ADX≥20 只稳定压低回撤，收益反而变差——BTC +51.6%→+36.7%、SOL +177.8%→+118.0%（ETH 则相反，−9.5%→+23.5%）。所以它是“降回撤”的开关，不是“提收益”的开关。三币种两个窗口的完整结果见 `HANDOVER.md` 的“三个币种扫描”。

## 目录

| 路径 | 作用 |
|---|---|
| `bot/data.py` | ccxt 分页拉取 K 线与资金费率，CSV 缓存在 `data/` |
| `bot/strategies/` | 策略：`ma_cross` 均线交叉、`donchian` 通道突破；`base.py` 里的 ADX / 波动率入场过滤所有策略通用 |
| `experiments/` | 研究脚本，如过滤器对比 |
| `bot/risk.py` | 仓位计算与熔断 |
| `bot/broker/` | `sim.py` 模拟撮合，`ccxt_broker.py` 测试网下单 |
| `bot/trader.py` | 回测和实时运行共用的决策核心 |
| `bot/engine/` | `backtest.py` 回测，`optimize.py` 网格搜索与 walk-forward，`live.py` 模拟盘 / 测试网轮询 |
| `bot/metrics.py`, `bot/report.py` | 绩效指标与报告 |
| `tools/` | 前向验证：`forward_bot.ps1` 看护进程、`forward_status.py` 当前状态、`forward_report.py` 阶段报告 |
| `tests/` | `pytest` 单元测试 |

## 添加新策略

继承 `bot/strategies/base.py` 的 `Strategy`，实现 `base_warmup` 和 `raw_signals(df)`（返回 -1/0/1 序列，第 i 个值只能用到第 i 行及以前的数据），然后在 `bot/strategies/__init__.py` 注册。入场过滤由基类自动套用，新策略不需要自己处理。`tests/test_strategies.py::test_no_lookahead` 可以检查有没有用到未来数据。

## 已知限制

- 交易所在部分地区不可用（例如 Binance 对受限地区返回 HTTP 451），请遵守所在地法规和交易所条款。
- 部分交易所只提供最近几个月的资金费率历史（如 OKX 约 3 个月），更早的区间回测不计资金费。
- 测试网的止损 / 止盈是“软止损”：每次轮询比对最新价后市价平仓，机器人停止运行时不生效。
- 强平价用简化公式估算（逐仓、忽略手续费），与交易所实际值会有少许差异。
- 现货手续费按币扣除，平仓数量又按交易所最小步长截断，平完会留下少量低于步长、卖不掉的“尘埃”。
- 最大回撤熔断是永久性的：回测中一旦触发，之后都保持空仓；单币种回测里这会把后面的收益、夏普、交易次数一起“截断”，读指标时要留意（`experiments/compare_filters.py` 为此把 `max_drawdown` 放宽到 0.99）。
- walk-forward 的每个测试窗口都从空仓开始，结束时平仓，熔断状态也按窗口重置，所以会比连续运行多一点手续费。
