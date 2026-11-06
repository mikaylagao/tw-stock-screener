# -*- coding: utf-8 -*-
"""篩選引擎：讀取 data/daily/*.json，計算所有指標與條件（三態：True／False／None=未判定）。

回看歷史日期時，只使用截至該日已知的資料（指標皆由 <= t 的資料計算；處置公告只看公布日 <= t）。
"""
import datetime as dt, glob, json, math, os
from decimal import Decimal, ROUND_FLOOR

BASE = os.path.dirname(os.path.abspath(__file__))
DAILY = os.path.join(BASE, "data", "daily")
DISPOSAL_FILE = os.path.join(BASE, "data", "disposal.json")
TOL = 1e-8
MACD_MIN = 120

COND_KEYS = ["c1", "c2", "c3", "c4", "c5", "c6", "c7", "c8", "c9", "c10", "luPrev", "mS", "mG", "mT", "disp"]
STRAT_KEYS = [f"s{i}" for i in range(1, 12)]
ALL_KEYS = COND_KEYS + STRAT_KEYS
ENC = {True: "1", False: "0", None: "?"}
DEC = {"1": True, "0": False, "?": None}


def AND(*xs):
    if any(x is False for x in xs):
        return False
    if any(x is None for x in xs):
        return None
    return True


def tick(p):
    if p < 10: return Decimal("0.01")
    if p < 50: return Decimal("0.05")
    if p < 100: return Decimal("0.1")
    if p < 500: return Decimal("0.5")
    if p < 1000: return Decimal("1")
    return Decimal("5")


def calc_limit_up(ref):
    """依官方參考價 ×1.1，按升降單位無條件捨去。"""
    raw = Decimal(str(ref)) * Decimal("1.1")
    t = tick(raw)
    return float((raw / t).to_integral_value(rounding=ROUND_FLOOR) * t)


def calc_limit_down(ref):
    """參考價 ×0.9，按升降單位無條件進位。"""
    raw = Decimal(str(ref)) * Decimal("0.9")
    t = tick(raw)
    return float((raw / t).to_integral_value(rounding="ROUND_CEILING") * t)


def ema_series(vals, n):
    """SMA 初始化的 EMA；回傳與 vals 等長，前 n-1 筆為 None。"""
    out = [None] * len(vals)
    if len(vals) < n:
        return out
    k = 2.0 / (n + 1)
    e = sum(vals[:n]) / n
    out[n - 1] = e
    for i in range(n, len(vals)):
        e = vals[i] * k + e * (1 - k)
        out[i] = e
    return out


def macd_series(closes):
    e12, e26 = ema_series(closes, 12), ema_series(closes, 26)
    dif = [a - b if a is not None and b is not None else None for a, b in zip(e12, e26)]
    valid = [(i, d) for i, d in enumerate(dif) if d is not None]
    dea = [None] * len(closes)
    if valid:
        sig = ema_series([d for _, d in valid], 9)
        for (i, _), s in zip(valid, sig):
            dea[i] = s
    hist = [2 * (d - s) if d is not None and s is not None else None for d, s in zip(dif, dea)]
    return dif, dea, hist


