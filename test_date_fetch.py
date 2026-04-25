#!/usr/bin/env python3
"""
test_date_fetch.py — 验证 fetch_and_score.py 的日期解析和 PubMed pdat 行为。

用法:
    python test_date_fetch.py
    python test_date_fetch.py --date 2025-03-10 --days 3

测试项:
  1. --date 参数解析（之前因 `date` 未 import 而 crash）
  2. PubMed esearch probe：确认 pdat 窗口命中数量
  3. 抓取 5 篇样本，显示它们的 epub/entrez/journal 日期
  4. 单独搜索 "malodorous flowers" 论文，解释为何未被推荐
"""

import sys
import io
# Force UTF-8 stdout/stderr so Chinese + symbols print correctly on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
import json
import argparse
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen, Request
import xml.etree.ElementTree as ET

# ── 路径设置 ──────────────────────────────────────────────────────────────
_DIR = Path(__file__).resolve().parent
_SHARED = _DIR / "_shared"
_DAILY = _DIR / "daily-papers"
for p in [str(_SHARED), str(_DAILY)]:
    if p not in sys.path:
        sys.path.insert(0, p)

from user_config import daily_papers_config, ncbi_api_key as _ncbi_api_key

_CONFIG = daily_papers_config()
KEYWORDS = list(_CONFIG["keywords"])
MIN_SCORE = _CONFIG["min_score"]
MIN_QUARTILE = int(_CONFIG.get("min_quartile", 1))
_API_KEY = _ncbi_api_key()

PUBMED_SEARCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
PUBMED_FETCH  = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"

SEP = "─" * 64

def _add_key(params: dict) -> dict:
    if _API_KEY:
        params["api_key"] = _API_KEY
    return params

def fetch_text(url: str) -> str:
    try:
        req = Request(url, headers={"User-Agent": "test_date_fetch/1.0"})
        with urlopen(req, timeout=20) as r:
            return r.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"  [HTTP ERROR] {e}", file=sys.stderr)
        return ""

def text_or_empty(node) -> str:
    return (node.text or "").strip() if node is not None else ""


# ── 测试 1: 日期解析 ──────────────────────────────────────────────────────
def test_date_parsing(date_str: str, days: int):
    print(f"\n{SEP}")
    print("TEST 1: --date 参数解析（之前 date 未 import 导致 NameError）")
    print(SEP)
    try:
        # 旧写法（会失败）:  date(y, m, d) 但 date 未 import
        # 新写法:            datetime.strptime 或 date(y,m,d) with 'from datetime import date'
        _dp = date_str.split("-")
        target_date = date(int(_dp[0]), int(_dp[1]), int(_dp[2]))
        start_date  = target_date - timedelta(days=days - 1)
        print(f"  ✅ 解析成功")
        print(f"  target_date = {target_date}")
        print(f"  start_date  = {start_date}  (days={days})")
        print(f"  PubMed window: {start_date.isoformat()} ~ {target_date.isoformat()}")
        return start_date, target_date
    except NameError as e:
        print(f"  ❌ NameError: {e}  ← 说明 'date' 仍未 import！")
        return None, None


# ── 测试 2: PubMed esearch probe ────────────────────────────────────────────
def test_esearch_probe(start_date: date, end_date: date):
    print(f"\n{SEP}")
    print(f"TEST 2: PubMed esearch probe (datetype=pdat, {start_date} ~ {end_date})")
    print(SEP)
    params = _add_key({
        "db": "pubmed",
        "term": "journal article[pt] AND hasabstract[text]",
        "mindate": start_date.isoformat(),
        "maxdate": end_date.isoformat(),
        "datetype": "pdat",
        "retmax": "0",
        "retmode": "json",
    })
    url = f"{PUBMED_SEARCH}?{urlencode(params)}"
    raw = fetch_text(url)
    if not raw:
        print("  ❌ 请求失败")
        return []
    try:
        data = json.loads(raw)
        count = int(data.get("esearchresult", {}).get("count", 0))
        print(f"  PubMed pdat [{start_date} ~ {end_date}] 命中: {count} 篇")
        # 取前 5 个 ID 用于后续展示
        params["retmax"] = "5"
        raw2 = fetch_text(f"{PUBMED_SEARCH}?{urlencode(params)}")
        ids = json.loads(raw2).get("esearchresult", {}).get("idlist", [])
        print(f"  前 5 个 PMID: {ids}")
        return ids
    except Exception as e:
        print(f"  ❌ 解析失败: {e}")
        return []


