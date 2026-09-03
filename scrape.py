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
 4. 日付・時刻・値の行を抽出。未来の時刻や、公開ラグで反映待ちの可能性が高い
    直近の欠測は保留し、それ以外の欠測は値を空欄として扱う
 5. data/{station}/{station}-{週の月曜日の日付}.csv にマージする。
    新規の日時は追加し、既存が空欄で今回実測値が取れた場合は上書きして埋める
    (既にある実測値を上書きすることはない)
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
# サイト側の公開ラグを考慮し、直近この時間以内の欠測は「反映待ち」とみなして記録を保留する
PUBLISH_LAG_BUFFER = datetime.timedelta(minutes=20)

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

        now = datetime.datetime.now(JST)
        if dt > now:
            continue  # まだ観測時刻に達していない未来の時刻は記録しない
        if value is None and dt > now - PUBLISH_LAG_BUFFER:
            # サイト側の公開ラグで、まだ反映されていないだけの可能性が高い時間帯。
            # 本当に欠測かどうかまだ判断できないので、今回は記録せず次回に持ち越す。
            continue

        rows.append((dt, value))

    return rows

    return rows


def load_existing_rows(csv_path: str):
    """既存CSVを {日時文字列: 値文字列(空欄含む)} の辞書として読み込む。挿入順を維持する。"""
    existing = {}
    if os.path.exists(csv_path):
        with open(csv_path, newline="", encoding="utf-8-sig") as f:
            reader = csv.reader(f)
            next(reader, None)  # header
            for row in reader:
                if row:
                    existing[row[0]] = row[1] if len(row) > 1 else ""
    return existing


def merge_rows(station_name: str, value_col: str, rows):
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
        existing = load_existing_rows(csv_path)
        before_count = len(existing)
        filled_count = 0
        added_count = 0

        for dt, value in week_rows:
            dt_key = dt.strftime("%Y-%m-%d %H:%M")
            new_value_str = "" if value is None else str(value)

            if dt_key not in existing:
                existing[dt_key] = new_value_str
                added_count += 1
            elif existing[dt_key] == "" and new_value_str != "":
                # 既存が空欄で、今回は実測値が取れた → 上書きして埋める
                existing[dt_key] = new_value_str
                filled_count += 1
            # それ以外(既存に既に実測値がある場合)は上書きしない

        if not existing:
            continue

        with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow(["datetime", value_col])
            for dt_key in sorted(existing.keys()):
                writer.writerow([dt_key, existing[dt_key]])

        if added_count or filled_count or before_count == 0:
            print(
                f"[{station_name}] {csv_path}: 新規{added_count}件追加"
                f"{f', 空欄→実測値に更新{filled_count}件' if filled_count else ''}"
            )


def main():
    had_error = False
    for station in STATIONS:
        name = station["name"]
        try:
            dat_url = fetch_dat_url(station["url"])
            rows = fetch_dat_rows(dat_url)
            merge_rows(name, station["value_col"], rows)
        except Exception as e:
            had_error = True
            print(f"[{name}] エラー: {e}", file=sys.stderr)
            # 1地点失敗しても他の地点は続行する

    if had_error:
        sys.exit(1)


if __name__ == "__main__":
    main()
