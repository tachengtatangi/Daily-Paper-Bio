#!/usr/bin/env python3
"""
独立测试脚本：验证 bioRxiv fetch 逻辑是否正常工作。
无需 user_config / cas_quartiles / library_profile，所有参数内联。

用法：
    python tests/test_biorxiv_fetch.py                     # 测试今天
    python tests/test_biorxiv_fetch.py --date 2025-08-26   # 测试指定单日
    python tests/test_biorxiv_fetch.py --days 3            # 测试过去3天
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import re
import sys
import time
from datetime import date, datetime, timedelta
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

# ── 测试参数（对应 user-config.json 里的值） ──────────────────────────────────
BIORXIV_RETMAX       = 300
BIORXIV_RETMAX_TOTAL_CAP = 3000
BIORXIV_TIMEOUT      = 45
BIORXIV_CATEGORIES   = {"evolutionary biology", "genomics", "zoology"}
# 如果改成空 set 则接受所有 category
# BIORXIV_CATEGORIES = set()

BIORXIV_DETAILS_BASE = "https://api.biorxiv.org/details/biorxiv"
USER_AGENT = "daily-papers-test/1.0"

# ── 统计 ──────────────────────────────────────────────────────────────────────
STATS = {
    "raw_total": 0,
    "after_category": 0,
    "fetch_failed_count": 0,
    "pagination_pages": 0,
    "daily_fallback_triggered": False,
    "daily_fallback_count": 0,
}

# ── HTTP 工具 ─────────────────────────────────────────────────────────────────

def fetch_json_with_meta(url: str, timeout: int = 60, retries: int = 3,
                          backoff_base: float = 2.0) -> tuple[dict, str]:
    last_exc = None
    last_kind = "error"
    for attempt in range(retries):
        try:
            req = Request(url, headers={"User-Agent": USER_AGENT})
            with urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                return json.loads(raw), "ok"
        except HTTPError as exc:
            last_exc = exc
            last_kind = f"http_{exc.code}"
            if exc.code == 404:
                break
            if exc.code < 500 and exc.code != 429:
                break
        except TimeoutError as exc:
            last_exc = exc
            last_kind = "timeout"
        except URLError as exc:
            last_exc = exc
            msg = str(getattr(exc, "reason", exc)).lower()
            last_kind = "timeout" if "timed out" in msg else "urlerror"
        except OSError as exc:
            last_exc = exc
            msg = str(exc).lower()
            if "timed out" in msg or "no such file or directory" in msg:
                last_kind = "timeout"
            else:
                last_kind = "oserror"
        except Exception as exc:
            last_exc = exc
            last_kind = exc.__class__.__name__.lower()
            break
        if attempt < retries - 1:
            time.sleep(backoff_base * (attempt + 1))
    print(f"  [WARN] fetch failed {url}: {last_exc}", file=sys.stderr)
    return {}, last_kind

# ── bioRxiv 工具 ──────────────────────────────────────────────────────────────

def category_allowed(category: str) -> bool:
    normalized = re.sub(r"\s+", " ", str(category or "").strip()).lower()
    if not BIORXIV_CATEGORIES:
        return True
    return normalized in BIORXIV_CATEGORIES


def normalize_item(item: dict) -> dict | None:
    """最简版 normalize：只取必要字段，不做打分。"""
    title = re.sub(r"\s+", " ", (item.get("title") or "")).strip()
    doi   = (item.get("doi") or "").strip()
    if not title or not doi:
        return None
    server  = (item.get("server") or "biorxiv").strip().lower()
    version = str(item.get("version") or "1").strip()
    return {
        "id":       f"{server}:{doi}v{version}",
        "title":    title,
        "doi":      doi,
        "date":     (item.get("date") or "").strip(),
        "category": (item.get("category") or "").strip(),
        "authors":  (item.get("authors") or "").strip(),
        "url":      f"https://www.biorxiv.org/content/{doi}v{version}",
    }


def fetch_biorxiv_interval(
    start_date, end_date,
    timeout: int | None = None,
    retries: int | None = None,
    retmax: int | None = None,
) -> tuple[list[dict], bool]:
    batch_size   = 100
    papers: list[dict] = []
    seen_ids: set[str] = set()
    start = start_date.isoformat()
    end   = end_date.isoformat()
    had_failure  = False

    base_timeout = BIORXIV_TIMEOUT
    timeout  = timeout  if timeout  is not None else (base_timeout * 2 if start_date != end_date else base_timeout)
    retries  = retries  if retries  is not None else (2            if start_date != end_date else 1)
    eff_max  = max(0, retmax if retmax is not None else BIORXIV_RETMAX)
    if eff_max == 0:
        print("  bioRxiv disabled (retmax=0)", file=sys.stderr)
        return [], False

    print(f"  fetch_biorxiv_interval {start} → {end}  timeout={timeout}s retries={retries} retmax={eff_max}", file=sys.stderr)

    def collect(collection: list[dict]) -> None:
        STATS["pagination_pages"] += 1
        STATS["raw_total"] += len(collection)
        for item in collection:
            if not isinstance(item, dict):
                continue
            cat = (item.get("category") or "").strip()
            if not category_allowed(cat):
                continue
            STATS["after_category"] += 1
            paper = normalize_item(item)
            if not paper or paper["id"] in seen_ids:
                continue
            seen_ids.add(paper["id"])
            papers.append(paper)

    def fetch_cursor(cursor: int):
        path = f"{BIORXIV_DETAILS_BASE}/{start}/{end}/{cursor}"
        payload, kind = fetch_json_with_meta(path, timeout=timeout, retries=retries, backoff_base=3.0)
        return cursor, payload, kind

    # ── 首页 ──
    cursor0, payload0, kind0 = fetch_cursor(0)
    collection0 = payload0.get("collection") if isinstance(payload0, dict) else None
    if not isinstance(collection0, list):
        if kind0 != "ok":
            STATS["fetch_failed_count"] += 1
            had_failure = True
        return papers, had_failure
    if not collection0:
        return papers, had_failure

    collect(collection0)

    message0 = payload0.get("messages", [{}])[0] if isinstance(payload0, dict) else {}
    try:
        total = int(message0.get("total") or len(collection0))
    except Exception:
        total = len(collection0)

    print(f"  API reports total={total} papers in range; eff_max={eff_max}", file=sys.stderr)

    max_items = min(total, eff_max)
    cursors   = list(range(batch_size, max_items, batch_size))
    if not cursors:
        return papers, had_failure

    max_workers = 3 if start_date != end_date else 2
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(fetch_cursor, c) for c in cursors]
        for future in concurrent.futures.as_completed(futures):
            cursor, payload, kind = future.result()
            collection = payload.get("collection") if isinstance(payload, dict) else None
            if not isinstance(collection, list):
                if kind != "ok":
                    STATS["fetch_failed_count"] += 1
                    had_failure = True
                continue
            collect(collection)

    return papers, had_failure


def fetch_biorxiv_interval_daily_recovery(start_date, end_date) -> list[dict]:
    """多天模式失败时，逐日重试。"""
    papers: list[dict] = []
    seen_ids: set[str] = set()
    current = start_date
    STATS["daily_fallback_triggered"] = True
    print("  [recovery] 逐日回退模式启动", file=sys.stderr)
    while current <= end_date:
        STATS["daily_fallback_count"] += 1
        day_papers, day_failed = fetch_biorxiv_interval(
            current, current, timeout=30, retries=1, retmax=BIORXIV_RETMAX
        )
        if not day_failed:
            for p in day_papers:
                if p["id"] not in seen_ids:
                    seen_ids.add(p["id"])
                    papers.append(p)
        current += timedelta(days=1)
    return papers


def fetch_biorxiv_papers(start_date, end_date, days: int = 1) -> list[dict]:
    eff = (BIORXIV_RETMAX * max(1, days)) if BIORXIV_RETMAX > 0 else 0
    if BIORXIV_RETMAX_TOTAL_CAP > 0 and eff > BIORXIV_RETMAX_TOTAL_CAP:
        eff = BIORXIV_RETMAX_TOTAL_CAP
        print(f"  bioRxiv retmax capped: {BIORXIV_RETMAX}/day × {days} → {eff}", file=sys.stderr)
    else:
        print(f"  bioRxiv retmax: {BIORXIV_RETMAX}/day × {days} = {eff or 'unlimited'}", file=sys.stderr)

    papers, had_failure = fetch_biorxiv_interval(start_date, end_date, retmax=eff)
    if had_failure and not papers and start_date != end_date:
        papers = fetch_biorxiv_interval_daily_recovery(start_date, end_date)

    papers.sort(key=lambda x: x.get("date", ""), reverse=True)
    return papers

# ── 入口 ──────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="独立测试 bioRxiv fetch")
    parser.add_argument("--date",  default=None, help="目标日期 YYYY-MM-DD（默认今天）")
    parser.add_argument("--days",  type=int, default=1, help="回溯天数（默认 1）")
    args = parser.parse_args()

    target_date = datetime.strptime(args.date, "%Y-%m-%d").date() if args.date else date.today()
    days        = max(1, args.days)
    start_date  = target_date - timedelta(days=days - 1)

    print(f"\n=== bioRxiv fetch test ===", file=sys.stderr)
    print(f"  日期范围: {start_date} → {target_date}  ({days} 天)", file=sys.stderr)
    print(f"  categories: {sorted(BIORXIV_CATEGORIES) or '全部'}", file=sys.stderr)
    print(f"  retmax/day={BIORXIV_RETMAX}  total_cap={BIORXIV_RETMAX_TOTAL_CAP}  timeout={BIORXIV_TIMEOUT}s", file=sys.stderr)
    print("", file=sys.stderr)

    t0 = time.time()
    papers = fetch_biorxiv_papers(start_date, target_date, days=days)
    elapsed = time.time() - t0

    print(f"\n=== 结果 ===", file=sys.stderr)
    print(f"  耗时:          {elapsed:.2f}s", file=sys.stderr)
    print(f"  raw_total:     {STATS['raw_total']}", file=sys.stderr)
    print(f"  after_category:{STATS['after_category']}", file=sys.stderr)
    print(f"  fetch_failed:  {STATS['fetch_failed_count']}", file=sys.stderr)
    print(f"  pagination_pages: {STATS['pagination_pages']}", file=sys.stderr)
    print(f"  daily_fallback: {STATS['daily_fallback_triggered']} (count={STATS['daily_fallback_count']})", file=sys.stderr)
    print(f"  最终论文数:    {len(papers)}", file=sys.stderr)

    if papers:
        print(f"\n  前 5 篇：", file=sys.stderr)
        for i, p in enumerate(papers[:5], 1):
            print(f"    [{i}] [{p['date']}] [{p['category']}] {p['title'][:80]}", file=sys.stderr)

    # stdout 输出 JSON（方便管道处理）
    print(json.dumps(papers, ensure_ascii=False, indent=2))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
