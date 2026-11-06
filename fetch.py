# -*- coding: utf-8 -*-
"""抓取證交所(TWSE)、櫃買中心(TPEx)每日行情、投信買賣超、處置公告，存成正規化 JSON。

用法：
  py fetch.py                       # 補到今天（自動從最後一個已存日期接續；首次從 2026-04-01 開始）
  py fetch.py --from 20260401 --to 20261106
  py fetch.py --refetch 20261105    # 重抓某日
"""
import argparse, datetime as dt, json, os, re, ssl, sys, time, urllib.request, urllib.error

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
DAILY = os.path.join(DATA, "daily")
DISPOSAL_FILE = os.path.join(DATA, "disposal.json")
HISTORY_START = "20260401"  # 與原平台相同的 MACD 歷史起點
UA = {"User-Agent": "Mozilla/5.0 (tw-stock-screener; personal use)"}
SLEEP = 2.5  # 官方網站有頻率限制，勿調太低
STOCK_RE = re.compile(r"^[1-9]\d{3}$")  # 一般股票（排除 ETF/權證/特別股等）

_last_call = {"twse": 0.0, "tpex": 0.0}


def log(*a):
    print(*a, flush=True)


def http_json(url, host):
    wait = SLEEP - (time.time() - _last_call[host])
    if wait > 0:
        time.sleep(wait)
    for attempt in range(4):
        try:
            _last_call[host] = time.time()
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=40) as r:
                txt = r.read(20_000_000).decode("utf-8", "replace")
            return json.loads(txt)
        except Exception as e:  # 網路中斷、被限流、回應不完整等皆重試
            log(f"  ! {host} 重試 {attempt + 1}: {e!r}")
            time.sleep(6 * (attempt + 1))
    return None


