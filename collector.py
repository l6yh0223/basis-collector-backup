#!/usr/bin/env python3
"""2-second public-basis collector, runs on InstaBox box-1 and on GitHub Actions (basis-collector workflow).

Two stages, matching the cloud-computer division of labour
(user, 2026-11-15): the WATCH stage fetches raw legs and index
quotes; the COMPUTE stage derives basis from those legs. Both
stages live in this script on box-1: Kaggle sessions have no
internet and Colab sessions are temporary, so neither can serve
as the live calculator. Muse (the mainstay) judges opportunities
from this series, advises the live book, and runs the sim book.

Sources (verified reachable from box-1 on 2026-11-15, Sunday; live
tick behaviour still to be validated on a trading day):
- ETF spot + Shanghai Composite: Tencent qt.gtimg.cn, one batch
  request sh000001/sh510050/sh510300/sh510500/sh512100.
  Price is field [3], quote time is field [30] (YYYYMMDDHHMMSS,
  CST); index change/pct are fields [31]/[32]. Volume is field [6]
  (hands; verified 2026-11-15: equals field [36] and the volume in
  the field [35] composite, and [7]+[8]=[6]); turnover is field [37]
  (10k CNY; verified against the field [35] turnover).
- Futures: Sina hq.sinajs.cn, CFF_IH2612/CFF_IF2612/CFF_IC2612, with a
  Referer header (without it Sina returns 403). Last price is field [3],
  quote time is field [-2]. Volume is field [4] (lots; verified
  2026-11-15: field [5]/[4] equals the session average price for all
  three contracts). Open interest is field [14] (INVALIDATED 2026-11-16: constant
  intraday, not live open interest; never use it for OI judgments). Price mapping was deduced from the 2026-11-13
  close sample and MUST be cross-checked against the user's
  software-displayed basis on the first live session; the
  open-interest mapping must be validated separately (intraday
  behaviour + software cross-check) before it is used as anything
  but reference.

IM2612 has NO working public source from box-1: Sina returns
wrong-level data (~6300 instead of ~2400), Eastmoney/126/Tencent are
unreachable. IM is recorded as null, never computed from bad data.

FTSE China A50 Index (XIN0) has NO working public intraday source
from box-1 either (probed 2026-11-15: Tencent XIN0/ff_XIN0 none_match,
Sina XIN0/hf_XIN0/int_XIN0/gb_XIN0/XINA50 empty, Eastmoney push2 502
even for the sh000001 control, Yahoo ^XIN0 404). A50 is recorded as
null, never substituted with the SGX A50 futures contract; use the
user's A50 chart screenshot instead.

Basis = ETF price * 1000 - futures last price (the user's formula).
This PUBLIC-formula 2s series is the primary working basis for the
sim book and live advice; the user's software-displayed basis, sent
about every 30 minutes, is cross-check only and governs on any
material conflict (user decision, 2026-11-15).

A futures leg whose quote timestamp is stale (not dated today, or more
than 60s behind the poll) produces NO basis value: Sina intermittently
serves ~3-minute-old cached quotes that print false basis extremes while
the ETF leg sits flat (proven 2026-11-16).

Each poll also maintains extremes-YYYYMMDD.json: per-contract open (the
09:35 anchor read; opening first prints are untrusted), high/low with
timestamps, AM/PM session extremes, last value, range, previous-day close
comparison, the four anchor reads (09:35/13:05/11:20/14:50), and a
data-quality section (poll/error counts, max gap). It is updated
read-modify-write every poll so the AM/PM sessions and restarts share one
record.
"""
import argparse
import json
import math
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone

CST = timezone(timedelta(hours=8))
ETF_URL = "http://qt.gtimg.cn/q=sh000001,sh510050,sh510300,sh510500,sh512100"
FUT_URL = "http://hq.sinajs.cn/list=CFF_IH2612,CFF_IF2612,CFF_IC2612"
INDEX_RANGE = (1000.0, 10000.0)
CONTRACTS = {
    "IH": ("510050", "IH2612", 1000.0, 5000.0),
    "IF": ("510300", "IF2612", 2000.0, 7000.0),
    "IC": ("510500", "IC2612", 3000.0, 10000.0),
}
ETF_RANGE = (0.5, 20.0)
FETCH_TIMEOUT = 2.0

TENCENT_RE = re.compile(r'v_sh(\d+)="([^"]*)";')
SINA_RE = re.compile(r'var hq_str_CFF_(\w+)="([^"]*)";')