# ── 测试 3: 日期字段验证 ────────────────────────────────────────────────────
def test_date_fields(pmids: list[str]):
    print(f"\n{SEP}")
    print("TEST 3: 验证每篇论文的 epub / entrez / journal 日期")
    print(SEP)
    if not pmids:
        print("  无 PMID 可测试")
        return

    params = _add_key({
        "db": "pubmed",
        "id": ",".join(pmids),
        "retmode": "xml",
    })
    raw = fetch_text(f"{PUBMED_FETCH}?{urlencode(params)}")
    if not raw:
        print("  ❌ efetch 失败")
        return

    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        print(f"  ❌ XML 解析失败: {e}")
        return

    for art in root.findall(".//PubmedArticle"):
        medline = art.find("MedlineCitation")
        pubmed  = art.find("PubmedData")
        if medline is None:
            continue

        pmid_node = medline.find("PMID")
        pmid = text_or_empty(pmid_node)

        article_node = medline.find("Article")
        if article_node is None:
            continue

        title = text_or_empty(article_node.find("ArticleTitle"))

        # ── epub date (fix 后的正确方式) ───────────────────────────────
        epub_date = ""
        for _ad in article_node.findall("ArticleDate"):
            if _ad.attrib.get("DateType") == "Electronic":
                y = text_or_empty(_ad.find("Year"))
                m = text_or_empty(_ad.find("Month"))
                d = text_or_empty(_ad.find("Day"))
                epub_date = "-".join(p for p in [y, m, d] if p)
                if epub_date:
                    break
        epub_exists = bool(epub_date)

        # ── entrez date ─────────────────────────────────────────────────
        entrez_date = ""
        if pubmed is not None:
            for hist in pubmed.findall("History/PubMedPubDate"):
                if hist.attrib.get("PubStatus") in {"pubmed", "entrez"}:
                    y = text_or_empty(hist.find("Year"))
                    m = text_or_empty(hist.find("Month"))
                    d = text_or_empty(hist.find("Day"))
                    entrez_date = "-".join(p for p in [y, m, d] if p)
                    if entrez_date:
                        break

        # ── journal/print date ──────────────────────────────────────────
        pubdate_node = article_node.find("Journal/JournalIssue/PubDate")
        journal_date = ""
        if pubdate_node is not None:
            y = text_or_empty(pubdate_node.find("Year"))
            m = text_or_empty(pubdate_node.find("Month"))
            d = text_or_empty(pubdate_node.find("Day"))
            journal_date = "-".join(p for p in [y, m, d] if p)

        chosen = epub_date or entrez_date or journal_date
        marker = "✅" if epub_exists else "⚠️ (no epub→fallback)"

        print(f"\n  PMID {pmid}: {title[:80]}")
        print(f"    epub_date    = {epub_date!r}  {marker}")
        print(f"    entrez_date  = {entrez_date!r}")
        print(f"    journal_date = {journal_date!r}")
        print(f"    → chosen     = {chosen!r}")


