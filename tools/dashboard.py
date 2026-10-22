"""只读交易看板：从账本、日志和 systemd 读出状态，用一个只监听 127.0.0.1 的小网页展示。

只用标准库（不 import ccxt / pandas / bot.*），常驻只占二三十 MB；
不读 .env、不需要 API Key：浮动盈亏用 Binance Demo 的公开标记价接口算。
SQLite 以 `mode=ro` 打开，不会写入或锁住机器人的账本。

    python3 tools/dashboard.py                 # http://127.0.0.1:8765 ，通过 ssh -L 隧道访问
    python3 tools/dashboard.py --once          # 把快照 JSON 打到标准输出（核对用）
"""
from __future__ import annotations

import argparse
import gzip
import ipaddress
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone, tzinfo
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

HOUR_MS = 3_600_000
MARK_URL = "https://demo-fapi.binance.com/fapi/v1/premiumIndex?symbol={}"
HTML_PATH = Path(__file__).with_name("dashboard.html")
CGROUP_ROOT = Path("/sys/fs/cgroup/system.slice")

# 对账 / 下单异常相关的 WARNING（其余 WARNING 如 set_margin_mode 失败是每次启动都有的噪音）
RECONCILE_WORDS = ("不一致", "状态未知", "核对", "补设止损", "未成交", "盈亏未知", "重新处理", "熔断", "被拒")

GUARD_RE = re.compile(
    r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\s+权益 (-?[\d.]+)\s+峰值 (-?[\d.]+)\s+回撤 ([+-]?[\d.]+)%"
    r"\s+持仓 (\d+)\s+免费 (-?[\d.]+)(\s+\[已停机\])?")
GUARD_EVENT_RE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\s+(.+)$")
LOG_RE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ (\w+) ([\w.]+): (.*)$")
BAR_RE = re.compile(r"^K 线收盘 (\d{4}-\d\d-\d\d \d\d:\d\d) close=(-?[\d.]+) 信号=([+-]?\d+) 权益=(-?[\d.]+)")


@dataclass
class Config:
    root: Path = Path("/opt/tradingbot")
    db: str = "data/portfolio.db"
    guard_state: str = "data/portfolio_guard.json"
    guard_log: Path = Path("/var/log/tradingbot-guard.log")
    # 品种 -> (systemd 服务, 日志)
    bots: dict = field(default_factory=lambda: {
        "BTC": ("tradingbot-btc.service", Path("/var/log/tradingbot-btc.log")),
        "SOL": ("tradingbot-sol.service", Path("/var/log/tradingbot-sol.log")),
    })
    guard_service: str = "tradingbot-guard.service"
    guard_timer: str = "tradingbot-guard.timer"
    bar_hours: float = 4.0
    bar_grace_min: float = 30.0   # K 线收盘后超过 1 根 + 这么多分钟还没处理下一根就报警
    guard_stale_min: float = 12.0  # guard 每 5 分钟一行，超过这么久没新行就报警
    max_dd: float = 0.20          # 与 tools/portfolio_guard.py 的 --max-dd 保持一致
    log_tail_bytes: int = 512 * 1024
    curve_points: int = 600
    log_tz: tzinfo | None = None  # 机器人日志用服务器本地时间；None = 系统时区

    def path(self, p: str) -> Path:
        return self.root / p


# ---- 读数据（全部只读） ----------------------------------------------------

def open_ro(db: Path) -> sqlite3.Connection:
    """只读打开。WAL 账本的 -wal/-shm 由机器人维护，这里不需要写权限；
    万一 -shm 不存在（机器人都停了且已 checkpoint），没有 -wal 时按 immutable 读主文件。"""
    uri = f"file:{quote(str(db))}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=2)
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchall()
        return conn
    except sqlite3.OperationalError:
        if Path(str(db) + "-wal").exists() or not db.exists():
            raise
        return sqlite3.connect(uri + "&immutable=1", uri=True, timeout=2)


