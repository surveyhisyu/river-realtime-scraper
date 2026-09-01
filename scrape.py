"""
国土交通省 水文水質データベース(www1.river.go.jp)の
「リアルタイム10分水位一覧表」ページから、観測所の10分水位データを取得し、
月ごとのCSVファイルに追記(重複排除)するスクリプト。

対象ページは静的HTMLで、ページ内の「CSVダウンロード」画像リンクの先に
直接データファイル(.dat, Shift-JIS)があるため、Playwright等のブラウザ操作は不要。
requestsのみで完結する。

流れ:
 1. SOURCE_URL(観測所の一覧ページ)を取得
 2. ページ内から .dat へのダウンロードリンクを抽出
 3. .dat を取得し、Shift-JISでデコード
 4. 日付・時刻・水位(m)の行を抽出(未観測 "-" の行は除外)
 5. data/{station}-{週の月曜日の日付}.csv に追記。既存の日時と重複する行は追加しない
    (例: 2026/8/31(月)〜9/6(日)のデータは nishisato-2026-08-31.csv にまとまる)
"""

import csv
import datetime
import os
import re
import sys
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

# 観測所: 西里橋(ID=304081284418020)
SOURCE_URL = "https://www1.river.go.jp/cgi-bin/DspWaterData.exe?KIND=9&ID=304081284418020"
STATION_NAME = "nishisato"  # ファイル名等に使う識別子
DATA_DIR = "data"
JST = ZoneInfo("Asia/Tokyo")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; river-realtime-scraper/1.0)"
}


def fetch_dat_url() -> str:
    """観測所ページから.datダウンロードリンクの絶対URLを取得する"""
    res = requests.get(SOURCE_URL, headers=HEADERS, timeout=30)
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

    return requests.compat.urljoin(SOURCE_URL, dat_link)


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

        if value_str in ("-", "", "*", "$"):
            continue  # 未観測・欠測はスキップ

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

        try:
            value = float(value_str)
        except ValueError:
            continue

        rows.append((dt, value))

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


def append_rows(rows):
    if not rows:
        print("新規データなし")
        return

    by_week = {}
    for dt, value in rows:
        # その日付が属する週の月曜日(週の開始日)を求める
        week_start = (dt - datetime.timedelta(days=dt.weekday())).date()
        week_key = week_start.strftime("%Y-%m-%d")
        by_week.setdefault(week_key, []).append((dt, value))

    os.makedirs(DATA_DIR, exist_ok=True)

    for week_key, week_rows in by_week.items():
        csv_path = os.path.join(DATA_DIR, f"{STATION_NAME}-{week_key}.csv")
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
                writer.writerow(["datetime", "water_level_m"])
            for dt, value in sorted(new_rows):
                writer.writerow([dt.strftime("%Y-%m-%d %H:%M"), value])

        print(f"{csv_path}: {len(new_rows)}件追加")


def main():
    try:
        dat_url = fetch_dat_url()
        rows = fetch_dat_rows(dat_url)
        append_rows(rows)
    except Exception as e:
        print(f"エラー: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