def fetch(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
        return resp.read().decode("gbk", "replace")


def _src_time_tencent(fields):
    if len(fields) > 30 and fields[30]:
        try:
            return datetime.strptime(fields[30], "%Y%m%d%H%M%S").replace(
                tzinfo=CST).isoformat()
        except ValueError:
            return None
    return None


def _num(fields, idx):
    try:
        value = float(fields[idx])
        return value if math.isfinite(value) else None
    except (ValueError, IndexError):
        return None


def _int(fields, idx):
    try:
        value = int(float(fields[idx]))
        return value if value >= 0 else None
    except (ValueError, IndexError):
        return None


def parse_tencent(text, errors):
    """One batch response: ETF legs plus the Shanghai Composite index.

    Returns (etf, indices). Index code 000001 is branched out before
    the ETF price-range check, which an index level would fail.
    """
    etf = {}
    indices = {}
    for code, body in TENCENT_RE.findall(text):
        fields = body.split("~")
        try:
            price = float(fields[3])
            if not math.isfinite(price):
                raise ValueError("price invalid: %r" % fields[3])
        except (ValueError, IndexError) as exc:
            errors.append("tencent %s parse: %s" % (code, exc))
            continue
        src_time = _src_time_tencent(fields)
        if code == "000001":
            if not (INDEX_RANGE[0] <= price <= INDEX_RANGE[1]):
                errors.append("index SH price out of range: %s" % price)
                continue
            indices["SH"] = {
                "price": price,
                "chg": _num(fields, 31),
                "pct": _num(fields, 32),
                "vol": _int(fields, 6),
                "turnover": _num(fields, 37),
                "src_time": src_time,
            }
            continue
        if not (ETF_RANGE[0] <= price <= ETF_RANGE[1]):
            errors.append("etf %s price out of range: %r" % (code, fields[3]))
            continue
        etf[code] = {"price": price, "src_time": src_time,
                     "vol": _int(fields, 6), "turnover": _num(fields, 37)}
    return etf, indices


def parse_fut(text, errors):
    out = {}
    for code, body in SINA_RE.findall(text):
        if not body:
            errors.append("fut %s empty" % code)
            continue
        fields = body.split(",")
        try:
            price = float(fields[3])
            if not math.isfinite(price) or price <= 0:
                raise ValueError("price invalid: %r" % fields[3])
            src_time = None
            if len(fields) >= 2 and fields[-2]:
                try:
                    src_time = datetime.strptime(
                        fields[-2], "%Y-%m-%d %H:%M:%S").replace(tzinfo=CST).isoformat()
                except ValueError:
                    src_time = None
            out[code] = {"price": price, "src_time": src_time,
                         "vol": _int(fields, 4), "oi": _int(fields, 14)}
        except (ValueError, IndexError) as exc:
            errors.append("fut %s parse: %s" % (code, exc))
    return out


def fetch_stage(errors):
    """WATCH stage: fetch raw legs and index quotes, no computation."""
    etf, indices, fut = {}, {}, {}
    try:
        etf, indices = parse_tencent(fetch(ETF_URL), errors)
    except Exception as exc:  # noqa: BLE001 - record and continue
        errors.append("tencent fetch: %s" % exc)
    try:
        fut = parse_fut(
            fetch(FUT_URL, {"Referer": "http://finance.sina.com.cn"}), errors)
    except Exception as exc:  # noqa: BLE001 - record and continue
        errors.append("fut fetch: %s" % exc)
    return {"etf": etf, "fut": fut, "indices": indices}


MAX_FUT_LAG_S = 60.0


def _leg_fresh(src_time, now_cst):
    """Sina intermittently serves a ~3-minute-old cached futures quote
    interleaved with fresh quotes (proven 2026-11-16: fresh lag p50 9s,
    stale lag 60-315s). A stale leg prints false basis extremes while the
    ETF leg sits flat, so it must not produce a basis value. Unparseable
    timestamps keep the old behaviour (compute) so a format quirk cannot
    blank the whole feed."""
    if not src_time:
        return True
    if str(src_time)[:10] != now_cst.strftime("%Y-%m-%d"):
        return False
    try:
        lag = (now_cst - datetime.fromisoformat(src_time)).total_seconds()
    except ValueError:
        return True
    return 0 <= lag <= MAX_FUT_LAG_S


def compute_stage(legs, errors, now_cst):
    """COMPUTE stage: derive basis from the fetched legs only."""
    etf, fut = legs["etf"], legs["fut"]
    basis = {}
    for name, (etf_code, fut_code, lo, hi) in CONTRACTS.items():
        e = etf.get(etf_code)
        f = fut.get(fut_code)
        if e is None or f is None:
            basis[name] = None
            continue
        if not (lo <= f["price"] <= hi):
            errors.append("fut %s price out of range: %s" % (fut_code, f["price"]))
            basis[name] = None
            continue
        if not _leg_fresh(f["src_time"], now_cst):
            errors.append("fut %s stale leg: src_time %s" % (fut_code, f["src_time"]))
            basis[name] = None
            continue
        basis[name] = round(e["price"] * 1000.0 - f["price"], 2)
    return basis


def poll():
    errors = []
    now_epoch = time.time()
    now_cst = datetime.fromtimestamp(now_epoch, CST)
    legs = fetch_stage(errors)
    basis = compute_stage(legs, errors, now_cst)
    return {
        "ts_epoch": round(now_epoch, 3),
        "ts_cst": now_cst.isoformat(timespec="seconds"),
        "etf": legs["etf"],
        "fut": legs["fut"],
        "indices": legs["indices"],
        "basis": basis,
        "im": None,
        "im_note": "no working public source from box-1; use software basis only",
        "a50": None,
        "a50_note": "no working public intraday source from box-1; use user's A50 chart screenshot, never the SGX futures contract",
        "errors": errors,
    }


def _session_of(ts_cst):
    t = ts_cst[11:19]
    if t < "11:35:00":
        return "am"
    if t >= "13:00:00":
        return "pm"
    return None


def _prev_close(outdir, date_yyyymmdd):
    """Most recent earlier extremes file's last values, up to 5 days back."""
    # 换月断点（仓库链）：仓库 data/ 里 20261119 的 extremes 文件是 11 月合约口径，
    # 其 last（IH 117.0/IF 89.0/IC 42.2）不能当 12 月的 prev_close。
    # 11-19 当晚再跑，用 11-18 的 12 月可交易收盘（用户收盘包：IH 151.3/IF 126.8/IC 123.0）；
    # 11-20 用 11-19 的 12 月可交易收盘（用户收盘包：IH 153.3/IF 136.6/IC 104.1）。
    # 各仅当天生效，之后恢复正常链式读取。
    if date_yyyymmdd == "20261119":
        return {"IH": 151.3, "IF": 126.8, "IC": 123.0}
    if date_yyyymmdd == "20261120":
        return {"IH": 153.3, "IF": 136.6, "IC": 104.1}
    try:
        base = datetime.strptime(date_yyyymmdd, "%Y%m%d").date()
    except ValueError:
        return {}
    for back in range(1, 6):
        day = (base - timedelta(days=back)).strftime("%Y%m%d")
        path = os.path.join(outdir, "extremes-%s.json" % day)
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                prev = json.load(fh)
        except (OSError, ValueError):
            continue
        out = {}
        for name, rec in (prev.get("contracts") or {}).items():
            if rec.get("last") is not None:
                out[name] = rec["last"]
        if out:
            return out
    return {}


def update_extremes(outdir, row):
    date = row["ts_cst"][:10].replace("-", "")
    path = os.path.join(outdir, "extremes-%s.json" % date)
    state = None
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                state = json.load(fh)
        except (OSError, ValueError):
            state = None
    if not isinstance(state, dict) or state.get("date") != row["ts_cst"][:10]:
        state = {
            "date": row["ts_cst"][:10],
            "source": "public 2s series (Tencent ETF x1000 - Sina futures); software basis governs on conflict",
            "contracts": {},
            "im": None,
            "im_note": "no working public source from box-1; use software basis only",
            "anchors": {"0935": None, "1305": None, "1120": None, "1450": None},
            "quality": {"polls": 0, "errors": 0, "max_gap_s": 0.0,
                        "last_ts_epoch": None},
        }
    session = _session_of(row["ts_cst"])
    # Anchor reads (user rule 2026-11-16): open/close references use ONLY
    # 09:35, 13:05, 11:20, 14:50. Filled per contract from the first row in
    # the 60s window after the anchor time with a usable (fresh-leg) value;
    # the recompute script later refines them to anchor-minute medians.
    anchors = state.setdefault(
        "anchors", {"0935": None, "1305": None, "1120": None, "1450": None})
    row_t = row["ts_cst"][11:19]
    for label, at in (("0935", "09:35:00"), ("1305", "13:05:00"),
                      ("1120", "11:20:00"), ("1450", "14:50:00")):
        if at <= row_t <= at[:6] + "59":
            slot = anchors.get(label)
            if slot is None:
                slot = {"IH": None, "IF": None, "IC": None}
                anchors[label] = slot
            for name, value in row["basis"].items():
                if slot.get(name) is None and value is not None:
                    slot[name] = value
    quality = state["quality"]
    last_epoch = quality.get("last_ts_epoch")
    if last_epoch is not None:
        gap = round(row["ts_epoch"] - last_epoch, 1)
        if gap > quality.get("max_gap_s", 0.0):
            quality["max_gap_s"] = gap
    quality["last_ts_epoch"] = row["ts_epoch"]
    quality["polls"] = quality.get("polls", 0) + 1
    quality["errors"] = quality.get("errors", 0) + len(row["errors"])
    prev = _prev_close(outdir, date)
    for name, value in row["basis"].items():
        if value is None:
            continue
        rec = state["contracts"].get(name)
        if rec is None:
            anchored = row["ts_cst"][11:19] >= "09:35:00"
            rec = {"open": value if anchored else None,
                   "open_ts": row["ts_cst"] if anchored else None,
                   "high": value,
                   "high_ts": row["ts_cst"], "low": value, "low_ts": row["ts_cst"],
                   "am_high": None, "am_high_ts": None, "am_low": None,
                   "am_low_ts": None, "pm_high": None, "pm_high_ts": None,
                   "pm_low": None, "pm_low_ts": None, "last": value,
                   "last_ts": row["ts_cst"]}
            state["contracts"][name] = rec
        if rec.get("open") is None and row["ts_cst"][11:19] >= "09:35:00":
            rec["open"], rec["open_ts"] = value, row["ts_cst"]
        if value > rec["high"]:
            rec["high"], rec["high_ts"] = value, row["ts_cst"]
        if value < rec["low"]:
            rec["low"], rec["low_ts"] = value, row["ts_cst"]
        if session:
            hi_key, lo_key = session + "_high", session + "_low"
            if rec[hi_key] is None or value > rec[hi_key]:
                rec[hi_key], rec[hi_key + "_ts"] = value, row["ts_cst"]
            if rec[lo_key] is None or value < rec[lo_key]:
                rec[lo_key], rec[lo_key + "_ts"] = value, row["ts_cst"]
        rec["last"], rec["last_ts"] = value, row["ts_cst"]
        rec["range"] = round(rec["high"] - rec["low"], 2)
        pc = prev.get(name)
        rec["prev_close"] = pc
        rec["chg_vs_prev_close"] = round(value - pc, 2) if pc is not None else None
    state["updated_ts_cst"] = row["ts_cst"]
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False)
    os.replace(tmp, path)


