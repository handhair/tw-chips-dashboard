# 抓台股大盤的每日籌碼資料，存到 data/chips_data.json 給儀表板網頁用
# 證交所：加權指數、成交金額、三大法人買賣超、融資餘額
# 期交所：台指期三大法人未平倉、小台散戶多空比、選擇權 Put/Call Ratio
#
# 用法：
#   python chips.py                    # 只補抓 data/chips_data.json 裡還沒有的交易日
#   python chips.py --full 2026-02-01  # 從指定日期整段重抓
#
# 只用 Python 標準函式庫，不用安裝套件。

import csv
import io
import json
import ssl
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).parent
CACHE = HERE / "chips_cache"
OUTPUT = HERE / "data" / "chips_data.json"
DEFAULT_START = date(2026, 2, 1)

FMTQIK_URL = "https://www.twse.com.tw/rwd/zh/afterTrading/FMTQIK?date={month}&response=json"
BFI82U_URL = "https://www.twse.com.tw/rwd/zh/fund/BFI82U?type=day&dayDate={day}&response=json"
MARGIN_URL = "https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN?date={day}&selectType=MS&response=json"
TAIFEX_INST_URL = "https://www.taifex.com.tw/cht/3/futContractsDateDown"
TAIFEX_DAILY_URL = "https://www.taifex.com.tw/cht/3/futDataDown"
TAIFEX_PCR_URL = "https://www.taifex.com.tw/cht/3/pcRatioDown"

HEADERS = {"User-Agent": "Mozilla/5.0 tw-chips-dashboard"}
TWSE_PAUSE = 2.5  # 證交所連續查詢太快會被暫時封鎖

# 證交所的憑證少了 Subject Key Identifier，Python 3.13+ 的嚴格模式會拒絕連線。
# 這裡照樣驗證憑證，只是關掉嚴格模式。
SSL_CONTEXT = ssl.create_default_context()
SSL_CONTEXT.verify_flags &= ~ssl.VERIFY_X509_STRICT


def to_number(text):
    return float(text.replace(",", ""))


def twse(url):
    time.sleep(TWSE_PAUSE)
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30, context=SSL_CONTEXT) as resp:
        return json.load(resp)


def taifex_csv(url, params):
    req = urllib.request.Request(url, data=urllib.parse.urlencode(params).encode(), headers=HEADERS)
    with urllib.request.urlopen(req, timeout=60, context=SSL_CONTEXT) as resp:
        text = resp.read().decode("big5", "replace")
    return list(csv.reader(io.StringIO(text)))[1:]


