#!/usr/bin/env python3
"""Refresh quotes independently of the manually verified dividend/history basis."""
import concurrent.futures
import html
import json
import math
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
with open(os.path.join(ROOT, "data/dividend_adjustments.json"), encoding="utf-8") as file:
    DIVIDEND_ADJUSTMENTS = json.load(file)
with open(os.path.join(ROOT, "data/dividend_reviews.json"), encoding="utf-8") as file:
    DIVIDEND_REVIEWS = json.load(file)


def rounded(value):
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def fetch(url):
    for attempt in range(3):
        try:
            with urlopen(Request(url, headers=UA), timeout=20) as response:
                return response.read().decode("utf-8")
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            retryable = not isinstance(exc, HTTPError) or exc.code == 429 or exc.code >= 500
            if attempt == 2 or not retryable:
                raise OSError(f"{url}: {exc}") from exc
            time.sleep(2 ** attempt)


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


def parse_yahoo(raw):
    fields = [(plain(dt), plain(dd)) for dt, dd in re.findall(
        r'<dl\b[^>]*class="_DataListItem[^>]*>\s*<dt\b[^>]*>(.*?)</dt>\s*<dd\b[^>]*>(.*?)</dd>\s*</dl>', raw, re.S)]
    def value(label):
        text = next(dd for dt, dd in fields if dt.startswith(label))
        return text, Decimal(re.search(r"[\d,]+(?:\.\d+)?", text)[0].replace(",", ""))
    _, shares = value("発行済株式数")
    dividend_text, dividend = value("1株配当")
    dividend_period = re.search(r"\((\d{4}/\d{2})\)", dividend_text)[1]
    cap_text, cap_million = value("時価総額")
    board = re.search(r'class="[^"]*\b_CommonPriceBoard__price_\w+[^"]*">.*?class="_StyledNumber__value[^>]*>([\d,.]+)</span>', raw, re.S)
    price = Decimal(board[1].replace(",", ""))
    stamp = re.search(r"\(([^()]+)\)", cap_text)[1]
    now = datetime.now(JST)
    if re.fullmatch(r"\d{1,2}:\d{2}", stamp):
        hour, minute = map(int, stamp.split(":"))
        quote_at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    elif re.fullmatch(r"\d{1,2}/\d{1,2}", stamp):
        month, day = map(int, stamp.split("/"))
        year = now.year if (month, day) <= (now.month, now.day) else now.year - 1
        quote_at = datetime(year, month, day, 15, 30, tzinfo=JST)
    else:
        raise ValueError(f"Yahoo quote date missing: {stamp}")
    cap = price * shares / Decimal(100000000)
    if abs(cap - cap_million / 100) > Decimal("0.02"):
        raise ValueError("Yahoo price, shares and market cap do not agree")
    return {"price": float(price), "div": float(dividend), "div_period": dividend_period, "shares": int(shares),
            "cap": rounded(cap), "quote_at": quote_at}


def calculation_dividend(code, reported, period):
    review = DIVIDEND_REVIEWS.get(code)
    # Recheck every stock when the forecast or fiscal period changes, including
    # stocks that currently have no one-off dividend. Quote-only updates continue.
    if not review or review["period"] != period or abs(
            Decimal(str(reported)) - Decimal(str(review["reported_div"]))) >= Decimal("0.01"):
        raise ValueError(f"Dividend basis needs review: {code} {period} forecast={reported}")
    adjustment = DIVIDEND_ADJUSTMENTS.get(code)
    if not adjustment:
        return reported, ""
    if adjustment["period"] != period or adjustment["calculation_div"] != review["calculation_div"]:
        raise ValueError(f"Dividend adjustment and review disagree: {code}")
    # A revised forecast needs a fresh check of its nonrecurring component.
    if abs(Decimal(str(reported)) - Decimal(str(adjustment["reported_div"]))) >= Decimal("0.01"):
        raise ValueError(f"Dividend adjustment needs review: {code} {period} forecast={reported}")
    return adjustment["calculation_div"], adjustment["note"]


