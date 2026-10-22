import asyncio
import gzip
import json

import numpy as np
import pandas as pd
import pytest
from aiohttp import web

from bot.mm import feasibility as fz
from bot.mm.recorder import (HEADERS, HOUR_MS, BookThrottle, HourlyGzipWriter, Recorder, RecorderConfig,
                             SessionLog, parse_message, read_coverage, stream_url)

T0 = 1_761_048_000_000  # 2025-10-21 12:00:00 UTC，整点


def book_msg(sym, E, T, b, B, a, A):
    return {"stream": f"{sym.lower()}@bookTicker",
            "data": {"e": "bookTicker", "u": 1, "s": sym, "b": b, "B": B, "a": a, "A": A, "T": T, "E": E}}


def trade_msg(sym, E, T, a, p, q, m, f=10, l=12):
    return {"stream": f"{sym.lower()}@aggTrade",
            "data": {"e": "aggTrade", "E": E, "a": a, "s": sym, "p": p, "q": q, "f": f, "l": l, "T": T, "m": m}}


# ---------------------------------------------------------------- recorder

def test_parse_book_ticker_keeps_strings_and_both_timestamps():
    kind, sym, row, (bid, ask) = parse_message(book_msg("BTCUSDT", 1002, 1001, "100.10", "3.5", "100.20", "1"), 1010)
    assert (kind, sym, bid, ask) == ("bookTicker", "BTCUSDT", "100.10", "100.20")
    assert row == "1002,1001,1010,100.10,3.5,100.20,1"
    assert len(row.split(",")) == len(HEADERS[kind].split(","))


def test_parse_agg_trade_maps_buyer_maker_and_fill_count():
    msg = trade_msg("SOLUSDT", 2002, 2001, 77, "150.5", "2", True)
    msg["data"]["nq"] = "1.5"
    kind, sym, row, _ = parse_message(msg, 2010)
    assert kind == "aggTrade" and sym == "SOLUSDT"
    assert row == "2002,2001,2010,77,150.5,2,1.5,1,3"  # m=true -> buyer_maker=1（主动卖），l-f+1=3
    row = parse_message(trade_msg("SOLUSDT", 1, 1, 1, "1", "1", False), 5)[2]
    assert row.split(",")[6:8] == ["", "0"]  # 没有 nq 就留空
    assert len(row.split(",")) == len(HEADERS[kind].split(","))


def test_parse_unknown_event_is_ignored():
    assert parse_message({"stream": "x", "data": {"e": "markPriceUpdate"}}, 0) is None
    assert parse_message({"result": None, "id": 1}, 0) is None


def test_stream_urls_split_public_and_market():
    assert stream_url(["BTCUSDT", "SOLUSDT"], "public") == \
        "wss://fstream.binance.com/public/stream?streams=btcusdt@bookTicker/solusdt@bookTicker"
    assert stream_url(["BTCUSDT"], "market").endswith("/market/stream?streams=btcusdt@aggTrade")


def test_book_throttle_keeps_price_changes_and_samples_qty_only_changes():
    th = BookThrottle(1000)
    assert th.keep("X", "1", "2", 0)
    assert not th.keep("X", "1", "2", 500)    # 只有量变
    assert th.keep("X", "1.1", "2", 600)      # 价变：一定保留
    assert not th.keep("X", "1.1", "2", 1500)
    assert th.keep("X", "1.1", "2", 1600)     # 距上一行满 1 秒
    assert th.keep("Y", "1", "2", 1601)       # 各品种独立
    assert BookThrottle(0).keep("X", "1", "2", 0) and BookThrottle(0).keep("X", "1", "2", 0)


def test_writer_splits_by_hour_and_appends_gzip_members(tmp_path):
    w = HourlyGzipWriter(tmp_path)
    w.add("aggTrade", "BTCUSDT", T0 + 10, "1,1,1,1,100,1,1,0,1")
    w.add("aggTrade", "BTCUSDT", T0 + HOUR_MS - 1, "2,2,2,2,100,1,1,0,1")
    w.add("aggTrade", "BTCUSDT", T0 + HOUR_MS, "3,3,3,3,100,1,1,1,1")
    w.flush()
    w.add("aggTrade", "BTCUSDT", T0 + 20, "4,4,4,4,100,1,1,0,1")  # 第二次刷盘：同一个文件追加
    w.flush()
    files = sorted((tmp_path / "aggTrade" / "BTCUSDT").iterdir())
    assert [f.name for f in files] == ["20251021-12.csv.gz", "20251021-13.csv.gz"]
    first = fz.read_gz_csv(files[0])
    assert list(first.columns) == HEADERS["aggTrade"].split(",")  # 表头只写一次
    assert first["E"].tolist() == [1, 2, 4]
    assert fz.read_gz_csv(files[1])["buyer_maker"].tolist() == [1]
    assert w.rows_written == 4


