#!/usr/bin/env python3
"""配当ダッシュボード自動更新スクリプト (GitHub Actions用)

- 各ダッシュボードHTML内の銘柄コードを抽出
- 年間配当予想: IRBANK (キャッシュ data/dividends.json、7日毎に再取得)
- 株価: Yahoo Finance chart API → stooq CSV の順でフォールバック
- 現在利回り cy = 配当予想 ÷ 株価 × 100 を書き換え
- どちらも失敗した場合は IRBANK掲載の予想利回りを使用、それも無ければ既存値維持
"""
import json
import os
import re
import sys
import time
import html as htmllib
from datetime import datetime, timedelta, timezone
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

JST = timezone(timedelta(hours=9))
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"}
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DASHBOARDS = ["d1/index.html", "d2/index.html"]
DIV_CACHE = os.path.join(ROOT, "data", "dividends.json")
SUMMARY = os.path.join(ROOT, "data", "last_update.json")
CACHE_DAYS = 7
YIELD_MIN, YIELD_MAX = 0.1, 15.0


def fetch(url, timeout=30):
    req = Request(url, headers=UA)
    with urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def strip_tags(raw):
    text = re.sub(r"<script[\s\S]*?</script>", "|", raw, flags=re.I)
    text = re.sub(r"<[^>]+>", "|", text)
    return htmllib.unescape(text)


def parse_market_cap(raw_html):
    """IRBANK銘柄トップページの生HTMLから時価総額[億円]を取得。失敗時 None。"""
    m = re.search(r"<dt>\u6642\u4fa1\u7dcf\u984d</dt>\s*<dd>([^<]+)</dd>", raw_html)
    if not m:
        return None
    s = m.group(1)
    cho = re.search(r"([\d]+)\u5146", s)
    oku = re.search(r"([\d]+)\u5104", s)
    man = re.search(r"([\d]+)\u4e07", s)
    if not (cho or oku or man):
        return None
    total = 0.0
    if cho:
        total += int(cho.group(1)) * 10000
    if oku:
        total += int(oku.group(1))
    if man:
        total += int(man.group(1)) / 10000
    return round(total, 2)


def get_dividend_from_irbank(code):
    """IRBANKから (年間配当予想[円], IRBANK予想利回り[%], 時価総額[億円]) を取得。失敗時は (None, None, None)。"""
    try:
        top = fetch(f"https://irbank.net/{code}")
    except (URLError, HTTPError, OSError) as e:
        print(f"  [WARN] {code}: irbank top fetch failed: {e}")
        return None, None, None
    cap = parse_market_cap(top)
    m = re.search(r'href="/(E\d+)/dividend"', top)
    text = None
    if m:
        try:
            time.sleep(0.5)
            text = strip_tags(fetch(f"https://irbank.net/{m.group(1)}/dividend"))
        except (URLError, HTTPError, OSError) as e:
            print(f"  [WARN] {code}: irbank dividend fetch failed: {e}")
    if text is None:
        text = strip_tags(top)

    div_total, best_key = None, -1
    # 行形式: 2027年 3月 | 予想 | 中間 | 期末 | 合計 | 利回り%
    for y, mo, kind, _mid, _fin, total, _yld in re.findall(
        r"(\d{4})年[\s\|]*(\d{1,2})月[^\|]*[\s\|]+(予想|修正|実績)[\s\|]+([\d.]+|-)[\s\|]+([\d.]+|-)[\s\|]+([\d.]+)[\s\|]+([\d.]+)%",
        text,
    ):
        key = int(y) * 12 + int(mo)
        if key >= best_key:
            best_key = key
            div_total = float(total)

    ir_yield = None
    m2 = re.search(r"配当[\s\|]*予[\s\|]+([\d.]+)%", text)
    if m2:
        ir_yield = float(m2.group(1))
    return div_total, ir_yield, cap