def read_db(db: Path) -> dict:
    conn = open_ro(db)
    try:
        runs: dict[str, dict] = {}
        for run, ts, eq in conn.execute("SELECT run, ts, equity FROM equity ORDER BY run, ts"):
            runs.setdefault(run, {"equity": [], "state": None})["equity"].append((ts, eq))
        for run, data in conn.execute("SELECT run, data FROM state"):
            runs.setdefault(run, {"equity": [], "state": None})["state"] = json.loads(data)
        fills = [dict(zip(("id", "run", "ts", "side", "qty", "price", "fee", "pnl", "reason"), r))
                 for r in conn.execute(
                     "SELECT id, run, ts, side, qty, price, fee, pnl, reason FROM fills ORDER BY id")]
        return {"runs": runs, "fills": fills}
    finally:
        conn.close()


def tail_lines(path: Path, max_bytes: int) -> list[str]:
    """文件最后 max_bytes 字节里的完整行；文件不存在返回空。"""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            data = f.read()
    except OSError:
        return []
    if size > max_bytes:
        data = data.split(b"\n", 1)[-1]
    return data.decode("utf-8", errors="replace").splitlines()


def _ms(text: str, fmt: str, tz: tzinfo | None) -> int:
    naive = datetime.strptime(text, fmt)
    aware = naive.replace(tzinfo=tz) if tz else naive.astimezone()
    return int(aware.timestamp() * 1000)


def parse_guard_log(lines: list[str]) -> dict:
    """guard 日志（UTC）：每行一个账户权益点，其它行（清仓、检查失败）当事件。"""
    points, events = [], []
    for line in lines:
        m = GUARD_RE.match(line)
        if m:
            points.append({
                "ts": _ms(m[1], "%Y-%m-%d %H:%M:%S", timezone.utc), "equity": float(m[2]),
                "peak": float(m[3]), "dd": float(m[4]) / 100, "positions": int(m[5]),
                "free": float(m[6]), "halted": bool(m[7])})
            continue
        m = GUARD_EVENT_RE.match(line)
        if m:
            events.append({"ts": _ms(m[1], "%Y-%m-%d %H:%M:%S", timezone.utc), "text": m[2].strip()})
    return {"points": points, "events": events}


def parse_bot_log(lines: list[str], tz: tzinfo | None) -> dict:
    """机器人日志：最后一次 K 线处理、最后一次启动、ERROR（带异常末行）和对账类 WARNING。"""
    last_bar = last_start = None
    errors, warnings = [], []
    current_error = None
    for line in lines:
        m = LOG_RE.match(line)
        if not m:
            # traceback 续行：记下最后一行（通常是 "SomeError: msg"）作为错误详情
            if current_error is not None and line.strip() and not line.startswith((" ", "Traceback")):
                current_error["detail"] = line.strip()[:300]
            continue
        current_error = None
        ts = _ms(m[1], "%Y-%m-%d %H:%M:%S", tz)
        level, msg = m[2], m[4]
        if level in ("ERROR", "CRITICAL"):
            current_error = {"ts": ts, "level": level, "text": msg[:300], "detail": None}
            errors.append(current_error)
        elif level == "WARNING" and any(w in msg for w in RECONCILE_WORDS):
            warnings.append({"ts": ts, "level": level, "text": msg[:300]})
        elif msg.startswith("启动 "):
            last_start = ts
        else:
            b = BAR_RE.match(msg)
            if b:
                last_bar = {"processed_at": ts, "open_ts": _ms(b[1], "%Y-%m-%d %H:%M", timezone.utc),
                            "close": float(b[2]), "signal": int(b[3]), "equity": float(b[4])}
    return {"last_bar": last_bar, "last_start": last_start, "errors": errors, "warnings": warnings}


def systemctl_show(unit: str, props: list[str]) -> dict:
    r = subprocess.run(
        ["systemctl", "show", unit, "--timestamp=unix", "-p", ",".join(props)],
        capture_output=True, text=True, timeout=5)
    values = dict(line.split("=", 1) for line in r.stdout.splitlines() if "=" in line)
    if r.returncode != 0 or not values:
        raise RuntimeError(f"systemctl show 退出码 {r.returncode}: {(r.stderr or r.stdout).strip()[:200]}")
    return values