def test_reader_tolerates_truncated_last_member(tmp_path):
    w = HourlyGzipWriter(tmp_path)
    for i in range(3):
        w.add("aggTrade", "X", T0, f"{i},{i},{i},{i},100,1,1,0,1")
        w.flush()
    path = w.path("aggTrade", "X", T0 // HOUR_MS)
    good = path.read_bytes()
    extra = gzip.compress(b"9,9,9,9,100,1,1,0,1\n" * 1000)
    path.write_bytes(good + extra[: len(extra) // 2])  # 模拟强杀：最后一个 member 写了一半
    df = fz.read_gz_csv(path)
    assert df["E"].tolist()[:3] == [0, 1, 2]
    assert set(df["E"].tolist()[3:]) <= {9} and df.notna().all().all()  # 半截 member 里只留完整的行


def test_read_coverage_handles_reconnects_and_hard_kills():
    lines = ["event,recv,conn,detail", "start,0,,", "connect,100,public,", "alive,500,public,",
             "alive,900,public,", "disconnect,950,public,ws CLOSE", "connect,2000,public,", "alive,3000,public,",
             # 进程被强杀：没有 disconnect，下一次 start 时截在最后一次 alive
             "start,9000,,", "connect,9100,public,", "alive,9500,public,", "disconnect,9600,public,stop",
             "stop,9700,,", "connect,10000,public,", "disconnect,9000,public,never got data"]
    assert read_coverage(lines) == [(100, 950), (2000, 3000), (9100, 9600)]


def test_read_coverage_intersects_connections():
    lines = ["event,recv,conn,detail", "start,0,,",
             "connect,100,public,", "connect,150,market,", "alive,1000,public,", "alive,1000,market,",
             "disconnect,400,market,ws CLOSE", "connect,600,market,", "alive,1200,market,",
             "alive,1300,public,", "stop,1400,,"]
    # public: 100~1300；market: 150~400、600~1200 -> 交集
    assert read_coverage(lines) == [(150, 1000), (600, 1200)]


def test_recorder_reconnects_and_stops_cleanly(tmp_path):
    """本地假 websocket：第一条 public 连接发几条消息后被服务器关掉，录制器应重连，停止时写完缓冲。"""
    connections = []

    async def handler(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        conn = request.match_info["conn"]
        connections.append((conn, request.query_string))
        n = sum(c == conn for c, _ in connections)
        if conn == "public":
            await ws.send_str(json.dumps(book_msg("BTCUSDT", T0 + n, T0 + n, "100.0", "1", "100.1", "1")))
            await ws.send_str("not json")  # 坏消息只记日志，不断线
        else:
            await ws.send_str(json.dumps(trade_msg("BTCUSDT", T0 + n, T0 + n, n, "100.1", "1", False)))
        if conn == "public" and n == 1:
            await ws.close()
        else:
            async for _ in ws:  # 读到客户端的 close 帧为止
                pass
        return ws

    async def scenario():
        app = web.Application()
        app.router.add_get("/{conn}/stream", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        rec = Recorder(RecorderConfig(symbols=["BTCUSDT"], out=tmp_path, flush_seconds=0.2,
                                      ws_root=f"http://127.0.0.1:{port}", fetch_meta=False,
                                      max_rss_mb=1e6))  # pytest 进程本身已经超过 150MB
        task = asyncio.create_task(rec.run())
        for _ in range(100):
            await asyncio.sleep(0.1)
            if len(connections) >= 3 and rec.counts["bookTicker"] >= 2:
                break
        await asyncio.sleep(0.5)  # 让 alive 落盘
        rec.stop.set()  # 和 SIGTERM 处理函数做的事一样
        await asyncio.wait_for(task, 10)
        await runner.cleanup()
        return rec

    rec = asyncio.run(scenario())
    assert sorted(connections) == [("market", "streams=btcusdt@aggTrade"),
                                   ("public", "streams=btcusdt@bookTicker"),
                                   ("public", "streams=btcusdt@bookTicker")]
    assert rec.bad_messages == 2 and rec.reconnects == 1
    assert not (tmp_path / "recorder.pid").exists()
    rows = [l.split(",") for l in (tmp_path / "sessions.csv").read_text().splitlines()[1:]]
    events = [(r[0], r[2]) for r in rows]
    assert events[0] == ("start", "") and events[-1] == ("stop", "")
    assert events.count(("connect", "public")) == 2 and events.count(("disconnect", "public")) == 2
    assert events.count(("connect", "market")) == 1 and ("alive", "market") in events
    book = fz.read_gz_csv(next((tmp_path / "bookTicker" / "BTCUSDT").iterdir()))
    assert book["E"].tolist() == [T0 + 1, T0 + 2]
    assert len(fz.read_gz_csv(next((tmp_path / "aggTrade" / "BTCUSDT").iterdir()))) == 1


# ---------------------------------------------------------------- 分析：纯计算

def make_book(rows):
    return pd.DataFrame(rows, columns=["T", "bid", "ask"]).assign(bid_qty=1.0, ask_qty=1.0)


def make_trades(rows):
    return pd.DataFrame(rows, columns=["T", "price", "qty", "buyer_maker"])


SEGS = np.array([[0, 1_000_000]], dtype=np.int64)


def test_markout_sign_from_passive_side():
    # 买一 100 / 卖一 101（中间价 100.5）；主动买吃到 101 后，中间价 2 秒后涨到 101.5
    book = make_book([(0, 100.0, 101.0), (12_000, 101.0, 102.0)])
    trades = make_trades([(10_000, 101.0, 1.0, 0)])
    m = fz.trade_metrics(book, trades, SEGS, (1, 5))
    r = m.iloc[0]
    assert r["side"] == 1 and r["touch"]
    assert r["half_spread_bp"] == pytest.approx(0.5 / 100.5 * 1e4)
    assert r["adverse_1s"] == pytest.approx(0.0)
    assert r["adverse_5s"] == pytest.approx(1.0 / 100.5 * 1e4)              # 涨了：挂卖单的人亏
    assert r["markout_5s"] == pytest.approx((101.0 - 101.5) / 100.5 * 1e4)  # 卖在 101，现在值 101.5
    assert r["markout_1s"] > 0 > r["markout_5s"]


def test_markout_sign_for_aggressive_sell_and_price_drop():
    book = make_book([(0, 100.0, 101.0), (11_000, 99.0, 100.0)])
    trades = make_trades([(10_000, 100.0, 1.0, 1)])  # 主动卖打到买一 100，随后跌
    r = fz.trade_metrics(book, trades, SEGS, (5,)).iloc[0]
    assert r["side"] == -1 and r["touch"] and r["cleared"] is not None
    assert r["adverse_5s"] == pytest.approx(1.0 / 100.5 * 1e4)              # 跌了：挂买单的人亏
    assert r["markout_5s"] == pytest.approx((100.0 - 99.5) / 100.5 * 1e4 * -1)


def test_pre_trade_quote_is_strictly_before_and_cleared_uses_same_ms_update():
    # 成交和它导致的报价更新同一毫秒：成交前报价必须用更早那一行
    book = make_book([(0, 100.0, 101.0), (10_000, 100.0, 102.0)])
    trades = make_trades([(10_000, 101.0, 1.0, 0), (10_000, 102.0, 1.0, 0)])
    m = fz.trade_metrics(book, trades, SEGS, (1,))
    assert m["touch"].tolist() == [True, False]   # 扫第二档的那笔不是 touch
    assert m["cleared"].tolist() == [True, False]  # 卖一 101 被吃光，变成 102
    assert m["spread_bp"].iloc[0] == pytest.approx(1.0 / 100.5 * 1e4)


def test_recorder_stops_itself_over_memory_limit(tmp_path):
    rec = Recorder(RecorderConfig(symbols=["X"], out=tmp_path, max_rss_mb=1))
    assert rec.check_limits().startswith("rss")
    rec = Recorder(RecorderConfig(symbols=["X"], out=tmp_path, max_rss_mb=1e6, min_free_gb=1e9))
    assert rec.check_limits().startswith("disk")


def test_round_trip_deducts_two_maker_fees():
    assert fz.round_trip_bp(1.5, 2.0) == pytest.approx(-1.0)  # 2×1.5 − 2×2
    assert fz.Params(maker_fee=0.0002).fee_bp == pytest.approx(2.0)


def test_gaps_exclude_trades_and_lookups_do_not_cross_segments():
    segs = np.array([[0, 120_000], [200_000, 400_000]], dtype=np.int64)
    book = make_book([(0, 100.0, 101.0), (150_000, 200.0, 201.0), (210_000, 100.0, 101.0)])
    trades = make_trades([
        (50_000, 101.0, 1.0, 0),   # 段内，t+60s 仍在段内 -> 保留
        (70_000, 101.0, 1.0, 0),   # t+60s 越过段尾 -> 剔除
        (150_000, 201.0, 1.0, 0),  # 落在空档里 -> 剔除
        (205_000, 101.0, 1.0, 0),  # 新段开头，段内还没有报价（不能借上一段的） -> 剔除
        (220_000, 101.0, 1.0, 0),  # 保留
    ])
    m = fz.trade_metrics(book, trades, segs, (1, 60))
    assert m["T"].tolist() == [50_000, 220_000]
    assert (m["adverse_60s"] == 0).all()  # 都没借到空档里那行 200/201 的报价


def test_book_durations_stop_at_segment_end():
    segs = np.array([[0, 100], [200, 300]], dtype=np.int64)
    bt = np.array([0, 40, 150, 200, 260])
    seg_b = fz.segment_of(bt, segs)
    assert seg_b.tolist() == [0, 0, -1, 1, 1]
    assert fz.book_durations(bt, seg_b, segs).tolist() == [40, 60, 0, 60, 40]


def test_spread_quantiles_are_time_weighted():
    acc = fz.SymbolAccumulator("X", tick=0.1, horizons=(1,))
    book = make_book([(0, 100.0, 100.1), (1, 100.0, 100.3)])
    acc.add_book(book, np.array([750.0, 250.0]))  # 1 tick 占 3/4 时间，3 tick 占 1/4
    s = acc.summary(fz.Params(horizons=(1,), verdict_horizon=1))
    assert s["spread_med_ticks"] == 1 and s["spread_p90_ticks"] == 3
    assert s["one_tick_time"] == pytest.approx(0.75)
    assert s["tick_bp"] == pytest.approx(0.1 / 100.05 * 1e4, rel=1e-3)


def test_cluster_se_and_verdict():
    # 两个小时：第一小时两笔都是 1，第二小时两笔都是 3
    mean, se = fz.cluster_mean_se(np.array([2.0, 6.0]), np.array([2.0, 2.0]))
    assert mean == 2.0 and se == pytest.approx(np.sqrt((2 ** 2 + 2 ** 2) * 2) / 4)
    assert np.isnan(fz.cluster_mean_se(np.array([5.0]), np.array([3.0]))[1])  # 只有一小时，算不了
    assert fz.verdict(-0.1, 0.01, -1) == "负"
    assert fz.verdict(0.5, 0.3, -1) == "不显著"
    assert fz.verdict(0.5, 0.1, -1) == "仅乐观口径为正"
    assert fz.verdict(0.5, 0.1, 0.2) == "正（悲观口径也为正）"


# ---------------------------------------------------------------- 分析：端到端（合成录制目录）

def write_dataset(root, segments, book_rows, trade_rows, symbol="TESTUSDT", tick="0.1"):
    """按 recorder 的格式写一份合成录制：book_rows/trade_rows 用交易所时间 T，recv = T + 5ms。"""
    w = HourlyGzipWriter(root)
    for T, bid, ask in book_rows:
        w.add("bookTicker", symbol, T + 5, f"{T},{T},{T + 5},{bid},2,{ask},3")
    for i, (T, price, buyer_maker) in enumerate(trade_rows):
        w.add("aggTrade", symbol, T + 5, f"{T},{T},{T + 5},{i},{price},1,1,{buyer_maker},1")
    w.flush()
    log = SessionLog(root / "sessions.csv")
    for s, e in segments:
        for conn in ("public", "market"):
            log.write("connect", s, conn)
            log.write("alive", e, conn)
            log.write("disconnect", e, conn)
    (root / "meta.json").write_text(json.dumps({"symbols": {symbol: {"tickSize": tick, "stepSize": "1"}}}))


def test_end_to_end_negative_when_every_fill_is_picked_off(tmp_path):
    """价差 1 tick（≈10bp），每笔主动买之后中间价都上移 1 tick：逆向选择吃掉全部价差，再扣手续费为负。"""
    book, trades = [], []
    t, price = T0, 100.0
    for i in range(400):  # 跨两个小时文件
        book.append((t, round(price, 1), round(price + 0.1, 1)))
        trades.append((t + 1000, round(price + 0.1, 1), 0))
        price += 0.1
        book.append((t + 1000, round(price, 1), round(price + 0.1, 1)))  # 卖一被吃光，同一毫秒的更新
        t += 20_000
    end = t + 120_000
    gap = (T0 + 3_000_000, T0 + 3_600_000)  # 中间断线 10 分钟
    write_dataset(tmp_path, [(T0 - 10_000, gap[0]), (gap[1], end)], book, trades)
    params = fz.Params(maker_fee=0.0002, horizons=(1, 5), verdict_horizon=5, chunk_hours=1)
    df = fz.analyze(tmp_path, None, params, log=lambda m: None)
    r = df.iloc[0]
    assert r["symbol"] == "TESTUSDT"
    assert r["spread_med_ticks"] == 1 and r["one_tick_time"] == pytest.approx(1.0)
    assert r["touch_share"] == pytest.approx(1.0) and r["cleared_per_h"] > 0
    in_gap = sum(gap[0] - 1000 <= tr[0] <= gap[1] + 1000 for tr in trades)
    assert in_gap > 0
    assert r["valid_trades"] < len(trades) - in_gap + 1          # 空档内和段尾附近的成交被剔除
    # 每笔：半价差 0.05、5 秒后中间价 +0.1 -> markout ≈ -0.05/price；来回 = 2×markout − 2×2bp
    assert r["touch_markout_5s"] < 0
    assert r["touch_rt_5s"] == pytest.approx(2 * r["touch_markout_5s"] - 4.0)
    assert r["touch_adverse_5s"] == pytest.approx(-r["touch_markout_5s"] * 2, rel=0.05)
    assert r["verdict"] == "负"
    assert r["breakeven_fee_bp"] < 0
    cov = fz.coverage_report(tmp_path)
    assert cov["segments"] == 2 and cov["gap_s_total"] == pytest.approx(600)
    # --until 截掉断线之后的那一段：只剩第一段里的成交
    first = fz.analyze(tmp_path, None, fz.Params(horizons=(1, 5), verdict_horizon=5, chunk_hours=1,
                                                 until_ms=gap[0]), log=lambda m: None).iloc[0]
    assert 0 < first["valid_trades"] < r["valid_trades"]
    assert fz.coverage_report(tmp_path, until_ms=gap[0])["segments"] == 1


def test_end_to_end_positive_when_price_never_moves(tmp_path):
    """价格不动、价差 5 tick：没有逆向选择，来回 = 价差 − 2×手续费 > 0。"""
    book = [(T0 + i * 60_000, 100.0, 100.5) for i in range(30)]
    trades = [(T0 + 5_000 + i * 10_000, 100.5 if i % 2 else 100.0, 0 if i % 2 else 1) for i in range(150)]
    write_dataset(tmp_path, [(T0 - 1_000, T0 + 1_800_000)], book, trades)
    params = fz.Params(maker_fee=0.0002, horizons=(1, 30), verdict_horizon=30)
    r = fz.analyze(tmp_path, ["TESTUSDT"], params, log=lambda m: None).iloc[0]
    spread_bp = 0.5 / 100.25 * 1e4
    assert r["fill_spread_bp"] == pytest.approx(spread_bp)
    assert r["touch_adverse_30s"] == pytest.approx(0.0)
    assert r["touch_rt_30s"] == pytest.approx(spread_bp - 4.0)
    assert r["bid_hits_per_h"] == pytest.approx(r["ask_lifts_per_h"], rel=0.05)
    assert r["verdict"].startswith("正") or r["verdict"] == "仅乐观口径为正"
