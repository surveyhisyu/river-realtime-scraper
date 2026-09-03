"""
国土交通省 水文水質データベース(www1.river.go.jp)から、
複数の観測所(水位・雨量)の10分値データを取得し、
観測所ごと・週単位(月曜始まり)のCSVファイルに追記(重複排除)するスクリプト。

対象ページはいずれも静的HTMLで、ページ内の「CSVダウンロード」画像リンクの先に
直接データファイル(.dat, Shift-JIS)があるため、Playwright等のブラウザ操作は不要。
requestsのみで完結する。水位(DspWaterData.exe)・雨量(DspRainData.exe)のどちらも
同じ.dat形式(日付,時刻,値,フラグ)なので、同じロジックで処理できる。

流れ(観測所ごとに):
 1. url(観測所の一覧ページ)を取得
 2. ページ内から .dat へのダウンロードリンクを抽出
 3. .dat を取得し、Shift-JISでデコード
 4. 日付・時刻・値の行を抽出(未観測 "-" の行は除外)
 5. data/{station}/{station}-{週の月曜日の日付}.csv に追記。既存の日時と重複する行は追加しない
    (例: 2026/8/31(月)〜9/6(日)のデータは data/nishisato/nishisato-2026-08-31.csv にまとまる)
"""

import csv
import datetime
import os
import re
import sys
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

JST = ZoneInfo("Asia/Tokyo")
DATA_DIR = "data"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; river-realtime-scraper/1.0)"
}

# 観測所一覧
# name: フォルダ名・ファイル名に使う識別子(観測所名の漢字表記)
# url: 観測所の一覧ページURL
# value_col: CSVの値列の見出し名
STATIONS = [
    {
        "name": "西里橋",
        "url": "https://www1.river.go.jp/cgi-bin/DspWaterData.exe?KIND=9&ID=304081284418020",
        "value_col": "water_level_m",
    },
    {
        "name": "双六岳",
        "url": "https://www1.river.go.jp/cgi-bin/DspRainData.exe?KIND=9&ID=104031284420040",
        "value_col": "rain_mm_10min",
    },
    {
        "name": "左俣谷",
        "url": "https://www1.river.go.jp/cgi-bin/DspRainData.exe?KIND=9&ID=104081284418010",
        "value_col": "rain_mm_10min",
    },
    {
        "name": "白出沢",
        "url": "https://www1.river.go.jp/cgi-bin/DspRainData.exe?KIND=9&ID=104081284408070",
        "value_col": "rain_mm_10min",
    },
    {
        "name": "西穂",
        "url": "https://www1.river.go.jp/cgi-bin/DspRainData.exe?KIND=9&ID=104031284417040",
        "value_col": "rain_mm_10min",
    },
    {
        "name": "中尾",
        "url": "https://www1.river.go.jp/cgi-bin/DspRainData.exe?KIND=9&ID=104081284418020",
        "value_col": "rain_mm_10min",
    },
    {
        "name": "栃尾",
        "url": "https://www1.river.go.jp/cgi-bin/DspRainData.exe?KIND=9&ID=104081284418030",
        "value_col": "rain_mm_10min",
    },
    {
        "name": "大棚",
        "url": "https://www1.river.go.jp/cgi-bin/DspRainData.exe?KIND=9&ID=104081284418140",
        "value_col": "rain_mm_10min",
    },
    {
        "name": "平湯",
        "url": "https://www1.river.go.jp/cgi-bin/DspRainData.exe?KIND=9&ID=104081284418110",
        "value_col": "rain_mm_10min",
    },
    {
        "name": "下佐谷",
        "url": "https://www1.river.go.jp/cgi-bin/DspRainData.exe?KIND=9&ID=104081284408060",
        "value_col": "rain_mm_10min",
    },
    {
        "name": "金木戸",
        "url": "https://www1.river.go.jp/cgi-bin/DspRainData.exe?KIND=9&ID=104081284418160",
        "value_col": "rain_mm_10min",
    },
    {
        "name": "本郷",
        "url": "https://www1.river.go.jp/cgi-bin/DspRainData.exe?KIND=9&ID=104081284408080",
        "value_col": "rain_mm_10min",
    },
]


