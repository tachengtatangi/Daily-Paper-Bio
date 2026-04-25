#!/usr/bin/env python3
"""
独立性能测试脚本：串行 vs 并行 PubMed efetch 对比。

测试原理
--------
PubMed efetch 当前是串行的：每批100个ID → sleep(0.11s) → HTTP请求(~2s) → XML解析。
有 API key 时 NCBI 上限是 10 req/s，但串行实际只达到 ~0.5 req/s（被网络延迟限制而非速率限制）。
并行方案：用线程安全 rate limiter 预约"发射槽"，多线程并发等待各自的槽，IO重叠 → 预期 4-5x 加速。

用法
----
    python test_pubmed_parallel_fetch.py --date 2025-08-26 --retmax 500
    python test_pubmed_parallel_fetch.py --date 2025-08-26 --retmax 2000 --workers 8
    python test_pubmed_parallel_fetch.py --date 2025-08-26 --retmax 500 --serial-only
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import sys
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

# ── 参数（对应 user-config.local.json 中的 ncbi_api_key） ────────────────────
# 优先从环境变量读取，方便 CI；也可以直接在这里填入
# 自动从 user-config.local.json 读取 API key（如有），也可用环境变量覆盖
def _load_ncbi_key() -> str:
    env = os.getenv("NCBI_API_KEY", "")
    if env:
        return env
    try:
        _local = os.path.join(os.path.dirname(__file__), "..", "_shared", "user-config.local.json")
        with open(_local, encoding="utf-8-sig") as _f:
            _d = json.load(_f)
        return str(_d.get("sources", {}).get("ncbi_api_key", "") or "").strip()
    except Exception:
        return ""

NCBI_API_KEY = _load_ncbi_key()
FETCH_BATCH    = 100       # 每批 ID 数，与主脚本一致
USER_AGENT     = "daily-papers-test/1.0"

PUBMED_SEARCH_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
PUBMED_FETCH_BASE  = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"

# ── 线程安全 Rate Limiter ──────────────────────────────────────────────────────
class _NcbiRateLimiter:
    """
    预约"发射时间槽"；lock 内只更新 _next_allowed，sleep 在 lock 外执行。
    多线程可并发等待各自的槽，不互相堵塞。

    示意（interval=0.11s, 3 workers）：
      thread-1 预约 t=0.11, thread-2 预约 t=0.22, thread-3 预约 t=0.33
      三者并发 sleep → 几乎同时发射请求
      批次完成后各自预约下一轮槽
    """
    def __init__(self, interval: float):
        self._lock = threading.Lock()
        self._next_allowed: float = 0.0
        self.interval = interval

    def acquire(self) -> None:
        with self._lock:
            now = time.time()
            fire_at = max(now, self._next_allowed)
            self._next_allowed = fire_at + self.interval
        wait = fire_at - time.time()
        if wait > 0:
            time.sleep(wait)


# ── HTTP 工具 ─────────────────────────────────────────────────────────────────
def _http_get(url: str, timeout: int = 60, retries: int = 2) -> str:
    req = Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(retries):
        try:
            with urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except HTTPError as exc:
            if exc.code not in (429, 500, 502, 503, 504):
                break
            if attempt < retries - 1:
                time.sleep(2.0 * (attempt + 1))
        except (URLError, OSError, TimeoutError) as exc:
            if attempt < retries - 1:
                time.sleep(1.5)
            else:
                # 截断 URL 避免刷屏
                short = url.split("&")[0]
                print(f"  [WARN] {short}…: {exc}", file=sys.stderr)
    return ""


def _add_key(params: dict) -> dict:
    if NCBI_API_KEY:
        params["api_key"] = NCBI_API_KEY
    params.setdefault("tool",  os.getenv("NCBI_TOOL_NAME",  "daily-papers-test"))
    params.setdefault("email", os.getenv("NCBI_TOOL_EMAIL", "daily-papers@example.com"))
    return params


# ── esearch：获取 ID 列表（与主脚本相同逻辑） ──────────────────────────────────
def esearch(start_date, end_date, retmax: int, rate: _NcbiRateLimiter) -> list[str]:
    base = _add_key({
        "db": "pubmed",
        "term": "journal article[pt] AND hasabstract[text]",
        "mindate": start_date.isoformat(),
        "maxdate": end_date.isoformat(),
        "datetype": "pdat",
        "sort": "pub date",
        "retmode": "json",
    })

    # probe：拿 total count
    probe = dict(base); probe["retmax"] = "0"
    rate.acquire()
    raw = _http_get(f"{PUBMED_SEARCH_BASE}?{urlencode(probe)}")
    if not raw:
        return []
    count = int(json.loads(raw).get("esearchresult", {}).get("count", 0))
    target = min(count, retmax) if retmax > 0 else count
    print(f"  esearch total={count}, target={target}", file=sys.stderr)

    ids: list[str] = []
    for retstart in range(0, target, 10000):
        p = dict(base)
        p["retstart"] = str(retstart)
        p["retmax"]   = str(min(10000, target - retstart))
        rate.acquire()
        page_raw = _http_get(f"{PUBMED_SEARCH_BASE}?{urlencode(p)}")
        if page_raw:
            ids.extend(json.loads(page_raw).get("esearchresult", {}).get("idlist", []))
    print(f"  fetched {len(ids)} IDs", file=sys.stderr)
    return ids


# ── XML 解析（最简版，只统计条数，不做完整打分） ──────────────────────────────
def _parse_batch_xml(xml_text: str) -> int:
    """返回成功解析的论文条数。仅用于验证结果一致性，不做完整字段提取。"""
    if not xml_text:
        return 0
    try:
        root = ET.fromstring(xml_text)
        return len(root.findall(".//PubmedArticle"))
    except ET.ParseError:
        return 0


# ── 串行 efetch ───────────────────────────────────────────────────────────────
def efetch_serial(ids: list[str], rate: _NcbiRateLimiter) -> tuple[int, float]:
    """串行逐批 efetch。返回 (解析论文总数, 耗时秒)。"""
    total = 0
    t0 = time.time()
    batches = [ids[i:i+FETCH_BATCH] for i in range(0, len(ids), FETCH_BATCH)]
    for idx, batch in enumerate(batches):
        rate.acquire()
        params = _add_key({"db":"pubmed","id":",".join(batch),"retmode":"xml"})
        xml = _http_get(f"{PUBMED_FETCH_BASE}?{urlencode(params)}")
        total += _parse_batch_xml(xml)
        if (idx + 1) % 10 == 0 or idx + 1 == len(batches):
            elapsed = time.time() - t0
            print(f"    串行 [{idx+1}/{len(batches)}] parsed={total}  elapsed={elapsed:.1f}s",
                  file=sys.stderr)
    return total, time.time() - t0


# ── 并行 efetch ───────────────────────────────────────────────────────────────
def efetch_parallel(ids: list[str], rate: _NcbiRateLimiter, workers: int) -> tuple[int, float]:
    """并行 efetch，使用线程安全 rate limiter。返回 (解析论文总数, 耗时秒)。"""
    batches = [ids[i:i+FETCH_BATCH] for i in range(0, len(ids), FETCH_BATCH)]
    results: dict[int, int] = {}  # idx → count
    lock = threading.Lock()
    completed = [0]

    def fetch_one(idx: int, batch: list[str]) -> None:
        rate.acquire()
        params = _add_key({"db":"pubmed","id":",".join(batch),"retmode":"xml"})
        xml = _http_get(f"{PUBMED_FETCH_BASE}?{urlencode(params)}")
        n = _parse_batch_xml(xml)
        with lock:
            results[idx] = n
            completed[0] += 1
            done = completed[0]
        if done % 10 == 0 or done == len(batches):
            elapsed = time.time() - t0
            print(f"    并行 [{done}/{len(batches)}]  elapsed={elapsed:.1f}s",
                  file=sys.stderr)

    t0 = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as exe:
        futs = [exe.submit(fetch_one, i, b) for i, b in enumerate(batches)]
        concurrent.futures.wait(futs)

    total = sum(results.values())
    return total, time.time() - t0


# ── 入口 ──────────────────────────────────────────────────────────────────────
def main() -> int:
    parser = argparse.ArgumentParser(description="PubMed efetch 串行 vs 并行性能测试")
    parser.add_argument("--date",    default="2025-08-26", help="测试日期 YYYY-MM-DD")
    parser.add_argument("--days",    type=int, default=1,   help="回溯天数")
    parser.add_argument("--retmax",  type=int, default=500, help="最多抓取 ID 数（建议 500-2000 快速测试）")
    parser.add_argument("--workers", type=int, default=5,   help="并行线程数（默认 5）")
    parser.add_argument("--serial-only", action="store_true", help="只跑串行，不跑并行对比")
    args = parser.parse_args()

    target = datetime.strptime(args.date, "%Y-%m-%d").date()
    start  = target - timedelta(days=args.days - 1)

    has_key = bool(NCBI_API_KEY)
    interval = 0.11 if has_key else 0.34
    print(f"\n=== PubMed efetch 性能测试 ===", file=sys.stderr)
    print(f"  日期: {start} → {target}  retmax={args.retmax}  workers={args.workers}", file=sys.stderr)
    print(f"  API key: {'有（10 req/s）' if has_key else '无（3 req/s）'}  interval={interval}s", file=sys.stderr)
    print(f"  预期批次数: {args.retmax // FETCH_BATCH} 批（{FETCH_BATCH} IDs/批）", file=sys.stderr)

    # 用单独的 rate limiter 先拿 IDs
    esearch_rate = _NcbiRateLimiter(interval)
    ids = esearch(start, target, args.retmax, esearch_rate)
    if not ids:
        print("  [ERROR] 没有拿到 ID，测试终止", file=sys.stderr)
        return 1
    actual_batches = (len(ids) + FETCH_BATCH - 1) // FETCH_BATCH
    print(f"\n  实际 ID 数: {len(ids)}  批次数: {actual_batches}", file=sys.stderr)

    # ── 串行测试 ──
    print(f"\n--- 串行 efetch ---", file=sys.stderr)
    serial_rate = _NcbiRateLimiter(interval)
    serial_count, serial_t = efetch_serial(ids, serial_rate)
    print(f"  串行完成: {serial_count} 篇  耗时 {serial_t:.2f}s  ({serial_t/actual_batches:.2f}s/批)", file=sys.stderr)

    if args.serial_only:
        return 0

    # 串并行使用不同 rate limiter 实例（各自独立计时）
    print(f"\n--- 并行 efetch (workers={args.workers}) ---", file=sys.stderr)
    parallel_rate = _NcbiRateLimiter(interval)
    parallel_count, parallel_t = efetch_parallel(ids, parallel_rate, args.workers)
    print(f"  并行完成: {parallel_count} 篇  耗时 {parallel_t:.2f}s  ({parallel_t/actual_batches:.2f}s/批)", file=sys.stderr)

    # ── 结果对比 ──
    speedup = serial_t / parallel_t if parallel_t > 0 else float("inf")
    print(f"\n=== 对比结果 ===", file=sys.stderr)
    print(f"  串行耗时:      {serial_t:.2f}s", file=sys.stderr)
    print(f"  并行耗时:      {parallel_t:.2f}s  (workers={args.workers})", file=sys.stderr)
    print(f"  加速比:        {speedup:.2f}x", file=sys.stderr)
    print(f"  结果一致性:    串行={serial_count}篇  并行={parallel_count}篇  "
          f"{'✅ 一致' if serial_count == parallel_count else '⚠️ 不一致'}", file=sys.stderr)

    # 推算完整 5000 IDs 的时间
    ratio = len(ids) / 5000
    est_serial_5k   = serial_t   / ratio
    est_parallel_5k = parallel_t / ratio
    print(f"\n  推算 5000 IDs（单日完整量）:", file=sys.stderr)
    print(f"    串行预估:    {est_serial_5k:.0f}s  (~{est_serial_5k/60:.1f} 分钟)", file=sys.stderr)
    print(f"    并行预估:    {est_parallel_5k:.0f}s  (~{est_parallel_5k/60:.1f} 分钟)", file=sys.stderr)

    # 推算 7 天 15000 IDs（cap 后）
    ratio15k = len(ids) / 15000
    est_serial_7d   = serial_t   / ratio15k
    est_parallel_7d = parallel_t / ratio15k
    print(f"\n  推算 7天/15000 IDs（cap 后）:", file=sys.stderr)
    print(f"    串行预估:    {est_serial_7d:.0f}s  (~{est_serial_7d/60:.1f} 分钟)", file=sys.stderr)
    print(f"    并行预估:    {est_parallel_7d:.0f}s  (~{est_parallel_7d/60:.1f} 分钟)", file=sys.stderr)

    print(f"\n  结论: {'建议引入并行，效果显著' if speedup > 1.8 else '网络条件限制，并行提升有限，建议保持串行'}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
