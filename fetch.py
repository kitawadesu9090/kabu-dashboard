#!/usr/bin/env python3
"""
株式情報収集スクリプト
- Yahoo!ファイナンス決算速報RSSからニュースを取得
- J-Quants API (v2) から決算発表予定を取得
- docs/data.json に書き出す
"""

import json
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

# --- 設定 ---
RSS_URL = "https://finance.yahoo.co.jp/news/settlement?output=rss"
JQUANTS_BASE = "https://api.jquants.com/v2"
JQUANTS_API_KEY = os.environ.get("JQUANTS_API_KEY", "")
JST = timezone(timedelta(hours=9))
OUTPUT_PATH = Path(__file__).resolve().parent / "docs" / "data.json"

# --- ニュース種別判定 ---
TYPE_RULES = [
    ("up",   ["上方修正", "上振れ", "増額", "増配", "引き上げ"]),
    ("down", ["下方修正", "下振れ", "減額", "減配", "引き下げ"]),
    ("div",  ["配当", "増配", "復配", "記念配", "特別配当"]),
    ("earn", ["決算", "営業利益", "経常利益", "純利益", "売上高", "業績"]),
]


def classify(title: str) -> str:
    """タイトルからニュース種別を判定する。優先度: up > down > div > earn"""
    t = title
    for type_key, keywords in TYPE_RULES:
        for kw in keywords:
            if kw in t:
                return type_key
    return "earn"


def extract_code(title: str) -> str:
    """タイトルから4桁の銘柄コードを正規表現で抽出する"""
    m = re.search(r"[（(〔\[【<](\d{4})[）)〕\]】>]", title)
    if m:
        return m.group(1)
    m = re.search(r"(?<!\d)(\d{4})(?!\d)", title)
    if m:
        return m.group(1)
    return ""


def clean_xml(text: str) -> str:
    """XMLパースエラー対策: 不正な文字やエンティティを修正"""
    # XML 1.0 で許可されない制御文字を除去
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', text)
    # エスケープされていない & を &amp; に変換
    text = re.sub(r'&(?!amp;|lt;|gt;|quot;|apos;|#\d+;|#x[0-9a-fA-F]+;)', '&amp;', text)
    return text


# --- Yahoo! ファイナンス RSS 取得 ---
def fetch_yahoo_rss() -> list[dict]:
    """Yahoo!ファイナンス決算速報RSSからニュースを取得"""
    items = []
    try:
        resp = requests.get(RSS_URL, timeout=30, headers={
            "User-Agent": "kabu-dashboard/1.0"
        })
        resp.raise_for_status()

        content = resp.content
        try:
            root = ET.fromstring(content)
        except ET.ParseError:
            # XMLクリーンアップしてリトライ
            text = content.decode("utf-8", errors="replace")
            cleaned = clean_xml(text)
            try:
                root = ET.fromstring(cleaned.encode("utf-8"))
            except ET.ParseError as e2:
                print(f"[Yahoo RSS] XMLパースエラー（クリーンアップ後も失敗）: {e2}")
                # 最終手段: regex でアイテムを抽出
                return parse_rss_with_regex(text)

        for item in root.iter("item"):
            title = item.findtext("title", "")
            link  = item.findtext("link", "")
            pub   = item.findtext("pubDate", "")
            desc  = item.findtext("description", "")

            news_type = classify(title)
            code = extract_code(title)

            items.append({
                "title": title,
                "link":  link,
                "type":  news_type,
                "code":  code,
                "pub":   pub,
                "desc":  desc[:200] if desc else "",
                "source": "yahoo"
            })
        print(f"[Yahoo RSS] {len(items)} 件取得")
    except Exception as e:
        print(f"[Yahoo RSS] 取得エラー: {e}")
    return items


def parse_rss_with_regex(text: str) -> list[dict]:
    """XMLパース失敗時のフォールバック: 正規表現でRSSアイテムを抽出"""
    items = []
    # <item>...</item> ブロックを抽出
    pattern = re.compile(r'<item>(.*?)</item>', re.DOTALL)
    for match in pattern.finditer(text):
        block = match.group(1)
        title = _extract_tag(block, "title")
        link  = _extract_tag(block, "link")
        pub   = _extract_tag(block, "pubDate")
        desc  = _extract_tag(block, "description")

        news_type = classify(title)
        code = extract_code(title)

        items.append({
            "title": title,
            "link":  link,
            "type":  news_type,
            "code":  code,
            "pub":   pub,
            "desc":  desc[:200] if desc else "",
            "source": "yahoo"
        })
    print(f"[Yahoo RSS] {len(items)} 件取得 (regexフォールバック)")
    return items


def _extract_tag(block: str, tag: str) -> str:
    """XMLブロックからタグの内容を正規表現で抽出"""
    m = re.search(rf'<{tag}[^>]*>(.*?)</{tag}>', block, re.DOTALL)
    if m:
        # CDATA除去
        val = m.group(1).strip()
        val = re.sub(r'<!\[CDATA\[(.*?)\]\]>', r'\1', val, flags=re.DOTALL)
        return val
    return ""


# --- J-Quants 決算発表予定取得 ---
def fetch_jquants_schedule() -> list[dict]:
    """J-Quants API v2 から決算発表予定を取得 (x-api-key 認証)"""
    schedule = []

    if not JQUANTS_API_KEY:
        print("[J-Quants] APIキー未設定のためスキップ")
        return schedule

    today = datetime.now(JST).strftime("%Y-%m-%d")

    try:
        resp = requests.get(
            f"{JQUANTS_BASE}/fins/announcement",
            headers={"x-api-key": JQUANTS_API_KEY},
            params={"date": today},
            timeout=30,
        )
        if resp.status_code == 200:
            data = resp.json()
            schedule = data.get("announcement", [])
            print(f"[J-Quants] 決算予定 {len(schedule)} 件取得")
        else:
            print(f"[J-Quants] ステータス {resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        print(f"[J-Quants] 取得エラー: {e}")
    return schedule


# --- メイン処理 ---
def main():
    print("=== 株式データ取得開始 ===")
    now = datetime.now(JST)

    # Yahoo RSS
    items = fetch_yahoo_rss()

    # J-Quants
    schedule = fetch_jquants_schedule()

    # 件数カウント
    count = {"up": 0, "down": 0, "div": 0, "earn": 0}
    for item in items:
        t = item.get("type", "earn")
        if t in count:
            count[t] += 1

    # data.json 書き出し
    output = {
        "updated": now.strftime("%Y/%m/%d %H:%M"),
        "date": now.strftime("%Y-%m-%d"),
        "count": count,
        "items": items,
        "schedule": schedule,
    }
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"=== 完了: {len(items)} 件のニュース, {len(schedule)} 件の決算予定 ===")
    print(f"=== 出力: {OUTPUT_PATH} ===")


if __name__ == "__main__":
    main()