def cgroup_state(unit: str, root: Path = CGROUP_ROOT, proc: Path = Path("/proc")) -> dict:
    """不经 D-Bus 判断服务是否在跑：unit 的 cgroup 里有进程就是在跑，启动时间取其中最早的进程。
    systemctl 在看板的 systemd 沙箱里读不到状态时用它兜底（cgroup 和 /proc 对任何用户都可读）。"""
    d = root / unit
    try:
        events = dict(line.split() for line in (d / "cgroup.events").read_text().splitlines() if " " in line)
        pids = [int(p) for p in (d / "cgroup.procs").read_text().split()]
    except OSError:
        return {"ActiveState": "inactive", "source": "cgroup"}
    if events.get("populated") != "1" or not pids:
        return {"ActiveState": "inactive", "source": "cgroup"}
    state = {"ActiveState": "active", "SubState": "running", "source": "cgroup"}
    try:
        btime = next(int(line.split()[1]) for line in (proc / "stat").read_text().splitlines()
                     if line.startswith("btime "))
        ticks = os.sysconf("SC_CLK_TCK")
        starts = [int((proc / str(p) / "stat").read_text().rsplit(")", 1)[1].split()[19]) for p in pids]
        state["ActiveEnterTimestamp"] = f"@{btime + min(starts) // ticks}"
    except (OSError, StopIteration, ValueError, IndexError):
        pass
    return state


def _unix_ms(value: str | None) -> int | None:
    """systemctl --timestamp=unix 的 "@1792..."；systemd 255 对 timer 的 LastTriggerUSec 不认这个参数，
    仍给 "Thu 2026-10-22 14:10:10 CST"（服务器本地时间）。空串 / n/a 返回 None。"""
    if not value:
        return None
    if value.startswith("@"):
        return int(float(value[1:]) * 1000)
    m = re.match(r"^\w{3} (\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) (\S+)$", value)
    if m:
        return _ms(m[1], "%Y-%m-%d %H:%M:%S", timezone.utc if m[2] == "UTC" else None)
    return None


def fetch_mark(symbol: str) -> float:
    """Binance Demo 公开标记价（不需要 Key）。symbol 形如 BTC/USDT:USDT。"""
    market_id = symbol.split(":")[0].replace("/", "")
    req = urllib.request.Request(MARK_URL.format(market_id), headers={"User-Agent": "tradingbot-dashboard"})
    with urllib.request.urlopen(req, timeout=5) as r:  # noqa: S310 - 固定的 https 地址
        return float(json.loads(r.read(4096))["markPrice"])


# ---- 汇总 -----------------------------------------------------------------

def run_symbol(run: str) -> str:
    """'testnet:binance:perp:BTC/USDT:USDT:4h:ma_cross' -> 'BTC/USDT:USDT'"""
    parts = run.split(":")
    return ":".join(parts[3:5]) if len(parts) >= 7 else run