def num(s):
    if s is None:
        return None
    s = str(s).replace(",", "").strip()
    if s in ("", "--", "---", "----", "X", "除權息", "除息", "除權"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def roc(d):  # 20261105 -> 2026/11/05
    return f"{d[:4]}/{d[4:6]}/{d[6:]}"


def roc_to_ymd(s):  # 115/11/05 -> 20261105
    m = re.match(r"\s*(\d{2,3})/(\d{1,2})/(\d{1,2})", s)
    if not m:
        return None
    return f"{int(m.group(1)) + 1911:04d}{int(m.group(2)):02d}{int(m.group(3)):02d}"


# ---------------- 各資料源 ----------------

def fetch_twse_quotes(d):
    j = http_json(f"https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?date={d}&type=ALLBUT0999&response=json", "twse")
    if j is None:
        return {"ok": False, "err": "連線失敗"}
    if j.get("stat") != "OK":
        return {"ok": False, "nodata": True, "err": j.get("stat")}
    for tb in j.get("tables", []):
        f = tb.get("fields") or []
        if f and f[0] == "證券代號" and "收盤價" in f:
            rows = {}
            for r in tb.get("data", []):
                code = r[0].strip()
                if not STOCK_RE.match(code):
                    continue
                sign_raw = r[9]
                chg = num(r[10])
                if "X" in sign_raw:
                    chg = None  # 不比價，參考價無法由此推得
                elif "-" in sign_raw and chg is not None:
                    chg = -chg
                elif "+" not in sign_raw and chg is not None:
                    chg = 0.0 if chg == 0 else chg
                rows[code] = [r[1].strip(), num(r[5]), num(r[6]), num(r[7]), num(r[8]), num(r[2]), chg, None]
            return {"ok": True, "rows": rows}
    return {"ok": False, "nodata": True, "err": "找不到個股收盤行情表"}


def fetch_tpex_quotes(d):
    j = http_json(f"https://www.tpex.org.tw/www/zh-tw/afterTrading/otc?date={roc(d)}&type=EW&response=json", "tpex")
    if j is None:
        return {"ok": False, "err": "連線失敗"}
    tbs = [t for t in j.get("tables", []) if t and t.get("data")]
    if not tbs or str(j.get("date")) != d:
        return {"ok": False, "nodata": True, "err": "無資料"}
    tb = tbs[0]
    rows = {}
    for r in tb["data"]:
        code = r[0].strip()
        if not STOCK_RE.match(code):
            continue
        chg_s = str(r[3]).strip().replace(",", "")
        try:
            chg = float(chg_s)
        except ValueError:
            chg = None
        rows[code] = [r[1].strip(), num(r[4]), num(r[5]), num(r[6]), num(r[2]), num(r[7]), chg, num(r[15])]
    return {"ok": True, "rows": rows}


def fetch_twse_inst(d):
    j = http_json(f"https://www.twse.com.tw/rwd/zh/fund/T86?date={d}&selectType=ALLBUT0999&response=json", "twse")
    if j is None:
        return {"ok": False, "err": "連線失敗"}
    if j.get("stat") != "OK":
        return {"ok": False, "nodata": True, "err": j.get("stat")}
    fields = j.get("fields", [])
    idx = fields.index("投信買賣超股數") if "投信買賣超股數" in fields else 10
    rows = {}
    for r in j.get("data", []):
        code = r[0].strip()
        if STOCK_RE.match(code):
            rows[code] = num(r[idx])
    return {"ok": True, "rows": rows}


def fetch_tpex_inst(d):
    j = http_json(f"https://www.tpex.org.tw/www/zh-tw/insti/dailyTrade?type=Daily&sect=EW&date={roc(d)}&response=json", "tpex")
    if j is None:
        return {"ok": False, "err": "連線失敗"}
    tbs = [t for t in j.get("tables", []) if t and t.get("data")]
    if not tbs or str(j.get("date")) != d:
        return {"ok": False, "nodata": True, "err": "無資料"}
    rows = {}
    for r in tbs[0]["data"]:
        code = r[0].strip()
        if STOCK_RE.match(code) and len(r) >= 24:
            rows[code] = num(r[13])  # 投信買賣超股數（第4組：外資不含自營/外資自營/外資合計/投信）
    return {"ok": True, "rows": rows}


# ---------------- 處置公告 ----------------

def _period(s):
    parts = re.split(r"[~～]", s or "")
    if len(parts) != 2:
        return None, None
    return roc_to_ymd(parts[0]), roc_to_ymd(parts[1])


def fetch_disposal(start, end):
    out = []
    j = http_json(f"https://www.twse.com.tw/rwd/zh/announcement/punish?startDate={start}&endDate={end}&response=json", "twse")
    ok_twse = bool(j and j.get("stat") == "OK")
    if ok_twse:
        f = j.get("fields", [])
        for r in j.get("data", []):
            code = str(r[f.index("證券代號")]).strip()
            if not STOCK_RE.match(code):
                continue
            s, e = _period(r[f.index("處置起迄時間")])
            out.append({"mkt": "上市", "code": code, "name": str(r[3]).strip(), "pub": roc_to_ymd(r[1]),
                        "start": s, "end": e, "kind": str(r[f.index("處置措施")]).strip()})
    j = http_json(f"https://www.tpex.org.tw/www/zh-tw/bulletin/disposal?startDate={roc(start)}&endDate={roc(end)}&response=json", "tpex")
    tbs = [t for t in (j or {}).get("tables", []) if t and t.get("data")]
    ok_tpex = bool(j and j.get("stat") == "ok")
    for tb in tbs:
        for r in tb["data"]:
            code = str(r[2]).strip()
            if not STOCK_RE.match(code):
                continue
            s, e = _period(r[5])
            name = re.sub(r"\(.*", "", str(r[3])).strip()
            out.append({"mkt": "上櫃", "code": code, "name": name, "pub": roc_to_ymd(r[1]),
                        "start": s, "end": e, "kind": str(r[7]).strip()})
    return ok_twse, ok_tpex, out


def update_disposal(start, end):
    db = {"records": [], "covered": {}}
    if os.path.exists(DISPOSAL_FILE):
        with open(DISPOSAL_FILE, encoding="utf-8") as fh:
            db = json.load(fh)
    s = dt.datetime.strptime(start, "%Y%m%d").date()
    e = dt.datetime.strptime(end, "%Y%m%d").date()
    keyset = {(r["mkt"], r["code"], r["pub"], r["start"], r["end"]) for r in db["records"]}
    cur = s
    while cur <= e:
        nxt = min(cur + dt.timedelta(days=30), e)
        a, b = cur.strftime("%Y%m%d"), nxt.strftime("%Y%m%d")
        log(f"處置公告 {a}~{b}")
        ok1, ok2, recs = fetch_disposal(a, b)
        for r in recs:
            k = (r["mkt"], r["code"], r["pub"], r["start"], r["end"])
            if k not in keyset:
                keyset.add(k)
                db["records"].append(r)
        if ok1:
            db["covered"]["上市"] = max(db["covered"].get("上市", ""), b)
        if ok2:
            db["covered"]["上櫃"] = max(db["covered"].get("上櫃", ""), b)
        cur = nxt + dt.timedelta(days=1)
    db["fetchedAt"] = dt.datetime.now().isoformat(timespec="seconds")
    with open(DISPOSAL_FILE, "w", encoding="utf-8") as fh:
        json.dump(db, fh, ensure_ascii=False)


# ---------------- 每日主流程 ----------------

def day_path(d):
    return os.path.join(DAILY, f"{d}.json")


def load_day(d):
    p = day_path(d)
    if os.path.exists(p):
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)
    return None