def cached(name, fetch, complete):
    """有快取就讀快取；沒有就抓，而且只有資料完整時才存（避免把「還沒公布」存起來）"""
    path = CACHE / name
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    data = fetch()
    if complete(data):
        CACHE.mkdir(exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return data


def twse_ok(data):
    return data.get("stat") == "OK"


def months(start, end):
    m = start.replace(day=1)
    while m <= end:
        yield m
        m = (m.replace(day=28) + timedelta(days=4)).replace(day=1)


def month_end(m):
    return (m.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)


def roc_to_iso(text):
    y, m, d = text.split("/")
    return f"{int(y) + 1911}-{m}-{d}"


def taiex_and_volume(start, end):
    """每個交易日的收盤指數與成交金額（億元）"""
    days = {}
    for m in months(start, end):
        url = FMTQIK_URL.format(month=m.strftime("%Y%m01"))
        # 還沒結束的月份每次都重抓
        finished = month_end(m) < date.today()
        data = cached(f"fmtqik_{m:%Y%m}.json", lambda: twse(url), lambda d: finished and twse_ok(d))
        for row in data.get("data", []):
            day = roc_to_iso(row[0])
            if start.isoformat() <= day <= end.isoformat():
                days[day] = {"taiex": to_number(row[4]), "turnover": round(to_number(row[2]) / 1e8)}
    return days


def institutions(day):
    url = BFI82U_URL.format(day=f"{day:%Y%m%d}")
    data = cached(f"bfi82u_{day:%Y%m%d}.json", lambda: twse(url), twse_ok)
    if not twse_ok(data):
        return {}
    net = {row[0]: to_number(row[3]) / 1e8 for row in data["data"]}
    return {
        "foreign_spot": round(net["外資及陸資(不含外資自營商)"], 1),
        "trust_spot": round(net["投信"], 1),
        "dealer_spot": round(net["自營商(自行買賣)"] + net["自營商(避險)"], 1),
    }


def margin(day):
    url = MARGIN_URL.format(day=f"{day:%Y%m%d}")
    data = cached(f"margin_{day:%Y%m%d}.json", lambda: twse(url), twse_ok)
    if not twse_ok(data):
        return None
    rows = {row[0]: row for row in data["tables"][0]["data"]}
    # 「今日餘額」單位是仟元，換成億元
    return round(to_number(rows["融資金額(仟元)"][5]) / 1e5, 1)


def taifex_month(kind, m, start, end):
    """期交所一次抓一個月內需要的區間，避免查詢區間太長被拒絕"""
    first, last = max(m, start), min(month_end(m), end)

    def fetch(last):
        span = {"queryStartDate": f"{first:%Y/%m/%d}", "queryEndDate": f"{last:%Y/%m/%d}"}
        if kind == "txf":
            return taifex_csv(TAIFEX_INST_URL, {**span, "commodityId": "TXF"})
        if kind == "mxf":
            return taifex_csv(TAIFEX_INST_URL, {**span, "commodityId": "MXF"})
        if kind == "mtx":
            return taifex_csv(TAIFEX_DAILY_URL, {**span, "down_type": "1", "commodity_id": "MTX"})
        return taifex_csv(TAIFEX_PCR_URL, span)

    # 只有整個月份都在查詢範圍內、而且已經過完的月份才存快取
    whole = first == m and last == month_end(m) and last < date.today()
    rows = cached(f"taifex_{kind}_{m:%Y%m}.json", lambda: fetch(last), lambda rows: whole and valid_rows(kind, rows))
    # 結束日那天的資料還沒公布時，期交所會整段回錯誤頁，所以往前縮一天再查（最多縮 5 天，遇到週末也夠）
    short = last
    for _ in range(5):
        if valid_rows(kind, rows) or short <= first:
            break
        short -= timedelta(days=1)
        rows = fetch(short)
    if not valid_rows(kind, rows) and kind in OPENAPI:
        # 下載頁還是拿不到時，改用 OpenAPI（只有最近一個交易日）
        print(f"期交所 {kind} 下載頁沒有資料，改用 OpenAPI", flush=True)
        lo, hi = f"{first:%Y/%m/%d}", f"{last:%Y/%m/%d}"
        rows = [r for r in openapi_rows(kind) if lo <= r[0] <= hi]
    return rows


def valid_rows(kind, rows):
    width = {"txf": 14, "mxf": 14, "mtx": 18, "pcr": 7}[kind]
    return any(len(r) >= width and r[0][:1].isdigit() for r in rows)


OPENAPI = {
    "txf": "MarketDataOfMajorInstitutionalTradersDetailsOfFuturesContractsBytheDate",
    "mxf": "MarketDataOfMajorInstitutionalTradersDetailsOfFuturesContractsBytheDate",
    "mtx": "DailyMarketReportFut",
}


def openapi_rows(kind):
    """把期交所 OpenAPI 的 JSON 轉成跟下載頁 CSV 一樣的欄位位置"""
    req = urllib.request.Request("https://openapi.taifex.com.tw/v1/" + OPENAPI[kind], headers={**HEADERS, "accept": "application/json"})
    with urllib.request.urlopen(req, timeout=60, context=SSL_CONTEXT) as resp:
        data = json.load(resp)
    day = lambda s: f"{s[:4]}/{s[4:6]}/{s[6:]}"
    if kind in ("txf", "mxf"):
        code = {"txf": "臺股期貨", "mxf": "小型臺指期貨"}[kind]
        return [[day(x["Date"]), code, x["Item"]] + [""] * 10 + [x["OpenInterest(Net)"]] for x in data if x.get("ContractCode") == code]
    rows = []
    for x in data:
        if x.get("Contract") == "MTX":
            row = [""] * 18
            row[0], row[1], row[2], row[11], row[17] = day(x["Date"]), "MTX", x["ContractMonth(Week)"], x["OpenInterest"], x["TradingSession"]
            rows.append(row)
    return rows


def futures(start, end):
    out = {}

    def slot(day):
        return out.setdefault(day.replace("/", "-"), {})

    for m in months(start, end):
        for row in taifex_month("txf", m, start, end):
            names = {"外資及陸資": "foreign_txf", "投信": "trust_txf", "自營商": "dealer_txf"}
            if len(row) > 13 and row[2] in names:
                slot(row[0])[names[row[2]]] = int(row[13])
        mxf_inst = {}
        for row in taifex_month("mxf", m, start, end):
            if len(row) > 13:
                mxf_inst[row[0]] = mxf_inst.get(row[0], 0) + int(row[13])
        mtx_oi = {}
        for row in taifex_month("mtx", m, start, end):
            # 只算一般交易時段；盤後時段的未沖銷欄位是「-」
            if len(row) > 17 and row[17] == "一般" and row[11].strip().isdigit():
                mtx_oi[row[0]] = mtx_oi.get(row[0], 0) + int(row[11])
        for day, inst_net in mxf_inst.items():
            if mtx_oi.get(day):
                # 散戶多空比 = 散戶淨未平倉 / 全市場未平倉；散戶淨部位 = -(三大法人淨部位)
                slot(day)["retail_ratio"] = round(-inst_net / mtx_oi[day] * 100, 2)
        for row in taifex_month("pcr", m, start, end):
            if len(row) > 6 and row[0][:1].isdigit():
                slot(row[0])["pcr_oi"] = float(row[6])
    return out


REQUIRED = ("taiex", "foreign_spot", "margin", "foreign_txf", "retail_ratio")


def complete(row):
    return all(row.get(k) is not None for k in REQUIRED)


def main():
    args = sys.argv[1:]
    existing = {}
    if "--full" in args:
        i = args.index("--full")
        start = date.fromisoformat(args[i + 1]) if len(args) > i + 1 else DEFAULT_START
    else:
        if OUTPUT.exists():
            existing = {r["date"]: r for r in json.loads(OUTPUT.read_text(encoding="utf-8"))}
        # 從第一筆不完整的日子開始補；全部完整就從最後一天的隔天開始
        incomplete = [d for d, r in existing.items() if not complete(r)]
        if incomplete:
            start = date.fromisoformat(min(incomplete))
        elif existing:
            start = date.fromisoformat(max(existing)) + timedelta(days=1)
        else:
            start = DEFAULT_START
    end = date.today()
    if start > end:
        print("資料已是最新")
        return

    print(f"抓取 {start} ~ {end}", flush=True)
    days = taiex_and_volume(start, end)
    fut = futures(start, end) if days else {}
    for n, day in enumerate(sorted(days), 1):
        d = date.fromisoformat(day)
        try:
            days[day].update(institutions(d))
            days[day]["margin"] = margin(d)
        except Exception as e:
            print(f"{day} 抓取失敗：{e}", flush=True)
        days[day].update(fut.get(day, {}))
        print(f"[{n}/{len(days)}] {day}{'' if complete(days[day]) else '（資料還沒公布齊）'}", flush=True)

    merged = {**existing, **{day: {"date": day, **row} for day, row in days.items()}}
    rows = [merged[d] for d in sorted(merged)]
    OUTPUT.parent.mkdir(exist_ok=True)
    OUTPUT.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"完成：共 {len(rows)} 個交易日，新增或更新 {len(days)} 天 → {OUTPUT.relative_to(HERE)}")


if __name__ == "__main__":
    main()