def downsample(points: list[list], max_points: int) -> list[list]:
    """按时间分桶，每桶保留最低点和最高点（按时间先后），回撤的谷底不会被抹掉。"""
    if len(points) <= max_points:
        return points
    buckets = max(1, max_points // 2)
    size = len(points) / buckets
    out = []
    for i in range(buckets):
        chunk = points[int(i * size):int((i + 1) * size)]
        if not chunk:
            continue
        lo = min(chunk, key=lambda p: p[1])
        hi = max(chunk, key=lambda p: p[1])
        out.extend(sorted({id(lo): lo, id(hi): hi}.values(), key=lambda p: p[0]))
    if out[-1] is not points[-1]:
        out.append(points[-1])
    return out


def build_snapshot(cfg: Config, now_ms: int | None = None, show=systemctl_show, mark=fetch_mark,
                   cgroup=cgroup_state) -> dict:
    now_ms = now_ms or int(time.time() * 1000)
    bar_ms = int(cfg.bar_hours * HOUR_MS)
    alerts: list[dict] = []

    def alert(level: str, text: str) -> None:
        alerts.append({"level": level, "text": text})

    # 账本
    try:
        db = read_db(cfg.path(cfg.db))
    except (sqlite3.Error, OSError) as e:
        db = {"runs": {}, "fills": []}
        alert("critical", f"读取账本失败: {e}")
    run_by_base = {run_symbol(r).split("/")[0]: r for r in db["runs"]}

    # 机器人
    bots = []
    systemd_errors: list[str] = []
    positions_open = 0
    for name, (unit, log_path) in cfg.bots.items():
        run = run_by_base.get(name)
        info = db["runs"].get(run, {"equity": [], "state": None})
        symbol = run_symbol(run) if run else f"{name}/USDT:USDT"
        trader = (info["state"] or {}).get("trader", {})
        broker = trader.get("broker", {})
        pos = broker.get("position")
        svc = _service(show, unit, ["ActiveState", "SubState", "ActiveEnterTimestamp", "NRestarts"])
        if "error" in svc:
            systemd_errors.append(svc["error"])
            svc = {**cgroup(unit), "error": svc["error"]}
        log = parse_bot_log(tail_lines(log_path, cfg.log_tail_bytes), cfg.log_tz)

        position = None
        if pos:
            positions_open += 1
            position = {"side": pos["side"], "qty": pos["qty"], "entry": pos["entry"],
                        "stop": pos.get("stop"), "liq_price": pos.get("liq_price"),
                        "opened_at": pos.get("opened_at"), "mark": None, "upnl": None, "upnl_pct": None}
            try:
                m = mark(symbol)
                position.update(mark=m, upnl=pos["side"] * pos["qty"] * (m - pos["entry"]),
                                upnl_pct=pos["side"] * (m / pos["entry"] - 1))
            except Exception as e:  # noqa: BLE001 - 标记价只是锦上添花
                alert("info", f"{name} 标记价获取失败，浮动盈亏暂缺: {type(e).__name__}")

        last_eq = info["equity"][-1] if info["equity"] else None
        last_close = last_eq[0] + bar_ms if last_eq else None
        bar_age_ms = now_ms - last_close if last_close else None
        overdue = bar_age_ms is not None and bar_age_ms > bar_ms + cfg.bar_grace_min * 60_000
        last_bar = log["last_bar"]
        if last_eq and last_bar and last_bar["open_ts"] != last_eq[0]:
            last_bar = None  # 日志被截断或对不上，不拿它当最近一根
        recent = now_ms - 24 * HOUR_MS
        bot = {
            "name": name, "run": run, "symbol": symbol, "service": svc, "position": position,
            "last_signal": trader.get("last_signal"),
            "unresolved": broker.get("unresolved"),
            "risk": trader.get("risk"),
            "last_bar": {"open_ts": last_eq[0], "close_ts": last_close, "equity": last_eq[1],
                         "close": last_bar and last_bar["close"],
                         "signal": last_bar["signal"] if last_bar else trader.get("last_signal"),
                         "processed_at": last_bar and last_bar["processed_at"]} if last_eq else None,
            "bar_age_ms": bar_age_ms, "bar_overdue": overdue,
            "equity_curve": [[t, e] for t, e in info["equity"]],
            "last_start": log["last_start"],
            "errors": log["errors"][-10:][::-1], "warnings": log["warnings"][-10:][::-1],
            "errors_24h": sum(e["ts"] >= recent for e in log["errors"]),
            "warnings_24h": sum(w["ts"] >= recent for w in log["warnings"]),
        }
        bots.append(bot)

        if svc.get("ActiveState") != "active":
            alert("critical", f"{name} 服务未运行（{svc.get('ActiveState') or '未知'}）")
        if run is None:
            alert("serious", f"{name} 在账本里没有记录")
        elif overdue:
            alert("serious", f"{name} 已 {bar_age_ms / HOUR_MS:.1f} 小时没有处理新 K 线"
                             f"（警戒线 {cfg.bar_hours + cfg.bar_grace_min / 60:.1f} 小时）")
        if broker.get("unresolved"):
            alert("serious", f"{name} 有状态未知的订单 {broker['unresolved'].get('id')}，等待核对")
        if (trader.get("risk") or {}).get("halted"):
            alert("serious", f"{name} 单币熔断：{trader['risk'].get('halt_reason')}")
        if bot["errors_24h"]:
            alert("warning", f"{name} 最近 24 小时有 {bot['errors_24h']} 条 ERROR")
        if bot["warnings_24h"]:
            alert("warning", f"{name} 最近 24 小时有 {bot['warnings_24h']} 条对账类警告")

    # 账户（guard）
    guard_log = parse_guard_log(tail_lines(cfg.guard_log, 64 * 1024 * 1024))
    pts = guard_log["points"]
    try:
        gstate = json.loads(cfg.path(cfg.guard_state).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        gstate = {}
    account = None
    if pts:
        last = pts[-1]
        peak = max(float(gstate.get("peak") or 0), last["peak"])
        dd = last["equity"] / peak - 1 if peak else 0.0
        line = peak * (1 - cfg.max_dd)
        account = {
            "equity": last["equity"], "free": last["free"], "peak": peak, "drawdown": dd,
            "max_dd": cfg.max_dd, "guard_line": line, "distance": last["equity"] - line,
            "distance_pct": dd + cfg.max_dd, "positions": last["positions"],
            "start_equity": pts[0]["equity"], "start_ts": pts[0]["ts"],
            "return": last["equity"] / pts[0]["equity"] - 1, "as_of": last["ts"],
            "halted": bool(gstate.get("halted")) or last["halted"], "halt_reason": gstate.get("reason"),
        }
        if account["halted"]:
            alert("critical", f"组合 guard 已停机：{account['halt_reason'] or '见 guard 日志'}")
        elif account["distance_pct"] < 0.05:
            alert("warning", f"回撤 {dd:.2%}，离 guard 熔断线 −{cfg.max_dd:.0%} 不到 5 个百分点")
        last_fill = db["fills"][-1]["ts"] if db["fills"] else 0
        if last["positions"] != positions_open and last["ts"] > last_fill:  # 刚成交时 guard 还没跑，先不报
            alert("warning", f"交易所持仓数 {last['positions']}（guard 记录）与账本持仓数 {positions_open} 不一致")
        if now_ms - last["ts"] > cfg.guard_stale_min * 60_000:
            alert("serious", f"guard 已 {(now_ms - last['ts']) / 60_000:.0f} 分钟没有新记录")
    else:
        alert("serious", "guard 日志里没有权益记录")

    # 读不到 systemd 时 guard 的死活只看日志新不新（上面“没有新记录”的告警）
    gsvc = _service(show, cfg.guard_service, ["Result", "ExecMainStatus", "ExecMainExitTimestamp"])
    gtimer = _service(show, cfg.guard_timer, ["ActiveState", "LastTriggerUSec"])
    systemd_errors += [x["error"] for x in (gsvc, gtimer) if "error" in x]
    guard = {
        "result": gsvc.get("Result"), "exit_status": gsvc.get("ExecMainStatus"),
        "last_exit": _unix_ms(gsvc.get("ExecMainExitTimestamp")),
        "timer_active": gtimer.get("ActiveState"), "last_trigger": _unix_ms(gtimer.get("LastTriggerUSec")),
        "last_line": pts[-1]["ts"] if pts else None,
        "events": guard_log["events"][-10:][::-1],
        "systemd_error": gtimer.get("error") or gsvc.get("error"),
    }
    if "error" not in gtimer and gtimer.get("ActiveState") != "active":
        alert("critical", f"guard 定时器未运行（{gtimer.get('ActiveState') or '未知'}）")
    if "error" not in gsvc and gsvc.get("Result") != "success":
        alert("serious", f"guard 上次运行失败：{gsvc.get('Result')}，退出码 {gsvc.get('ExecMainStatus')}")

    names = {r: run_symbol(r).split("/")[0] for r in db["runs"]}
    fills = [{**{k: f[k] for k in ("ts", "side", "qty", "price", "fee", "pnl", "reason")},
              "symbol": names.get(f["run"], f["run"])} for f in sorted(db["fills"], key=lambda f: (f["ts"], f["id"]))[-500:][::-1]]

    if systemd_errors:
        alert("info", f"读不到 systemd 状态，服务是否在跑改看 cgroup，guard 看日志：{systemd_errors[0]}")
        _log_once(systemd_errors[0])

    order = {"critical": 0, "serious": 1, "warning": 2, "info": 3}
    alerts.sort(key=lambda a: order[a["level"]])
    return {
        "generated_at": now_ms, "bar_hours": cfg.bar_hours,
        "thresholds": {"bar_grace_min": cfg.bar_grace_min, "guard_stale_min": cfg.guard_stale_min},
        "account": account,
        "curve": downsample([[p["ts"], p["equity"]] for p in pts], cfg.curve_points),
        "bots": bots, "fills": fills, "guard": guard, "alerts": alerts,
    }


def _service(show, unit: str, props: list[str]) -> dict:
    try:
        return show(unit, props)
    except Exception as e:  # noqa: BLE001 - 读不到就降级，不能让整个快照失败
        return {"error": f"{type(e).__name__}: {e}"[:300]}


_last_logged: list[str] = [""]


def _log_once(message: str) -> None:
    """同一条错误只往 journal 打一次，免得每 30 秒刷屏。"""
    if message != _last_logged[0]:
        _last_logged[0] = message
        print(f"systemd 状态读取失败: {message}", file=sys.stderr, flush=True)


# ---- HTTP（只读：只有 GET） ------------------------------------------------

class Snapshots:
    """快照缓存：多个页面 / 刷新共用一次采集，避免频繁读文件、打交易所接口。"""

    def __init__(self, cfg: Config, ttl: float = 30.0, build=build_snapshot):
        self.cfg, self.ttl, self.build = cfg, ttl, build
        self.lock = threading.Lock()
        self.cached: tuple[float, bytes] | None = None

    def get(self) -> bytes:
        with self.lock:
            if self.cached is None or time.monotonic() - self.cached[0] > self.ttl:
                body = json.dumps(self.build(self.cfg), ensure_ascii=False, separators=(",", ":"))
                self.cached = (time.monotonic(), body.encode("utf-8"))
            return self.cached[1]


SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                                "connect-src 'self'; img-src data:; base-uri 'none'; form-action 'none'; "
                                "frame-ancestors 'none'"),
}