def write_row(outdir, row):
    date = row["ts_cst"][:10].replace("-", "")
    os.makedirs(outdir, exist_ok=True)
    path = os.path.join(outdir, "basis-%s.jsonl" % date)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    latest = dict(row)
    latest["interval_s"] = CURRENT_INTERVAL
    tmp = os.path.join(outdir, "latest.json.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(latest, fh, ensure_ascii=False)
    os.replace(tmp, os.path.join(outdir, "latest.json"))
    update_extremes(outdir, row)


CURRENT_INTERVAL = 2.0


def pid_alive(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description="Poll public basis every N seconds.")
    parser.add_argument("--once", action="store_true", help="Single poll, print row, exit.")
    parser.add_argument("--seconds", type=float, default=7200.0, help="Loop duration.")
    parser.add_argument("--interval", type=float, default=2.0, help="Poll interval seconds.")
    parser.add_argument("--outdir", default="/data/basis", help="Output directory.")
    args = parser.parse_args(argv)
    global CURRENT_INTERVAL
    CURRENT_INTERVAL = args.interval
    os.makedirs(args.outdir, exist_ok=True)
    if args.once:
        row = poll()
        write_row(args.outdir, row)
        print(json.dumps(row, ensure_ascii=False))
        return 0
    pidfile = os.path.join(args.outdir, "collector.pid")
    if os.path.exists(pidfile):
        try:
            old_pid = int(open(pidfile, encoding="utf-8").read().strip())
        except ValueError:
            old_pid = 0
        if old_pid and pid_alive(old_pid):
            print("collector already running (pid %d); refusing second instance" % old_pid,
                  file=sys.stderr)
            return 2
    with open(pidfile, "w", encoding="utf-8") as fh:
        fh.write(str(os.getpid()))
    print("collector started pid=%d interval=%ss duration=%ss outdir=%s"
          % (os.getpid(), args.interval, args.seconds, args.outdir), flush=True)
    polls = 0
    overruns = 0
    end = time.monotonic() + args.seconds
    try:
        while time.monotonic() < end:
            t0 = time.monotonic()
            try:
                write_row(args.outdir, poll())
                polls += 1
            except Exception as exc:  # noqa: BLE001 - keep polling
                print("poll error: %s" % exc, file=sys.stderr, flush=True)
            spent = time.monotonic() - t0
            if spent > args.interval:
                overruns += 1
            else:
                time.sleep(args.interval - spent)
    finally:
        try:
            os.remove(pidfile)
        except OSError:
            pass
    print("collector stopped polls=%d overruns=%d" % (polls, overruns), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
