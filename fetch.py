#!/usr/bin/env python3
"""
株式情報収集スクリプト
- Yahoo!ファイナンス決算速報RSSからニュースを取得
- J-Quants API (v2) から財務サマリーを取得
- docs/data.json に書き出す
"""

import json
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from pathlib import Path
from html.parser import HTMLParser

import requests

# --- 設定 ---
RSS_URL = "https://finance.yahoo.co.jp/news/settlement?output=rss"
RSS_URL_ALT = "https://news.yahoo.co.jp/rss/categories/business.xml"
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

# 決算関連キーワード（フィルタ用）
SETTLEMENT_KEYWORDS = [
    "決算", "営業利益", "経常利益", "純利益", "売上高", "業績",
    "上方修正", "下方修正", "増配", "減配", "配当", "修正",
    "上振れ", "下振れ", "増額", "減額", "引き上げ", "引き下げ",
    "復配", "記念配", "特別配当", "増収", "減収", "増益", "減益",
]


def classify(title: str) -> str:
    """タイトルからニュース種別を判定する。優先度: up > down > div > earn"""
    t = title
    for type_key, keywords in TYPE_RULES:
        for kw in keywords:
            if kw in t:
                return type_key
    return "earn"


def is_settlement_news(title: str) -> bool:
    """タイトルが決算関連ニュースかどうか判定"""
    return any(kw in title for kw in SETTLEMENT_KEYWORDS)


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
    text = re.sub(r'[\x00-\x04\x0b\x0c\x0e-\x1f\x7f]', '', text)
    # エスケープされていない & を &amp; に変換
    text = re.sub(r'&(?!amp;|lt;|gt;|quot;|apos;|#\d+;|#x[0-9a-fA-F]+;)', '&amp;', text)
    # 不正なタグ属性内の引用符問題を軽減
    return text


def try_parse_xml(content: bytes) -> ET.Element | None:
    """複数の方法でXMLパースを試みる"""
    # 方法1: そのままパース
    try:
        return ET.fromstring(content)
    except ET.ParseError:
        pass

    # 方法2: UTF-8デコード + クリーンアップ
    text = content.decode("utf-8", errors="replace")
    cleaned = clean_xml(text)
    try:
        return ET.fromstring(cleaned.encode("utf-8"))
    except ET.ParseError:
        pass

    # 方法3: 問題の行を除去してパース
    lines = cleaned.split('\n')
    for attempt in range(5):
        try:
            return ET.fromstring('\n'.join(lines).encode("utf-8"))
        except ET.ParseError as e:
            # エラーメッセージから行番号を取得して除去
            err_match = re.search(r'line (\d+)', str(e))
            if err_match:
                bad_line = int(err_match.group(1)) - 1
                if 0 <= bad_line < len(lines):
                    print(f"[Yahoo RSS] 問題の行 {bad_line + 1} を除去: {lines[bad_line][:80]}")
                    lines[bad_line] = ''
                else:
                    break
            else:
                break

    return None


# --- Yahoo! ファイナンス RSS 取得 ---
def fetch_yahoo_rss() -> list[dict]:
    """Yahoo!ファイナンス決算速報RSSからニュースを取得"""
    items = []
    try:
        resp = requests.get(RSS_URL, timeout=30, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        })
        resp.raise_for_status()

        content = resp.content
        content_str = content.decode("utf-8", errors="replace")

        # デバッグ: レスポンスの先頭を出力
        print(f"[Yahoo RSS] レスポンスサイズ: {len(content)} bytes")
        print(f"[Yahoo RSS] Content-Type: {resp.headers.get('Content-Type', 'unknown')}")
        print(f"[Yahoo RSS] 先頭200文字: {content_str[:200]}")

        # HTMLが返ってきた場合はRSSではない
        if '<html' in content_str[:500].lower() or '<!doctype html' in content_str[:500].lower():
            print("[Yahoo RSS] HTMLレスポンスを検出 - RSSではありません")
            return items

        # XMLパース試行
        root = try_parse_xml(content)
        if root is not None:
            for item in root.iter("item"):
                title = item.findtext("title", "")
                link = item.findtext("link", "")
                pub = item.findtext("pubDate", "")
                desc = item.findtext("description", "")

                news_type = classify(title)
                code = extract_code(title)

                items.append({
                    "title": title,
                    "link": link,
                    "type": news_type,
                    "code": code,
                    "pub": pub,
                    "desc": desc[:200] if desc else "",
                    "source": "yahoo"
                })
            print(f"[Yahoo RSS] {len(items)} 件取得 (XMLパース)")
        else:
            # XMLパース完全失敗 → regex フォールバック
            print("[Yahoo RSS] XMLパース完全失敗 → regexフォールバック")
            items = parse_rss_with_regex(content_str)

    except Exception as e:
        print(f"[Yahoo RSS] 取得エラー: {e}")
    return items