def make_handler(snapshots: Snapshots, html: bytes):
    allowed_hosts = {"127.0.0.1", "localhost", "[::1]"}

    class Handler(BaseHTTPRequestHandler):
        server_version = "tradingbot-dashboard"
        sys_version = ""

        def do_GET(self) -> None:  # noqa: N802
            # 只认 localhost 的 Host 头：防止 DNS rebinding 让别的网站借浏览器读数据
            # 端口不限：隧道本地端口可以随便选（如 ssh -L 18080:127.0.0.1:8765）
            host = re.sub(r":\d+$", "", self.headers.get("Host") or "")
            if host not in allowed_hosts:
                return self._send(403, b"forbidden", "text/plain; charset=utf-8")
            path = self.path.split("?", 1)[0]
            if path == "/":
                return self._send(200, html, "text/html; charset=utf-8")
            if path == "/api/snapshot":
                try:
                    body = snapshots.get()
                except Exception as e:  # noqa: BLE001
                    self.log_error("snapshot failed: %r", e)
                    return self._send(500, b'{"error":"snapshot failed"}', "application/json")
                return self._send(200, body, "application/json; charset=utf-8")
            return self._send(404, b"not found", "text/plain; charset=utf-8")

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            if len(body) > 1024 and "gzip" in self.headers.get("Accept-Encoding", ""):
                body = gzip.compress(body, 6)
                encoded = True
            else:
                encoded = False
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            if encoded:
                self.send_header("Content-Encoding", "gzip")
            self.send_header("Vary", "Accept-Encoding")
            for k, v in SECURITY_HEADERS.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

    return Handler


