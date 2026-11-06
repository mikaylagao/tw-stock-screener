# -*- coding: utf-8 -*-
"""產生可放上 GitHub Pages 的靜態網站：site/（介面 + 預先算好的每日篩選資料）。

用法：py build.py [--days 60]
"""
import argparse, datetime as dt, json, os, shutil
import engine

BASE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(BASE, "web")
SITE = os.path.join(BASE, "site")


def main(keep_days):
    e = engine.Engine()
    if os.path.exists(SITE):
        shutil.rmtree(SITE)
    shutil.copytree(WEB, SITE)
    out = os.path.join(SITE, "data", "screen")
    os.makedirs(out)
    dates = e.dates()[:keep_days]
    for d in dates:
        res = e.screen(d["date"])
        with open(os.path.join(out, f"{d['date']}.json"), "w", encoding="utf-8") as fh:
            json.dump(res, fh, ensure_ascii=False, separators=(",", ":"))
    tw = dt.datetime.now(dt.timezone(dt.timedelta(hours=8)))
    with open(os.path.join(SITE, "data", "dates.json"), "w", encoding="utf-8") as fh:
        json.dump({"dates": dates, "static": True, "generatedAt": tw.strftime("%Y/%m/%d %H:%M")}, fh, ensure_ascii=False)
    open(os.path.join(SITE, ".nojekyll"), "w").close()
    print(f"site/ 已產生：{len(dates)} 個交易日（{dates[-1]['date']}～{dates[0]['date']}）" if dates else "無資料")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60)
    main(ap.parse_args().days)