class Engine:
    def __init__(self):
        self.load()

    # ------------------------------------------------------------
    def load(self):
        files = sorted(glob.glob(os.path.join(DAILY, "*.json")))
        self.days, self.status, raw = [], {}, []
        for f in files:
            with open(f, encoding="utf-8") as fh:
                d = json.load(fh)
            if d.get("holiday"):
                continue
            if not (d.get("twse", {}).get("ok") or d.get("tpex", {}).get("ok")):
                continue
            self.days.append(d["date"])
            st = {k: bool(d.get(k, {}).get("ok")) for k in ("twse", "tpex", "inst_twse", "inst_tpex")}
            prov = [d[k].get("asOf", "") for k in ("twse", "tpex") if d.get(k, {}).get("provisional")]
            st["provisional"] = bool(prov)
            st["asOf"] = max(prov) if prov else None
            self.status[d["date"]] = st
            raw.append(d)
        self.didx = {d: i for i, d in enumerate(self.days)}
        N = len(self.days)
        # bars[code][i] = (mkt, name, o, h, l, c, v, chg, nextLU, limitToday)
        # limitToday 只有暫行版（即時行情）提供：證交所公布之當日漲停價
        self.bars, self.inst = {}, {}
        for i, d in enumerate(raw):
            for mkt_key, mkt in (("twse", "上市"), ("tpex", "上櫃")):
                part = d.get(mkt_key, {})
                if not part.get("ok"):
                    continue
                for code, r in part["rows"].items():
                    self.bars.setdefault(code, [None] * N)[i] = (mkt, r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7],
                                                                 r[8] if len(r) > 8 else None)
            for ik in ("inst_twse", "inst_tpex"):
                part = d.get(ik, {})
                if part.get("ok"):
                    for code, f in part["rows"].items():
                        self.inst.setdefault(code, {})[i] = f
        self.inst_ok = [(self.status[dd]["inst_twse"], self.status[dd]["inst_tpex"]) for dd in self.days]
        # 處置
        self.disp_db = {"records": [], "covered": {}}
        if os.path.exists(DISPOSAL_FILE):
            with open(DISPOSAL_FILE, encoding="utf-8") as fh:
                self.disp_db = json.load(fh)
        self.disp_by_code = {}
        for r in self.disp_db["records"]:
            if r.get("start") and r.get("end") and r.get("pub"):
                self.disp_by_code.setdefault(r["code"], []).append(r)
        self._prep()

    # ------------------------------------------------------------
    def _prep(self):
        N = len(self.days)
        first = self.days[0] if self.days else "99999999"
        self.pre = {}
        for code, bars in self.bars.items():
            closes, cidx = [], []
            for i, b in enumerate(bars):
                if b and b[5] is not None:
                    closes.append(b[5]); cidx.append(i)
            dif, dea, hist = macd_series(closes)
            pos = {i: k for k, i in enumerate(cidx)}  # day index -> position in closes
            # 每日之前（含）最後一筆有效收盤位置
            upto, k = [-1] * N, -1
            for i in range(N):
                if k + 1 < len(cidx) and cidx[k + 1] == i:
                    k += 1
                upto[i] = k
            # 漲停判定
            lim = [None] * N
            limit_px = [None] * N
            for i, b in enumerate(bars):
                if not b:
                    continue
                mkt, c, chg = b[0], b[5], b[7]
                lp = None
                if b[9]:
                    lp = b[9]  # 暫行版：即時行情的當日漲停價
                elif mkt == "上櫃" and i > 0 and bars[i - 1] and bars[i - 1][0] == "上櫃" and bars[i - 1][8]:
                    lp = bars[i - 1][8]  # 櫃買中心前一日公告之「次日漲停價」
                elif c is not None and chg is not None:
                    ref = round(c - chg, 4)
                    if ref > 0:
                        lp = calc_limit_up(ref)
                limit_px[i] = lp
                if c is None:
                    lim[i] = False if b[6] == 0 else None  # 無成交 → 非收盤漲停
                elif lp is None:
                    lim[i] = None
                else:
                    lim[i] = abs(c - lp) < 1e-6
            # 月 K：各月首次成交開盤、最後收盤（含日期索引）
            mopen, mclose = {}, {}
            for i, b in enumerate(bars):
                if b and b[2] is not None and b[5] is not None:
                    ym = self.days[i][:6]
                    if ym not in mopen:
                        mopen[ym] = (i, b[2])
                    mclose[ym] = (i, b[5])
            self.pre[code] = dict(closes=closes, cidx=cidx, pos=pos, upto=upto, dif=dif, dea=dea, hist=hist,
                                  lim=lim, limit_px=limit_px, mopen=mopen, mclose=mclose)
        self.first_day = first
        # 預先計算每檔每日的條件字串，供「前次符合日」使用
        self.flagstr = {}
        for code in self.bars:
            s = []
            for i in range(N):
                r = self.metrics(code, i) if self.bars[code][i] else None
                s.append("".join(ENC[r["f"][k]] for k in ALL_KEYS) if r else "")
            self.flagstr[code] = s

    # ------------------------------------------------------------
    def _ma5(self, P, i):
        k = P["upto"][i]
        if k < 4:
            return None
        return sum(P["closes"][k - 4:k + 1]) / 5

    def _F(self, code, i, mkt):
        ok = self.inst_ok[i][0 if mkt == "上市" else 1]
        if not ok:
            return None
        return self.inst.get(code, {}).get(i, 0.0)  # 當日資料存在但未列出 → 投信無交易，視為 0

    def _prev_month_covered(self, ym_prev):
        """資料起點是否涵蓋上個月第一個交易日。"""
        y, m = int(ym_prev[:4]), int(ym_prev[4:])
        d = dt.date(y, m, 1)
        while d.weekday() >= 5:
            d += dt.timedelta(days=1)
        return self.first_day <= d.strftime("%Y%m%d")

    def _disposal(self, code, i, traded):
        t = self.days[i]
        recs = [r for r in self.disp_by_code.get(code, []) if r["pub"] <= t]
        hit = [r for r in recs if r["end"] == t]
        info = None
        cur = [r for r in recs if r["start"] <= t <= r["end"]]
        if cur:
            r = max(cur, key=lambda r: (r["pub"], r["end"]))
            info = {"start": r["start"], "end": r["end"], "kind": r["kind"], "pub": r["pub"]}
        if not hit:
            return False, info
        # 再次處置：已公告、起始日晚於 t 且與 t 相接的新處置期間 → 未出關
        if any(r["start"] > t and r["pub"] <= t for r in recs):
            return False, info
        # 若期間內曾因停牌順延，可能另有調整後迄日較晚的公告
        if any(r["end"] > t and r["start"] <= t for r in recs):
            return False, info
        if not traded:
            return None, info  # 當日無成交／停牌，可能順延 → 待核對
        return True, info

    # ------------------------------------------------------------
    def metrics(self, code, i):
        bars = self.bars[code]
        b = bars[i]
        if not b:
            return None
        P = self.pre[code]
        mkt, name, o, h, l, c, v, chg = b[:8]
        bp = bars[i - 1] if i > 0 else None
        hp = bp[3] if bp else None
        lp_ = bp[4] if bp else None
        vp = bp[6] if bp else None
        cp = bp[5] if bp else None
        traded = c is not None
        ref = round(c - chg, 4) if (c is not None and chg is not None) else None
        chg_pct = (c / ref - 1) * 100 if ref else None
        ma5 = self._ma5(P, i) if traded else None
        ma5p = self._ma5(P, i - 1) if i > 0 else None
        vinc = (v / vp - 1) * 100 if (v is not None and vp) else None

        def gt(a, b_):
            return None if a is None or b_ is None else a > b_

        c1 = gt(h, hp)
        c2 = gt(v, vp)
        # 近 20 個交易日曾收盤漲停
        lu20d = None
        if i >= 19:
            win = [P["lim"][j] if bars[j] else None for j in range(i - 19, i + 1)]
            hits = [j for j, x in zip(range(i - 19, i + 1), win) if x is True]
            if hits:
                c3, lu20d = True, self.days[hits[-1]]
            elif any(x is None for x in win):
                c3 = None
            else:
                c3 = False
        else:
            c3 = None
        c4 = None if (o is None or c is None) else round(o, 4) == round(c, 4)
        c5 = gt(ma5, ma5p)
        c6 = None if (v is None or vp is None) else v <= vp * 0.5
        c7 = None if (c is None or ma5 is None) else c < ma5
        F = self._F(code, i, mkt)
        Fp = self._F(code, i - 1, bp[0]) if (i > 0 and bp) else None
        c8 = AND(None if F is None else F > 0, None if Fp is None else Fp > 0)
        # 月 K
        ym = self.days[i][:6]
        y, m = int(ym[:4]), int(ym[4:])
        pym = f"{y - 1:04d}12" if m == 1 else f"{y:04d}{m - 1:02d}"
        pmO = P["mopen"].get(pym, (None, None))[1]
        pmC = P["mclose"].get(pym, (None, None))[1]
        cm = P["mopen"].get(ym)
        cmO = cm[1] if cm and cm[0] <= i else None
        if not self._prev_month_covered(pym):
            c9 = None
        elif pmO is None or pmC is None:
            # 上月無成交（如新上市）→ 未判定
            c9 = None
        elif not pmC < pmO:
            c9 = False
        else:
            c9 = gt(c, cmO)
        c10 = AND(gt(l, lp_), None if (v is None or vp is None) else v < vp)
        lu_prev = P["lim"][i - 1] if (i > 0 and bp) else None
        # MACD
        k = P["pos"].get(i)
        mn = P["upto"][i] + 1
        dif = dea = hist = histp = None
        if k is not None and cp is not None and k >= 1 and mn >= MACD_MIN:
            dif, dea, hist, histp = P["dif"][k], P["dea"][k], P["hist"][k], P["hist"][k - 1]
        if hist is None or histp is None:
            mS = mG = mT = None
        else:
            mS = histp < -TOL and hist < -TOL and hist > histp
            mG = histp > TOL and hist > TOL and hist > histp
            mT = histp <= TOL and hist > TOL
        disp, dinfo = self._disposal(code, i, traded and v is not None and v > 0)
        f = dict(c1=c1, c2=c2, c3=c3, c4=c4, c5=c5, c6=c6, c7=c7, c8=c8, c9=c9, c10=c10,
                 luPrev=lu_prev, mS=mS, mG=mG, mT=mT, disp=disp)
        f.update(s1=AND(c1, c2), s2=AND(c3, c7), s3=AND(lu_prev, c4), s4=AND(c5, c6), s5=c10, s6=c8,
                 s7=disp, s8=c9, s9=mS, s10=mG, s11=mT)
        return dict(code=code, name=name, mkt=mkt, o=o, h=h, l=l, c=c, v=v, ref=ref, chg=chg_pct,
                    hp=hp, lp=lp_, vp=vp, ma5=ma5, ma5p=ma5p, vinc=vinc, lu20d=lu20d,
                    limit=P["limit_px"][i], F=F, Fp=Fp, pmO=pmO, pmC=pmC, cmO=cmO,
                    dif=dif, dea=dea, hist=hist, histp=histp, mn=mn, disp=dinfo, f=f)

    # ------------------------------------------------------------
    def screen(self, date):
        if date not in self.didx:
            return None
        i = self.didx[date]
        rows = []
        for code, bars in self.bars.items():
            if not bars[i]:
                continue
            r = self.metrics(code, i)
            fs = self.flagstr[code]
            last = {}
            for si, sk in enumerate(STRAT_KEYS):
                pos = len(COND_KEYS) + si
                for j in range(i - 1, -1, -1):
                    if fs[j] and fs[j][pos] == "1":
                        last[sk] = self.days[j]
                        break
            r["last"] = last
            # JSON 不接受 NaN
            for kk, vv in list(r.items()):
                if isinstance(vv, float) and (math.isnan(vv) or math.isinf(vv)):
                    r[kk] = None
            rows.append(r)
        cov = self.disp_db.get("covered", {})
        picks, alerts, market, comment = self._picks(rows, date)
        return {
            "picks": picks,
            "alerts": alerts,
            "market": market,
            "comment": comment,
            "date": date,
            "prevDate": self.days[i - 1] if i > 0 else None,
            "status": self.status[date],
            "prevStatus": self.status[self.days[i - 1]] if i > 0 else None,
            "historyStart": self.days[0],
            "historyCount": i + 1,
            "disposalCovered": cov,
            "disposalFetchedAt": self.disp_db.get("fetchedAt"),
            "rows": rows,
        }

    # ------------------------------------------------------------
    PICK_MIN_CLOSE = 200
    PICK_MIN_LOTS = 500
    PICK_TOP = 12

    ALERT_TOP = 12

    def _nh(self, code, i):
        """(收盤創前 60 日新高, 創前 60 日新低, 創資料期間新高)，結果快取。"""
        cache = self.__dict__.setdefault("_nh_cache", {})
        key = (code, i)
        if key in cache:
            return cache[key]
        bars = self.bars[code]
        b = bars[i]
        res = (None, None, None)
        if b and b[5] is not None:
            c = b[5]
            prev_c = [bars[j][5] for j in range(max(0, i - 60), i) if bars[j] and bars[j][5] is not None]
            nh = nl = na = None
            if len(prev_c) >= 40:
                nh, nl = c > max(prev_c), c < min(prev_c)
            P = self.pre[code]
            k = P["upto"][i - 1] if i > 0 else -1  # 前一日（含）之前的收盤筆數-1
            if k + 1 >= 100:
                if "cmax" not in P:
                    m, acc = float("-inf"), []
                    for x in P["closes"]:
                        m = max(m, x); acc.append(m)
                    P["cmax"] = acc
                na = c > P["cmax"][k]
            res = (nh, nl, na)
        cache[key] = res
        return res

    def _price_state(self, code, i):
        """純價格強弱狀態（只看收盤價、漲跌停，不用均線/MACD/籌碼）。"""
        bars, P = self.bars[code], self.pre[code]
        b = bars[i]
        if not b or b[5] is None:
            return None
        c, h = b[5], b[3]
        nh, nl, na = self._nh(code, i)
        st = {"nh60": nh, "nl60": nl, "nhAll": na}
        # 連漲天數（收盤 > 前一日收盤）
        up, j = 0, i
        while j > 0 and bars[j] and bars[j - 1] and bars[j][5] is not None and bars[j - 1][5] is not None and bars[j][5] > bars[j - 1][5]:
            up += 1; j -= 1
        st["upDays"] = up
        c3 = bars[i - 3][5] if i >= 3 and bars[i - 3] else None
        st["gain3"] = (c / c3 - 1) * 100 if c3 else None
        lim, lp = P["lim"], P["limit_px"][i]
        st["lu"] = lim[i] is True
        st["luPrev"] = i > 0 and lim[i - 1] is True
        st["touchLU"] = bool(lp and h is not None and h >= lp - 1e-6 and c < lp - 1e-6)
        ref = round(c - b[7], 4) if b[7] is not None else None
        st["ld"] = bool(ref and ref > 0 and abs(c - calc_limit_down(ref)) < 1e-6)
        lows5 = [bars[j][4] for j in range(max(0, i - 5), i) if bars[j] and bars[j][4] is not None]
        st["breakLow5"] = bool(lows5) and c < min(lows5)
        # 近 10 日內是否曾創 60 日收盤新高
        st["recentNH"] = any(self._nh(code, k)[0] for k in range(max(1, i - 10), i))
        # 突破整數關卡
        pc = bars[i - 1][5] if i > 0 and bars[i - 1] else None
        step = 10 if c < 100 else 50 if c < 1000 else 100 if c < 5000 else 500
        lvl = int(c // step) * step
        st["round"] = lvl if (pc is not None and lvl > 0 and pc < lvl <= c) else None
        return st

    def _picks(self, rows, date):
        """強勢股觀察：以價格強弱為核心（漲停、收盤創新高、突破前高／整數關卡、連漲、逆勢不跌），
        並列出強勢股轉弱的出場警示。規則固定、可重現。"""
        i = self.didx[date]
        traded = [r for r in rows if r["c"] is not None and r["chg"] is not None]
        up = sum(1 for r in traded if r["chg"] > 0)
        down = sum(1 for r in traded if r["chg"] < 0)
        states = {r["code"]: self._price_state(r["code"], i) for r in traded}
        cnt = lambda key: sum(1 for s in states.values() if s and s.get(key))
        nh, nl, lu, ld = cnt("nh60"), cnt("nl60"), cnt("lu"), cnt("ld")
        # 前一交易日的新高家數（比較市場熱度）
        nh_prev = sum(1 for code in self.bars if self._nh(code, i - 1)[0]) if i > 0 else None
        weak = down > up * 1.5
        strong_mkt = up > down * 1.5
        market = {"traded": len(traded), "up": up, "down": down, "flat": len(traded) - up - down,
                  "limitUp": lu, "limitDown": ld, "newHigh": nh, "newLow": nl, "newHighPrev": nh_prev,
                  "tone": "弱" if weak else "強" if strong_mkt else "中性"}
        picks, alerts = [], []
        for r in traded:
            s = states.get(r["code"])
            if not s or r["c"] < self.PICK_MIN_CLOSE or (r["v"] or 0) < self.PICK_MIN_LOTS * 1000:
                continue
            in_disp = r["disp"] and r["disp"]["start"] <= date <= r["disp"]["end"] and r["f"]["s7"] is not True
            # ---- 出場警示（強勢股轉弱）----
            warn = []
            if s["luPrev"] and r["chg"] < 0:
                warn.append("漲停次日轉跌")
            if s["touchLU"] and r["chg"] < 0:
                warn.append("盤中觸及漲停後翻黑")
            if s["upDays"] >= 3 and (s["gain3"] or 0) >= 15 and (r["vinc"] or 0) >= 100:
                warn.append(f"連漲 {s['upDays']} 日後爆量（量增 {r['vinc']:.0f}%）")
            if s["recentNH"] and s["breakLow5"]:
                warn.append("近期創高後跌破前 5 日低點（支撐）")
            if warn:
                alerts.append({"code": r["code"], "name": r["name"], "mkt": r["mkt"], "c": r["c"], "chg": r["chg"],
                               "v": r["v"], "vinc": r["vinc"], "warn": warn})
            # ---- 強勢股觀察 ----
            if in_disp or s["ld"] or r["chg"] < 0:
                continue  # 處置中、跌停、當日下跌不列入（只追強勢股）
            score, why, risk = 0, [], []
            def add(pts, text):
                nonlocal score
                score += pts; why.append(text)
            if s["lu"]: add(3, "收盤漲停")
            if s["nh60"]:
                add(3, "收盤創 60 日新高")
                if s["nhAll"]: add(1, "創資料期間（約半年）新高")
            if s["upDays"] >= 3 and not s["lu"]: add(2, f"連漲 {s['upDays']} 日・漲不停")
            if weak and r["chg"] >= 0: add(2, f"大盤弱勢（{up} 漲／{down} 跌）中逆勢{'收紅' if r['chg'] > 0 else '平盤'}")
            if s["round"]: add(1, f"突破整數關卡 {s['round']:,}")
            if r["f"]["s1"]: add(1, "突破昨高且量增")
            if score < 3:
                continue
            if r["vinc"] is not None and r["vinc"] > 200:
                risk.append(f"爆量（量增 {r['vinc']:.0f}%），留意漲勢是否趨緩")
            if s["upDays"] >= 3 and (s["gain3"] or 0) >= 15:
                risk.append(f"3 日已漲 {s['gain3']:.0f}%，避免追高重押")
            if warn:
                risk.extend(warn)
            picks.append({"code": r["code"], "name": r["name"], "mkt": r["mkt"], "c": r["c"], "chg": r["chg"],
                          "v": r["v"], "vinc": r["vinc"], "score": score, "why": why, "risk": risk})
        picks.sort(key=lambda x: (-x["score"], -x["chg"]))
        alerts.sort(key=lambda x: (-len(x["warn"]), x["chg"]))
        picks, alerts = picks[:self.PICK_TOP], alerts[:self.ALERT_TOP]
        # ---- 盤勢評語 ----
        tone = {"弱": "賣壓明顯、市場偏弱", "強": "買盤積極、市場偏強", "中性": "多空互見"}[market["tone"]]
        cm = [f"今日上漲 {up} 家、下跌 {down} 家，{tone}。收盤創 60 日新高 {nh} 檔、創新低 {nl} 檔；漲停 {lu} 檔、跌停 {ld} 檔。"]
        if nh_prev is not None:
            diff = nh - nh_prev
            cm.append(f"創新高家數較前一交易日{'增加' if diff > 0 else '減少' if diff < 0 else '持平'}"
                      f"{'' if diff == 0 else f' {abs(diff)} 檔'}（{nh_prev} → {nh}）。")
        if nl > nh:
            cm.append("新低多於新高，強勢股變少：只留仍會漲停、創新高的股票，其餘降低持股，不攤平。")
        elif nh > nl and up >= down:
            cm.append("強勢股增加：可從收盤創新高、漲停鎖住的個股中挑選，分散、小量布局。")
        else:
            cm.append("盤勢拉鋸：以逆勢不跌、突破前高或整數關卡的個股為主要觀察對象。")
        if picks:
            cm.append("今日價格表現最強：" + "、".join(f"{p['name']}（{p['why'][0]}）" for p in picks[:3]) + "。")
        else:
            cm.append("今日收盤價 ≥200 元的個股中，沒有符合強勢條件者，宜觀望。")
        if alerts:
            cm.append(f"另有 {len(alerts)} 檔強勢股出現轉弱訊號，若持有宜先減碼一半。")
        return picks, alerts, market, cm

    def dates(self):
        return [{"date": d, **self.status[d]} for d in reversed(self.days)]