def fetch_dat_url(source_url: str) -> str:
    """観測所ページから.datダウンロードリンクの絶対URLを取得する"""
    res = requests.get(source_url, headers=HEADERS, timeout=30)
    res.raise_for_status()
    # このページ自体もShift-JISで配信されている
    res.encoding = "shift_jis"
    soup = BeautifulSoup(res.text, "html.parser")

    dat_link = None
    for a in soup.find_all("a", href=True):
        if a["href"].endswith(".dat"):
            dat_link = a["href"]
            break

    if dat_link is None:
        # 念のためHTML全文から正規表現でも探す
        m = re.search(r'href="([^"]+\.dat)"', res.text)
        if m:
            dat_link = m.group(1)

    if dat_link is None:
        raise RuntimeError("ページ内に.datへのダウンロードリンクが見つかりませんでした")

    return requests.compat.urljoin(source_url, dat_link)


def fetch_dat_rows(dat_url: str):
    """.datファイルを取得し、(datetime, value)のリストを返す"""
    res = requests.get(dat_url, headers=HEADERS, timeout=30)
    res.raise_for_status()
    text = res.content.decode("shift_jis", errors="replace")

    rows = []
    for line in text.splitlines():
        parts = line.split(",")
        if len(parts) < 3:
            continue
        date_str = parts[0].strip()
        if not re.match(r"^\d{4}/\d{2}/\d{2}$", date_str):
            continue  # ヘッダ・コメント行はスキップ

        time_str = parts[1].strip()
        value_str = parts[2].strip()

        # "24:00" のような表記に対応(翌日の00:00として扱う)
        try:
            hh, mm = time_str.split(":")
            hh = int(hh)
            extra_day = 0
            if hh == 24:
                hh = 0
                extra_day = 1
            dt = datetime.datetime.strptime(date_str, "%Y/%m/%d").replace(tzinfo=JST)
            dt = dt + datetime.timedelta(days=extra_day, hours=hh, minutes=int(mm))
        except ValueError:
            continue

        # 欠測・閉局は行自体は残し、値だけ空欄(None)にする
        value = None
        if value_str not in ("-", "", "*", "$"):
            try:
                value = float(value_str)
            except ValueError:
                value = None
            if value is not None and value <= -90:
                value = None  # "-99.999" 等、閉局・欠測を示す特殊な数値コード

        rows.append((dt, value))

    return rows

    return rows


def load_existing_datetimes(csv_path: str):
    existing = set()
    if os.path.exists(csv_path):
        with open(csv_path, newline="", encoding="utf-8-sig") as f:
            reader = csv.reader(f)
            next(reader, None)  # header
            for row in reader:
                if row:
                    existing.add(row[0])
    return existing


def append_rows(station_name: str, value_col: str, rows):
    if not rows:
        print(f"[{station_name}] 新規データなし")
        return

    by_week = {}
    for dt, value in rows:
        # その日付が属する週の月曜日(週の開始日)を求める
        week_start = (dt - datetime.timedelta(days=dt.weekday())).date()
        week_key = week_start.strftime("%Y-%m-%d")
        by_week.setdefault(week_key, []).append((dt, value))

    os.makedirs(os.path.join(DATA_DIR, station_name), exist_ok=True)

    for week_key, week_rows in by_week.items():
        csv_path = os.path.join(DATA_DIR, station_name, f"{station_name}-{week_key}.csv")
        existing = load_existing_datetimes(csv_path)
        is_new_file = not os.path.exists(csv_path)

        new_rows = [
            (dt, value)
            for dt, value in week_rows
            if dt.strftime("%Y-%m-%d %H:%M") not in existing
        ]

        if not new_rows and not is_new_file:
            continue

        with open(csv_path, "a", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            if is_new_file:
                writer.writerow(["datetime", value_col])
            for dt, value in sorted(new_rows, key=lambda r: r[0]):
                writer.writerow([dt.strftime("%Y-%m-%d %H:%M"), "" if value is None else value])

        print(f"[{station_name}] {csv_path}: {len(new_rows)}件追加")


def main():
    had_error = False
    for station in STATIONS:
        name = station["name"]
        try:
            dat_url = fetch_dat_url(station["url"])
            rows = fetch_dat_rows(dat_url)
            append_rows(name, station["value_col"], rows)
        except Exception as e:
            had_error = True
            print(f"[{name}] エラー: {e}", file=sys.stderr)
            # 1地点失敗しても他の地点は続行する

    if had_error:
        sys.exit(1)


if __name__ == "__main__":
    main()