# ── 测试 4: 搜索 malodorous flowers 论文 ────────────────────────────────────
def test_malodorous_flowers():
    print(f"\n{SEP}")
    print("TEST 4: 搜索 'Convergent acquisition of disulfide-forming enzymes in malodorous flowers'")
    print(SEP)

    # 用标题关键词直接搜索
    query = 'disulfide malodorous flowers[tiab] OR "malodorous flowers"[tiab]'
    params = _add_key({
        "db": "pubmed",
        "term": query,
        "retmax": "5",
        "retmode": "json",
    })
    raw = fetch_text(f"{PUBMED_SEARCH}?{urlencode(params)}")
    if not raw:
        print("  ❌ 搜索失败")
        return
    ids = json.loads(raw).get("esearchresult", {}).get("idlist", [])
    if not ids:
        print("  结果: 0 篇 — PubMed 内未找到此论文（可能还未收录）")
        return
    print(f"  找到 PMID: {ids}")

    # 获取详情并评分
    params2 = _add_key({"db": "pubmed", "id": ",".join(ids), "retmode": "xml"})
    raw2 = fetch_text(f"{PUBMED_FETCH}?{urlencode(params2)}")
    if not raw2:
        return

    try:
        root = ET.fromstring(raw2)
    except ET.ParseError:
        return

    import re

    def norm(text: str) -> list[str]:
        tokens = []
        for part in re.split(r"[^A-Za-z0-9]+", (text or "").lower()):
            part = part.strip()
            if not part:
                continue
            # very simple stemming: strip trailing s/es
            if part.endswith("ies") and len(part) > 4:
                part = part[:-3] + "y"
            elif part.endswith("es") and len(part) > 4:
                part = part[:-2]
            elif part.endswith("s") and len(part) > 3:
                part = part[:-1]
            tokens.append(part)
        return tokens

    def kw_matches(all_tokens, kw):
        kw_toks = norm(kw)
        if not kw_toks:
            return False
        if len(kw_toks) == 1:
            return kw_toks[0] in all_tokens
        for i in range(len(all_tokens) - len(kw_toks) + 1):
            if all_tokens[i:i+len(kw_toks)] == kw_toks:
                return True
        return False

    for art in root.findall(".//PubmedArticle"):
        medline = art.find("MedlineCitation")
        if medline is None:
            continue
        pmid = text_or_empty(medline.find("PMID"))
        article = medline.find("Article")
        if article is None:
            continue
        title    = text_or_empty(article.find("ArticleTitle"))
        abstract = text_or_empty(article.find("Abstract/AbstractText"))
        journal  = text_or_empty(article.find("Journal/Title"))

        # epub date
        epub_date = ""
        for _ad in article.findall("ArticleDate"):
            if _ad.attrib.get("DateType") == "Electronic":
                y = text_or_empty(_ad.find("Year"))
                m = text_or_empty(_ad.find("Month"))
                d = text_or_empty(_ad.find("Day"))
                epub_date = "-".join(p for p in [y, m, d] if p)
                if epub_date:
                    break

        all_tokens = norm(title) + norm(abstract)
        matched = [kw for kw in KEYWORDS if kw_matches(all_tokens, kw)]

        # 简单计分
        title_tok = norm(title)
        abs_tok = norm(abstract)
        score = 0
        for kw in matched:
            if kw_matches(title_tok, kw):
                score += 3
            else:
                score += 1

        print(f"\n  PMID {pmid}")
        print(f"  标题:  {title}")
        print(f"  期刊:  {journal}")
        print(f"  epub:  {epub_date!r}")
        print(f"  命中关键词: {matched}")
        print(f"  估算得分: {score}  (min_score={MIN_SCORE})")
        if score < MIN_SCORE:
            print(f"  → ❌ 得分不足 — 会被过滤")
        else:
            print(f"  → ✅ 得分通过，会被保留（若分区满足 Q≤{MIN_QUARTILE}）")
        print(f"  摘要前200字: {abstract[:200]}")


# ── main ───────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date",  default="2025-03-10", help="目标日期 YYYY-MM-DD")
    parser.add_argument("--days",  type=int, default=3,  help="天数区间")
    args = parser.parse_args()

    print(f"\n{'═'*64}")
    print(f"  test_date_fetch.py  date={args.date}  days={args.days}")
    print(f"  config: min_score={MIN_SCORE}, min_quartile={MIN_QUARTILE}")
    print(f"  keywords ({len(KEYWORDS)}): {KEYWORDS[:5]}...")
    print(f"{'═'*64}")

    start_date, end_date = test_date_parsing(args.date, args.days)
    if start_date is None:
        print("\n❌ 日期解析失败，终止测试")
        sys.exit(1)

    pmids = test_esearch_probe(start_date, end_date)
    test_date_fields(pmids)
    test_malodorous_flowers()

    print(f"\n{'═'*64}")
    print("测试完成")
    print(f"{'═'*64}\n")


if __name__ == "__main__":
    main()