def serve(cfg: Config, host: str, port: int, ttl: float) -> None:
    if not ipaddress.ip_address(host).is_loopback:
        raise SystemExit(f"只允许监听回环地址（127.0.0.1 / ::1），拒绝 {host}；远程访问请用 ssh -L 隧道")
    handler = make_handler(Snapshots(cfg, ttl), HTML_PATH.read_bytes())
    server = ThreadingHTTPServer((host, port), handler)
    print(f"看板已启动: http://{host}:{port}  （快照缓存 {ttl:.0f} 秒）", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


def main() -> int:
    ap = argparse.ArgumentParser(description="只读交易看板")
    ap.add_argument("--root", default="/opt/tradingbot", help="机器人目录（账本、guard 状态的相对路径基准）")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--ttl", type=float, default=30.0, help="快照缓存秒数")
    ap.add_argument("--max-dd", type=float, default=0.20, help="与 portfolio_guard 的 --max-dd 一致")
    ap.add_argument("--once", action="store_true", help="打印一次快照 JSON 后退出")
    args = ap.parse_args()

    cfg = Config(root=Path(args.root), max_dd=args.max_dd)
    if args.once:
        print(json.dumps(build_snapshot(cfg), ensure_ascii=False, indent=1))
        return 0
    serve(cfg, args.host, args.port, args.ttl)
    return 0


if __name__ == "__main__":
    sys.exit(main())