def parse_chart(raw, code, previous):
    chart = json.loads(raw)["chart"]
    if chart.get("error") or not chart.get("result"):
        raise ValueError(f"Yahoo chart error: {chart.get('error')}")
    result = chart["result"][0]
    meta = result["meta"]
    if meta.get("symbol") != f"{code}.T" or meta.get("currency") != "JPY":
        raise ValueError("Yahoo chart symbol/currency mismatch")
    # A new split invalidates the cached per-share dividend and share count.
    old_stamp = datetime.fromisoformat(previous["quoteAt"]).timestamp()
    for split in result.get("events", {}).get("splits", {}).values():
        if split["date"] > old_stamp:
            raise ValueError("New stock split: dividend/share basis needs review")
    return {"price": float(meta["regularMarketPrice"]),
            "quote_at": datetime.fromtimestamp(meta["regularMarketTime"], JST),
            "shares": previous["shares"], "div": None, "div_period": None}


def get_stock(code, previous):
    warnings = []
    try:
        yahoo = parse_yahoo(fetch(f"https://finance.yahoo.co.jp/quote/{code}.T"))
        source = "Yahoo!ファイナンス"
        shares_at = yahoo["quote_at"].isoformat()
    except Exception as exc:
        warnings.append(str(exc))
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{code}.T?range=1mo&interval=1d&events=splits"
        yahoo = parse_chart(fetch(url), code, previous)
        source = "Yahoo Finance chart API"
        shares_at = previous.get("sharesAt", previous["quoteAt"])
        warnings.append("配当予想・株式数は前回確認値を使用")
    price, quote_at = yahoo["price"], yahoo["quote_at"]
    if not math.isfinite(price) or price <= 0:
        raise ValueError("Invalid quote price")
    if quote_at < datetime.fromisoformat(previous["quoteAt"]):
        raise ValueError("Quote is older than the displayed quote")
    if quote_at > datetime.now(JST) + timedelta(minutes=5):
        raise ValueError("Quote date is in the future")
    if not .55 < yahoo["shares"] / previous["shares"] < 1.8:
        raise ValueError("Share count changed substantially: check split/dividend basis")
    review = DIVIDEND_REVIEWS[code]
    # A forecast revision never replaces the reviewed ordinary dividend silently.
    # Still refresh its price, with the retained calculation basis clearly labelled.
    needs_review = False
    if yahoo["div"] is not None:
        try:
            calculation_dividend(code, yahoo["div"], yahoo["div_period"])
        except ValueError as exc:
            warnings.append(str(exc))
            needs_review = True
    dividend = review["calculation_div"]
    current = rounded(Decimal(str(dividend)) / Decimal(str(price)) * 100)
    if not 0.1 <= current <= 15:
        raise ValueError(f"Unexpected current yield: {current}")
    if len(previous["hist"]) != 10:
        raise ValueError("Verified ten-year history missing")
    data = previous.copy()
    data.update({"cy": current, "price": price, "cap": rounded(price * yahoo["shares"] / 100000000),
                 "shares": yahoo["shares"], "sharesAt": shares_at, "quoteAt": quote_at.isoformat(),
                 "quoteSource": source, "div": dividend, "divSource": review["source"],
                 "divPeriod": review["period"], "divReviewAt": review["reviewed_at"],
                 "divReviewNote": review["note"], "divNeedsReview": needs_review,
                 "divForecastCheckedAt": datetime.now(JST).isoformat() if yahoo["div"] is not None else previous.get("divForecastCheckedAt"),
                 "updateStatus": "updated", "updateWarning": " / ".join(warnings)})
    if needs_review:
        data.update({"observedDiv": yahoo["div"], "observedDivPeriod": yahoo["div_period"]})
    else:
        data.pop("observedDiv", None)
        data.pop("observedDivPeriod", None)
    adjustment = DIVIDEND_ADJUSTMENTS.get(code)
    if adjustment:
        data.update({"reportedDiv": review["reported_div"], "divNote": adjustment["note"]})
    data.pop("ratioDisplay", None)
    entry = {"div": dividend, "reported_div": review["reported_div"], "cap": data["cap"],
             "shares": data["shares"], "shares_at": shares_at, "div_source": review["source"],
             "div_period": review["period"], "div_review_at": review["reviewed_at"],
             "div_review_note": review["note"], "div_needs_review": needs_review,
             "asof": datetime.now(JST).strftime("%Y-%m-%d"), "quote_at": data["quoteAt"],
             "quote_source": source, "price": price, "update_warning": data["updateWarning"]}
    return data, entry