def parse_rss_with_regex(text: str) -> list[dict]:
    """XMLパース失敗時のフォールバック: 正規表現でRSSアイテムを抽出"""
    items = []

    # デバッグ: <item>タグの数を確認
    item_count = text.count('<item>')
    print(f"[Yahoo RSS regex] テキスト内の<item>タグ数: {item_count}")

    if item_count == 0:
        # <item>がない場合、<entry>タグ（Atom形式）を試す
        item_count = text.count('<entry>')
        if item_count > 0:
            print(f"[Yahoo RSS regex] Atom形式検出: <entry>タグ {item_count} 個")
            pattern = re.compile(r'<entry>(.*?)</entry>', re.DOTALL)
        else:
            print(f"[Yahoo RSS regex] RSSアイテムが見つかりません")
            # デバッグ用に先頭500文字を出力
            print(f"[Yahoo RSS regex] コンテンツ先頭: {text[:500]}")
            return items
    else:
        pattern = re.compile(r'<item>(.*?)</item>', re.DOTALL)

    for match in pattern.finditer(text):
        block = match.group(1)
        title = _extract_tag(block, "title")
        link = _extract_tag(block, "link")
        pub = _extract_tag(block, "pubDate") or _extract_tag(block, "published")
        desc = _extract_tag(block, "description") or _extract_tag(block, "summary")

        if not title:
            continue

        news_type = classify(title)
        code = extract_code(title)

        items.append({
            "title": title,
            "link": link,
            "type": news_type,
            "code": code,
            "pub": pub,
            "desc": desc[:200] if desc else "",
            "source": "yahoo"
        })

    print(f"[Yahoo RSS regex] {len(items)} 件取得")
    return items


def _extract_tag(block: str, tag: str) -> str:
    """XMLブロックからタグの内容を正規表現で抽出"""
    # CDATA付きパターンを先に試す
    m = re.search(rf'<{tag}[^>]*><!\[CDATA\[(.*?)\]\]></{tag}>', block, re.DOTALL)
    if m:
        return m.group(1).strip()
    # 通常パターン
    m = re.search(rf'<{tag}[^>]*>(.*?)</{tag}>', block, re.DOTALL)
    if m:
        val = m.group(1).strip()
        # 入れ子CDATA除去
        val = re.sub(r'<!\[CDATA\[(.*?)\]\]>', r'\1', val, flags=re.DOTALL)
        return val
    # 自己閉じタグ（link href="..."）
    m = re.search(rf'<{tag}\s+[^>]*href="([^"]*)"[^>]*/>', block)
    if m:
        return m.group(1)
    return ""


# --- J-Quants 財務サマリー取得 ---
def fetch_jquants_schedule() -> list[dict]:
    """J-Quants API v2 から財務サマリーを取得 (x-api-key 認証)
    注: 無料プランでは /fins/announcement は利用不可
    代わりに /fins/summary（利用可能）から最新の決算情報を取得"""
    schedule = []

    if not JQUANTS_API_KEY:
        print("[J-Quants] APIキー未設定のためスキップ")
        return schedule

    today = datetime.now(JST).strftime("%Y-%m-%d")

    # まず /fins/announcement を試す（有料プランの場合）
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
            return schedule
        elif resp.status_code == 403:
            print("[J-Quants] /fins/announcement は利用不可（無料プラン制限）")
        else:
            print(f"[J-Quants] /fins/announcement ステータス {resp.status_code}")
    except Exception as e:
        print(f"[J-Quants] /fins/announcement エラー: {e}")

    # フォールバック: /fins/summary から最新の決算情報を取得
    try:
        resp = requests.get(
            f"{JQUANTS_BASE}/fins/summary",
            headers={"x-api-key": JQUANTS_API_KEY},
            params={"date": today},
            timeout=30,
        )
        if resp.status_code == 200:
            data = resp.json()
            summary_list = data.get("fins_summary", [])
            print(f"[J-Quants] 財務サマリー {len(summary_list)} 件取得")
            # サマリーをschedule形式に変換
            for item in summary_list[:50]:  # 最大50件
                schedule.append({
                    "code": item.get("LocalCode", "")[:4],
                    "company": item.get("CompanyName", ""),
                    "date": item.get("DisclosedDate", ""),
                    "type": "summary",
                })
        else:
            print(f"[J-Quants] /fins/summary ステータス {resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        print(f"[J-Quants] /fins/summary エラー: {e}")

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
