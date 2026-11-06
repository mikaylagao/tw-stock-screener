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
        picks, market = self._picks(rows, date)
        return {
            "picks": picks,
            "market": market,
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

    def _picks(self, rows, date):
        """今日選股觀察：依多策略共振計分排序（規則固定、可重現），附入選理由與風險提示。"""
        traded = [r for r in rows if r["c"] is not None and r["chg"] is not None]
        up = sum(1 for r in traded if r["chg"] > 0)
        down = sum(1 for r in traded if r["chg"] < 0)
        lu = sum(1 for r in traded if r["limit"] and abs(r["c"] - r["limit"]) < 1e-6)
        market = {"traded": len(traded), "up": up, "down": down, "flat": len(traded) - up - down, "limitUp": lu,
                  "strat": {k: sum(1 for r in rows if r["c"] is not None and r["c"] >= self.PICK_MIN_CLOSE and r["f"][k] is True)
                            for k in STRAT_KEYS}}
        out = []
        for r in traded:
            f = r["f"]
            if r["c"] < self.PICK_MIN_CLOSE or (r["v"] or 0) < self.PICK_MIN_LOTS * 1000:
                continue
            in_disp = r["disp"] and r["disp"]["start"] <= date <= r["disp"]["end"] and f["s7"] is not True
            if in_disp:
                continue  # 處置中（分盤撮合、流動性受限）不列入，出關日除外
            score, why, risk = 0, [], []
            def add(pts, text):
                nonlocal score
                score += pts; why.append(text)
            if f["s1"]:
                add(3, f"突破昨高且量增 {r['vinc']:.0f}%" if r["vinc"] is not None else "突破昨高且量增")
            if f["s11"]: add(3, "日MACD 由綠翻紅")
            elif f["s10"]: add(2, "日MACD 紅柱增長")
            elif f["s9"]: add(1, "日MACD 綠柱縮短")
            if f["s6"]:
                add(2, f"投信連兩日買超（合計 {round((r['F'] + r['Fp']) / 1000):,} 張）")
            if f["s8"]: add(2, "月K由綠轉紅")
            if f["s4"]: add(2, "五日線上揚、量縮一半（價穩量縮）")
            elif f["s5"]: add(1, "低點墊高、量縮")
            if f["c5"] and f["c7"] is False:
                add(1, "收盤站上上揚的五日線")
            if f["s7"]: add(1, "處置最後一日，下一交易日出關")
            if len(why) < 2 or score < 4:
                continue
            if r["limit"] and abs(r["c"] - r["limit"]) < 1e-6:
                risk.append("今日收漲停，追價風險高")
            elif r["chg"] >= 7:
                risk.append(f"今日已漲 {r['chg']:.1f}%，留意追高")
            if r["chg"] <= -5:
                risk.append(f"今日跌 {abs(r['chg']):.1f}%")
            if r["vinc"] is not None and r["vinc"] > 200:
                risk.append("爆量，留意隔日量能能否延續")
            if f["c7"]:
                risk.append("收盤仍在五日線下")
            if f["c3"] and f["c7"]:
                risk.append("近 20 日曾漲停但已跌破五日線")
            if f["c8"] is None:
                risk.append("投信資料未公布")
            out.append({"code": r["code"], "name": r["name"], "mkt": r["mkt"], "c": r["c"], "chg": r["chg"],
                        "v": r["v"], "vinc": r["vinc"], "score": score, "why": why, "risk": risk})
        out.sort(key=lambda x: (-x["score"], -(x["vinc"] if x["vinc"] is not None else -1e9)))
        return out[:self.PICK_TOP], market

    def dates(self):
        return [{"date": d, **self.status[d]} for d in reversed(self.days)]
