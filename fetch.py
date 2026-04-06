#!/usr/bin/env python3
"""
株式情報収集スクリプト
- Yahoo!ファイナンス決算速報ページからニュースをスクレイピング
- Yahoo!ニュース ビジネスRSSからニュースを取得（フォールバック）
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
# Yahoo!ファイナンス決算速報ページ（HTML）
SETTLEMENT_URL = "https://finance.yahoo.co.jp/news/settlement"
# Yahoo!ニュース ビジネスRSS（フォールバック用）
YAHOO_NEWS_RSS = "https://news.yahoo.co.jp/rss/categories/business.xml"
# 代\u66ffRSSソース
KABUTAN_URL = "https://kabutan.jp/news/marketnews/?category=3"

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
    for type_key, keywords in TYPE_RULES:
        for kw in keywords:
            if kw in title:
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


# --- HTMLパーサー: Yahoo!ファイナンス決算速報ページ ---
class SettlementPageParser(HTMLParser):
    """Yahoo!ファイナンス決算速報ページからニュース項目を抽出"""

    def __init__(self):
        super().__init__()
        self.items = []
        self._in_article = False
        self._in_link = False
        self._current_link = ""
        self._current_title = ""
        self._in_time = False
        self._current_time = ""
        self._capture_text = False

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)

        # ニュース記事のリンクを探す
        if tag == "a":
            href = attrs_dict.get("href", "")
            if "/news/detail/" in href or "news.yahoo.co.jp" in href:
                self._in_link = True
                self._current_link = href
                self._current_title = ""
                self._capture_text = True

        # 時刻要素
        if tag == "time":
            self._in_time = True
            self._current_time = attrs_dict.get("datetime", "")

    def handle_endtag(self, tag):
        if tag == "a" and self._in_link:
            self._in_link = False
            self._capture_text = False
            if self._current_title.strip():
                self.items.append({
                    "title": self._current_title.strip(),
                    "link": self._current_link,
                    "time": self._current_time,
                })
            self._current_title = ""

        if tag == "time":
            self._in_time = False

    def handle_data(self, data):
        if self._capture_text and self._in_link:
            self._current_title += data


def fetch_yahoo_settlement_html() -> list[dict]:
    """Yahoo!ファイナンス決算速報ページをHTMLスクレイピング"""
    items = []
    try:
        resp = requests.get(SETTLEMENT_URL, timeout=30, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        })
        resp.raise_for_status()

        content = resp.text
        print(f"[Yahoo HTML] レスポンスサイズ: {len(content)} 文字")

        # HTMLパーサーでニュース衹目を抽出
        parser = SettlementPageParser()
        parser.feed(content)

        print(f"[Yahoo HTML] パーサーが {len(parser.items)} 件のリンクを検出")

        # 決算関連ニュースのみフィルタ
        for item in parser.items:
            title = item["title"]
            if not is_settlement_news(title):
                continue

            news_type = classify(title)
            code = extract_code(title)

            link = item["link"]
            if link.startswith("/"):
                link = "https://finance.yahoo.co.jp" + link

            items.append({
                "title": title,
                "link": link,
                "type": news_type,
                "code": code,
                "pub": item.get("time", ""),
                "desc": "",
                "source": "yahoo"
            })

        print(f"[Yahoo HTML] 決算関連ニュース {len(items)} 件抽出")

        # HTMLパーサーで0件の場合、正規表現フォールバック
        if len(items) == 0:
            print("[Yahoo HTML] パーサー0件 → 正規表現フォールバック")
            items = scrape_settlement_regex(content)

    except Exception as e:
        print(f"[Yahoo HTML] 取得エラー: {e}")
    return items


def scrape_settlement_regex(html: str) -> list[dict]:
    """正規表現でHTMLからニュースリンクを抽出（フォールバック）"""
    items = []

    # <a href="...">タイトル</a> パターンで抽出
    pattern = re.compile(
        r'<a\s+[^>]*href="([^"]*(?:news\.yahoo\.co\.jp|/news/detail/)[^"]*)"[^>]*>(.*?)</a>',
        re.DOTALL
    )

    seen_titles = set()
    for match in pattern.finditer(html):
        link = match.group(1)
        # HTMLタグを除去してタイトルを取得
        title = re.sub(r'<[^>]+>', '', match.group(2)).strip()

        if not title or len(title) < 5:
            continue
        if title in seen_titles:
            continue
        seen_titles.add(title)

        if not is_settlement_news(title):
            continue

        news_type = classify(title)
        code = extract_code(title)

        if link.startswith("/"):
            link = "https://finance.yahoo.co.jp" + link

        items.append({
            "title": title,
            "link": link,
            "type": news_type,
            "code": code,
            "pub": "",
            "desc": "",
            "source": "yahoo"
        })

    print(f"[Yahoo HTML regex] {len(items)} 件抽出")
    return items


# --- Yahoo!ニュース ビジネスRSS ---
def fetch_yahoo_news_rss() -> list[dict]:
    """Yahoo!ニュース ビジネスRSSから決算関連ニュースを取得"""
    items = []
    try:
        resp = requests.get(YAHOO_NEWS_RSS, timeout=30, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        })
        resp.raise_for_status()

        content = resp.content
        content_type = resp.headers.get("Content-Type", "")
        print(f"[Yahoo News RSS] サイズ: {len(content)} bytes, Content-Type: {content_type}")

        # HTMLチェック
        text = content.decode("utf-8", errors="replace")
        if '<html' in text[:500].lower() or '<!doctype html' in text[:500].lower():
            print("[Yahoo News RSS] HTMLが返されました - スキップ")
            return items

        # XMLパース
        try:
            root = ET.fromstring(content)
        except ET.ParseError:
            cleaned = clean_xml(text)
            try:
                root = ET.fromstring(cleaned.encode("utf-8"))
            except ET.ParseError as e:
                print(f"[Yahoo News RSS] XMLパースエラー: {e}")
                return items

        # item要素を探す（RSS 2.0 / Atom両対応）
        ns = {'atom': 'http://www.w3.org/2005/Atom'}

        # RSS 2.0
        for item in root.iter("item"):
            title = item.findtext("title", "")
            link = item.findtext("link", "")
            pub = item.findtext("pubDate", "")
            desc = item.findtext("description", "")

            if not is_settlement_news(title):
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
                "source": "yahoo_news"
            })

        # Atom形式
        if len(items) == 0:
            for entry in root.iter("{http://www.w3.org/2005/Atom}entry"):
                title_el = entry.find("{http://www.w3.org/2005/Atom}title")
                link_el = entry.find("{http://www.w3.org/2005/Atom}link")
                pub_el = entry.find("{http://www.w3.org/2005/Atom}published")

                title = title_el.text if title_el is not None and title_el.text else ""
                link = link_el.get("href", "") if link_el is not None else ""
                pub = pub_el.text if pub_el is not None and pub_el.text else ""

                if not is_settlement_news(title):
                    continue

                news_type = classify(title)
                code = extract_code(title)

                items.append({
                    "title": title,
                    "link": link,
                    "type": news_type,
                    "code": code,
                    "pub": pub,
                    "desc": "",
                    "source": "yahoo_news"
                })

        print(f"[Yahoo News RSS] 決算関連 {len(items)} 件取得")
    except Exception as e:
        print(f"[Yahoo News RSS] 取得エラー: {e}")
    return items


def clean_xml(text: str) -> str:
    """XMLパースエラー対策"""
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', text)
    text = re.sub(r'&(?!amp;|lt;|gt;|quot;|apos;|#\d+;|#x[0-9a-fA-F]+;)', '&amp;', text)
    return text


# --- 株探（kabutan）ニュース取得 ---
def fetch_kabutan_news() -> list[dict]:
    """株探の決算ニュースページからスクレイピング"""
    items = []
    try:
        resp = requests.get(KABUTAN_URL, timeout=30, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        })
        resp.raise_for_status()

        content = resp.text
        print(f"[Kabutan] レスポンスサイズ: {len(content)} 文字")

        # ニュース記事リンクを正規表現で抽出
        # kabutan.jp/news/?b=n202XXXXXXXX 形式
        pattern = re.compile(
            r'<a\s+[^>]*href="(/news/\?b=[^"]+)"[^>]*>\s*(.*?)\s*</a>',
            re.DOTALL
        )

        seen = set()
        for match in pattern.finditer(content):
            link = "https://kabutan.jp" + match.group(1)
            title = re.sub(r'<[^>]+>', '', match.group(2)).strip()

            if not title or len(title) < 5 or title in seen:
                continue
            seen.add(title)

            if not is_settlement_news(title):
                continue

            news_type = classify(title)
            code = extract_code(title)

            items.append({
                "title": title,
                "link": link,
                "type": news_type,
                "code": code,
                "pub": "",
                "desc": "",
                "source": "kabutan"
            })

        print(f"[Kabutan] 決算関連ニュース {len(items)} 件抽出")
    except Exception as e:
        print(f"[Kabutan] 取得エラー: {e}")
    return items


# --- メイン処理 ---
def main():
    print("=== 株式データ取得開始 ===")
    now = datetime.now(JST)

    all_items = []

    # ソース1: Yahoo!ファイナンス決算速報ページ（HTMLスクレイピング）
    yahoo_items = fetch_yahoo_settlement_html()
    all_items.extend(yahoo_items)

    # ソース2: Yahoo!ニュース ビジネスRSS
    rss_items = fetch_yahoo_news_rss()
    # 重複タイトル除去
    existing_titles = {item["title"] for item in all_items}
    for item in rss_items:
        if item["title"] not in existing_titles:
            all_items.append(item)
            existing_titles.add(item["title"])

    # ソース3: 株探（フォールバック）
    if len(all_items) < 5:
        print("[Main] ニュース件数が少ないため株探からも取得")
        kabutan_items = fetch_kabutan_news()
        for item in kabutan_items:
            if item["title"] not in existing_titles:
                all_items.append(item)
                existing_titles.add(item["title"])

    # 件数カウント
    count = {"up": 0, "down": 0, "div": 0, "earn": 0}
    for item in all_items:
        t = item.get("type", "earn")
        if t in count:
            count[t] += 1

    # data.json 書き出し
    output = {
        "updated": now.strftime("%Y/%m/%d %H:%M"),
        "date": now.strftime("%Y-%m-%d"),
        "count": count,
        "items": all_items,
        "schedule": [],
    }
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"=== 完了: {len(all_items)} 件のニュース ===")
    print(f"=== 内��: 上方修正={count['up']}, 下方修正={count['down']}, 配当={count['div']}, 決算={count['earn']} ===")
    print(f"=== 出力: {OUTPUT_PATH} ===")


if __name__ == "__main__":
    main()