def main():
    page_path = os.path.join(ROOT, "d1/index.html")
    cache_path = os.path.join(ROOT, "data/dividends.json")
    with open(page_path, encoding="utf-8") as file:
        page = file.read()
    cache = {}
    if os.path.exists(cache_path):
        with open(cache_path, encoding="utf-8") as file:
            cache = json.load(file)
    row_texts = re.findall(r'^  \{code:"\d{4}"[^\n]*\},$', page, re.M)
    previous = [json.loads(re.sub(r'([{,])\s*([A-Za-z]\w*)\s*:', r'\1"\2":',
                               row.strip().rstrip(','))) for row in row_texts]
    rows = [row["code"] for row in previous]
    if not rows or len(rows) != len(set(rows)):
        raise ValueError("Missing or duplicate stock codes")
    old_data = {row["code"]: row for row in previous}
    updated, failed, errors, needs_review = [], [], {}, []
    print(f"Refreshing {len(rows)} stocks", flush=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        tasks = {executor.submit(get_stock, code, old_data[code]): code for code in rows}
        for task in concurrent.futures.as_completed(tasks):
            code = tasks[task]
            try:
                data, entry = task.result()
                pattern = r'^  \{code:"' + code + r'"[^\n]*\},$'
                old_row = re.search(pattern, page, re.M)[0]
                new_row = '  {' + ', '.join(key + ':' + json.dumps(value, ensure_ascii=False, separators=(',', ':'))
                                           for key, value in data.items()) + '},'
                page = re.sub(pattern, lambda m: new_row, page, flags=re.M)
                cache[code] = entry
                updated.append(code)
                if data["divNeedsReview"]:
                    needs_review.append(code)
                    print(f"::warning::Dividend basis needs review for {code}; retained reviewed dividend", flush=True)
                print(f"[OK] {code} yield={data['cy']}% cap={data['cap']} history={data['historyPeriod']} quote={data['quoteAt']}", flush=True)
            except Exception as exc:
                failed.append(code)
                errors[code] = str(exc)
                data = old_data[code].copy()
                data.update({"updateStatus": "failed", "updateWarning": str(exc)})
                new_row = '  {' + ', '.join(key + ':' + json.dumps(value, ensure_ascii=False, separators=(',', ':'))
                                           for key, value in data.items()) + '},'
                page = re.sub(r'^  \{code:"' + code + r'"[^\n]*\},$', lambda m: new_row, page, flags=re.M)
                print(f"[FAIL] {code}: {exc}", flush=True)
    now = datetime.now(JST)
    if updated:
        page = re.sub(r"データ取得日: \d{4}年\d{1,2}月\d{1,2}日(?: [\d:]+)?",
                      f"データ取得日: {now.year}年{now.month}月{now.day}日 {now:%H:%M}", page)
    summary = {"run_at": now.isoformat(), "updated": sorted(updated), "failed": sorted(failed),
               "errors": errors, "dividend_needs_review": sorted(needs_review),
               "history": "Retained verified ten completed fiscal years; not fetched on daily quote updates"}
    summary_path = os.path.join(ROOT, "data/last_update.json")
    with open(summary_path, "w", encoding="utf-8") as file:
        json.dump(summary, file, ensure_ascii=False, indent=1)
    if not updated:
        raise SystemExit(f"No quotes updated: {errors}")
    label = f"自動更新: {now:%Y/%m/%d %H:%M} JST ｜ 株価取得 {len(updated)}/{len(rows)}銘柄"
    if failed:
        label += " ｜ 前回値維持: " + "・".join(sorted(failed))
    if needs_review:
        label += " ｜ 配当要確認: " + "・".join(sorted(needs_review))
    page = re.sub(r'(<div id="updateStatus"[^>]*>).*?(</div>)',
                  lambda m: m[1] + html.escape(label) + m[2], page, flags=re.S)
    # Write complete files only after quote validation; a failed stock retains its own timestamp.
    for path, content in [(page_path, page), (cache_path, json.dumps(cache, ensure_ascii=False, indent=1) + "\n")]:
        temp = path + ".tmp"
        with open(temp, "w", encoding="utf-8") as file:
            file.write(content)
        os.replace(temp, path)
    print(label, flush=True)


if __name__ == "__main__":
    main()