PARTS = [("twse", fetch_twse_quotes), ("tpex", fetch_tpex_quotes),
         ("inst_twse", fetch_twse_inst), ("inst_tpex", fetch_tpex_inst)]


def fetch_day(d, force=False):
    cur = None if force else load_day(d)
    if cur and (cur.get("holiday") or all(cur.get(k, {}).get("ok") for k, _ in PARTS)):
        return cur
    cur = cur or {"date": d}
    # 先抓上市行情判斷是否休市
    for key, fn in PARTS:
        if cur.get(key, {}).get("ok"):
            continue
        res = fn(d)
        cur[key] = res
        log(f"  {d} {key}: {'OK ' + str(len(res.get('rows', {}))) if res.get('ok') else res.get('err')}")
        if key == "tpex" and not cur["twse"].get("ok") and cur["twse"].get("nodata") and res.get("nodata"):
            # 兩市場都無資料
            today = dt.date.today().strftime("%Y%m%d")
            if d < today:
                cur = {"date": d, "holiday": True}
            else:
                cur = None  # 今天尚未收盤或尚未公布，不存
            break
    if cur is not None:
        cur["fetchedAt"] = dt.datetime.now().isoformat(timespec="seconds")
        with open(day_path(d), "w", encoding="utf-8") as fh:
            json.dump(cur, fh, ensure_ascii=False, separators=(",", ":"))
    return cur


def run(start=None, end=None, refetch=None, progress=None):
    os.makedirs(DAILY, exist_ok=True)
    today = dt.date.today()
    end = end or today.strftime("%Y%m%d")
    if refetch:
        fetch_day(refetch, force=True)
        days = []
    else:
        start = start or HISTORY_START
        s = dt.datetime.strptime(start, "%Y%m%d").date()
        e = dt.datetime.strptime(end, "%Y%m%d").date()
        days = []
        while s <= e:
            if s.weekday() < 5:
                days.append(s.strftime("%Y%m%d"))
            s += dt.timedelta(days=1)
    for i, d in enumerate(days):
        if progress:
            progress(f"抓取 {d}（{i + 1}/{len(days)}）")
        fetch_day(d)
    # 處置公告：首次抓全區間，之後只更新最近 45 天（含已公告的未來處置期間）
    if progress:
        progress("抓取處置公告")
    db_exists = os.path.exists(DISPOSAL_FILE)
    ds = (today - dt.timedelta(days=45)).strftime("%Y%m%d") if db_exists else (start or HISTORY_START)
    update_disposal(ds, today.strftime("%Y%m%d"))
    if progress:
        progress("完成")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="start")
    ap.add_argument("--to", dest="end")
    ap.add_argument("--refetch")
    a = ap.parse_args()
    run(a.start, a.end, a.refetch, progress=lambda m: log("==", m))