def get_price(code):
    """現在株価。Yahoo → stooq フォールバック。失敗時 None。"""
    try:
        j = json.loads(fetch(f"https://query1.finance.yahoo.com/v8/finance/chart/{code}.T?range=1d&interval=1d"))
        p = j["chart"]["result"][0]["meta"].get("regularMarketPrice")
        if p and p > 0:
            return float(p), "yahoo"
    except Exception as e:
        print(f"  [WARN] {code}: yahoo failed: {e}")
    try:
        csv = fetch(f"https://stooq.com/q/l/?s={code}.jp&f=sd2t2ohlcv&e=csv")
        line = csv.strip().splitlines()[-1]
        c = line.split(",")[6]
        if c not in ("N/D", "", "-"):
            p = float(c)
            if p > 0:
                return p, "stooq"
    except Exception as e:
        print(f"  [WARN] {code}: stooq failed: {e}")
    return None, None


def main():
    now = datetime.now(JST)
    # 配当キャッシュ読み込み
    cache = {}
    if os.path.exists(DIV_CACHE):
        with open(DIV_CACHE, encoding="utf-8") as f:
            cache = json.load(f)

    # 全銘柄コード収集
    docs = {}
    codes = []
    for rel in DASHBOARDS:
        path = os.path.join(ROOT, rel)
        with open(path, encoding="utf-8") as f:
            docs[rel] = f.read()
        for c in re.findall(r'\{code:"(\d{4})"', docs[rel]):
            if c not in codes:
                codes.append(c)
    print(f"銘柄数: {len(codes)}")

    # 配当予想の更新（キャッシュが古い/無い銘柄のみ）
    cutoff = (now - timedelta(days=CACHE_DAYS)).strftime("%Y-%m-%d")
    for c in codes:
        ent = cache.get(c)
        if ent and ent.get("asof", "") >= cutoff and ent.get("div"):
            continue
        div, ir_y, cap = get_dividend_from_irbank(c)
        if div or ir_y or cap:
            cache[c] = {"div": div, "irbank_yield": ir_y, "cap": cap, "asof": now.strftime("%Y-%m-%d")}
            print(f"  {c}: 配当予想 {div}円 / IRBANK利回り {ir_y}% / 時価総額 {cap}億円")
            if cap is not None:
                for rel in DASHBOARDS:
                    docs[rel] = re.sub(
                        r'(\{code:"%s"[^\n]*?cap:)([\d.]+)' % c,
                        lambda m: f"{m.group(1)}{cap:.2f}",
                        docs[rel],
                    )
        else:
            print(f"  [WARN] {c}: 配当情報の取得失敗（既存キャッシュ使用）")
        time.sleep(0.7)

    # 利回り計算と書き換え
    updated, failed = [], []
    for c in codes:
        ent = cache.get(c) or {}
        cy = None
        price, src = get_price(c)
        if price and ent.get("div"):
            cy = round(ent["div"] / price * 100, 2)
            reason = f"div {ent['div']} / {src} price {price}"
        elif ent.get("irbank_yield"):
            cy = ent["irbank_yield"]
            reason = "irbank yield fallback"
        if cy is None or not (YIELD_MIN <= cy <= YIELD_MAX):
            failed.append(c)
            print(f"  [SKIP] {c}: cy={cy} 既存値維持")
            continue
        for rel in DASHBOARDS:
            docs[rel] = re.sub(
                r'(\{code:"%s"[^\n]*?cy:)([\d.]+)' % c,
                lambda m: f"{m.group(1)}{cy:.2f}",
                docs[rel],
            )
        updated.append(c)
        print(f"  [OK] {c}: cy={cy:.2f}% ({reason})")
        time.sleep(0.4)

    # 取得日の更新
    datestr = f"{now.year}年{now.month}月{now.day}日"
    for rel in DASHBOARDS:
        docs[rel] = re.sub(r"データ取得日: \d{4}年\d{1,2}月\d{1,2}日", f"データ取得日: {datestr}", docs[rel])
        with open(os.path.join(ROOT, rel), "w", encoding="utf-8") as f:
            f.write(docs[rel])

    os.makedirs(os.path.dirname(DIV_CACHE), exist_ok=True)
    with open(DIV_CACHE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=1)
    with open(SUMMARY, "w", encoding="utf-8") as f:
        json.dump(
            {"run_at": now.isoformat(), "updated": updated, "failed": failed},
            f, ensure_ascii=False, indent=1,
        )

    print(f"\n完了: 更新 {len(updated)}銘柄 / 失敗 {len(failed)}銘柄 {failed if failed else ''}")
    if not updated:
        print("[ERROR] 全銘柄の更新に失敗")
        sys.exit(1)


if __name__ == "__main__":
    main()
