#!/usr/bin/env python3
"""Fetch current quotes and the latest ten completed fiscal years for d1."""
import concurrent.futures
import html
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from html.parser import HTMLParser
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

JST = timezone(timedelta(hours=9))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UA = {"User-Agent": "Mozilla/5.0"}


def rounded(value):
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def fetch(url):
    for attempt in range(2):
        try:
            with urlopen(Request(url, headers=UA), timeout=20) as response:
                return response.read().decode("utf-8")
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            if attempt or (isinstance(exc, HTTPError) and exc.code < 500):
                raise
            time.sleep(1)


def plain(value):
    return html.unescape(re.sub(r"<[^>]*>", "", value)).strip()


class TableRows(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows = []
        self.row = None
        self.cell = None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.row = []
        elif tag in ("td", "th"):
            # IRBANK sometimes omits an opening <tr> on rowspan continuation rows.
            if self.row is None:
                self.row = []
            self.cell = []

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self.cell is not None:
            self.row.append("".join(self.cell).strip())
            self.cell = None
        elif tag == "tr" and self.row is not None:
            self.rows.append(self.row)
            self.row = None


def parse_history(raw):
    table = next((t for t in re.findall(r"<table\b[^>]*>.*?</table>", raw, re.S)
                  if "配当金の状況" in t), None)
    if table is None:
        raise ValueError("Dividend table missing")
    parser = TableRows()
    parser.feed(table)
    headers = [re.sub(r"\s+", "", c) for c in parser.rows[0]][1:]
    total_index = headers.index("合計")
    yield_index = headers.index("配当利回り")
    completed = {}
    latest_div, latest_key, year, month = None, -1, None, None
    today = datetime.now(JST).date()
    for cells in parser.rows[1:]:
        cells = cells.copy()
        match = re.fullmatch(r"(\d{4})年\s*(\d{1,2})月", cells[0])
        if match:
            year, month = int(match[1]), int(match[2])
            cells.pop(0)
        if year is None or len(cells) <= yield_index:
            continue
        kind = cells[0]
        if kind not in ("予想", "修正", "実績"):
            continue
        total = cells[total_index].replace(",", "")
        key = year * 12 + month
        if re.fullmatch(r"\d+(?:\.\d+)?", total) and key >= latest_key:
            latest_div, latest_key = float(total), key
        match = re.search(r"([\d.]+)%", cells[yield_index])
        if kind == "実績" and match and (year, month) <= (today.year, today.month):
            completed[key] = {"y": f"{year}年{month}月期", "v": float(match[1])}
    history = [completed[k] for k in sorted(completed)[-10:]]
    if len(history) != 10:
        raise ValueError(f"Expected ten actual years, found {len(history)}")
    return latest_div, history


def parse_top(raw, code):
    values = [(plain(dt), plain(dd)) for dt, dd in re.findall(
        r"<dt\b[^>]*>(.*?)</dt>\s*<dd\b[^>]*>(.*?)</dd>", raw, re.S)]
    cap_text = next(dd for dt, dd in values if dt == "時価総額")
    cap = 0.0
    for unit, multiplier in [("兆", 10000), ("億", 1), ("万", .0001)]:
        match = re.search(r"([\d,.]+)" + unit, cap_text)
        if match:
            cap += float(match[1].replace(",", "")) * multiplier
    close = float(next(dd for dt, dd in values if dt.startswith("終値")).replace(",", ""))
    date_match = re.search(r'href="/' + code + r'/chart">(\d{4}/\d{2}/\d{2})', raw)
    quote_at = datetime.strptime(date_match[1], "%Y/%m/%d").replace(hour=15, minute=30, tzinfo=JST)
    div_text = next(dd for dt, dd in values if "配当利回り" in dt and "予" in dt)
    div_match = re.search(r"([\d.]+)%\s*\(([\d,.]+)\)", div_text)
    div = float(div_match[2].replace(",", "")) if div_match else None
    company = re.search(r'href="/(E\d+)/dividend"', raw)
    if not company or cap <= 0 or close <= 0:
        raise ValueError("Required IRBANK values missing")
    return {"cap": cap, "close": close, "quote_at": quote_at, "div": div,
            "dividend_url": "https://irbank.net/" + company[1] + "/dividend"}


def get_stock(code):
    top = parse_top(fetch("https://irbank.net/" + code), code)
    dividend, history = parse_history(fetch(top["dividend_url"]))
    dividend = top["div"] if top["div"] is not None else dividend
    if dividend is None:
        raise ValueError("Annual dividend missing")
    price, quote_at, source = top["close"], top["quote_at"], "IRBANK"
    try:
        result = json.loads(fetch(f"https://query1.finance.yahoo.com/v8/finance/chart/{code}.T?range=1d&interval=1d"))
        meta = result["chart"]["result"][0]["meta"]
        yahoo_at = datetime.fromtimestamp(meta["regularMarketTime"], JST)
        yahoo_price = float(meta["regularMarketPrice"])
        if yahoo_price > 0 and quote_at <= yahoo_at <= datetime.now(JST) + timedelta(minutes=5):
            price, quote_at, source = yahoo_price, yahoo_at, "Yahoo Finance"
    except Exception as exc:
        print(f"[WARN] {code}: Yahoo unavailable, using IRBANK closing quote: {exc}", flush=True)
    current = rounded(Decimal(str(dividend)) / Decimal(str(price)) * 100)
    if not 0.1 <= current <= 15:
        raise ValueError(f"Unexpected current yield: {current}")
    highest = max(history, key=lambda x: x["v"])
    data = {"cy": current, "cap": rounded(Decimal(str(top["cap"])) * Decimal(str(price)) / Decimal(str(top["close"]))),
            "avg10y": rounded(sum(Decimal(str(h["v"])) for h in history) / 10),
            "my": highest["v"], "myr": highest["y"].removesuffix("期"),
            "hist": list(reversed(history)), "historyPeriod": history[0]["y"] + "〜" + history[-1]["y"],
            "quoteAt": quote_at.isoformat(), "quoteSource": source, "price": price, "div": dividend}
    cache = {"div": dividend, "irbank_yield": rounded(Decimal(str(dividend)) / Decimal(str(top["close"])) * 100),
             "cap": data["cap"], "asof": datetime.now(JST).strftime("%Y-%m-%d")}
    return data, cache


def main():
    page_path = os.path.join(ROOT, "d1/index.html")
    cache_path = os.path.join(ROOT, "data/dividends.json")
    page = open(page_path, encoding="utf-8").read()
    cache = json.load(open(cache_path, encoding="utf-8")) if os.path.exists(cache_path) else {}
    rows = re.findall(r'^  \{code:"(\d{4})"[^\n]*\},$', page, re.M)
    if not rows or len(rows) != len(set(rows)):
        raise ValueError("Missing or duplicate stock codes")
    updated, failed = [], []
    print(f"Refreshing {len(rows)} stocks", flush=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        tasks = {executor.submit(get_stock, code): code for code in rows}
        for task in concurrent.futures.as_completed(tasks):
            code = tasks[task]
            try:
                data, entry = task.result()
                pattern = r'^  \{code:"' + code + r'"[^\n]*\},$'
                old_row = re.search(pattern, page, re.M)[0]
                name = re.search(r'name:("(?:[^"\\]|\\.)*")', old_row)[1]
                industry = re.search(r'ind:("(?:[^"\\]|\\.)*")', old_row)[1]
                new_row = '  {code:"' + code + '",name:' + name + ', ind:' + industry
                new_row += ''.join(', ' + key + ':' + json.dumps(value, ensure_ascii=False, separators=(',', ':'))
                                   for key, value in data.items()) + '},'
                page = re.sub(pattern, lambda m: new_row, page, flags=re.M)
                cache[code] = entry
                updated.append(code)
                print(f"[OK] {code} yield={data['cy']}% cap={data['cap']} history={data['historyPeriod']} quote={data['quoteAt']}", flush=True)
            except Exception as exc:
                failed.append(code)
                print(f"[FAIL] {code}: {exc}", flush=True)
    now = datetime.now(JST)
    if not failed:
        page = re.sub(r"データ取得日: \d{4}年\d{1,2}月\d{1,2}日(?: [\d:]+)?",
                      f"データ取得日: {now.year}年{now.month}月{now.day}日 {now:%H:%M}", page)
    with open(page_path, "w", encoding="utf-8") as file:
        file.write(page)
    with open(cache_path, "w", encoding="utf-8") as file:
        json.dump(cache, file, ensure_ascii=False, indent=1)
    with open(os.path.join(ROOT, "data/last_update.json"), "w", encoding="utf-8") as file:
        json.dump({"run_at": now.isoformat(), "updated": sorted(updated), "failed": sorted(failed)}, file, ensure_ascii=False, indent=1)
    if failed:
        raise SystemExit(f"Failed stocks: {failed}")


if __name__ == "__main__":
    main()

