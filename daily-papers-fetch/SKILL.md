---
name: daily-papers-fetch
description: |
  论文抓取流程的第 1 步。抓取 PubMed + bioRxiv，打分、去重、富化后输出到临时 JSON，供 review 和 notes 使用。
---

# Daily Papers Fetch

这是 3 步流水线的第 1 步。目标是产出稳定的候选 JSON，不负责写推荐评论。

## 读取配置

先读取：

- `../_shared/user-config.json`
- 如果存在，再读取 `../_shared/user-config.local.json`

需要显式拿到：

- `KEYWORDS`
- `NEGATIVE_KEYWORDS`
- `DOMAIN_BOOST_KEYWORDS`
- `MIN_SCORE`
- `TOP_N`
- `BIORXIV_CATEGORIES`
- `TEMP_DIR`

## 时间范围

`fetch_and_score.py` 支持两个参数：

| 参数 | 含义 |
|------|------|
| `--date YYYY-MM-DD` | 窗口的**结束日期**（含），默认今天 |
| `--days N` | 往前覆盖几天，默认 1（即只取当天） |

实际抓取窗口 = `[--date 那天 - (N-1) 天, --date 那天]`，使用 PubMed 的 `datetype=pdat`（电子发布日期）过滤。

**把日期区间翻译成参数的方法：**

```
"YYYY-MM-DD 到 YYYY-MM-DD 的论文推荐"
  → --date <结束日期> --days <结束日期减起始日期+1天>
```

示例：

| 用户说的区间 | 对应参数 |
|-------------|---------|
| 2025-03-08 到 2025-03-10 | `--date 2025-03-10 --days 3` |
| 2025-02-01 到 2025-02-03 | `--date 2025-02-03 --days 3` |
| 仅 2025-03-10 当天 | `--date 2025-03-10 --days 1` |
| 过去 7 天 | `--days 7`（不加 --date，自动取今天） |

> **注意**：`--date` 控制 PubMed 搜索窗口的结束日期。
> 如果用户说"2025-03-08 的论文"，用 `--date 2025-03-08 --days 1`；
> 如果说"2025-03-08 到 2025-03-10"，用 `--date 2025-03-10 --days 3`。

## 执行顺序

1. 运行抓取与打分：

```powershell
python ..\daily-papers\fetch_and_score.py --date YYYY-MM-DD --days N
```

如有临时关键词：

```powershell
python ..\daily-papers\fetch_and_score.py --date YYYY-MM-DD --days N --keywords "kw1 kw2"
```

2. 确认 `{TEMP_DIR}\daily_papers_top30.json` 已写出。

3. 运行富化：

```powershell
python ..\daily-papers\enrich_papers.py
```

4. 确认 `{TEMP_DIR}\daily_papers_enriched.json` 已写出。

## 当前抓取语义

- PubMed 默认先按日期抓取宽候选池；如果日期窗口总量超过抓取上限，会用 `keywords` 追加一次主题补充检索，避免重要主题论文被 `retmax` 截断漏掉。
- `keywords` 用于本地准入和打分；临时 `--keywords` 会覆盖这组词。
- `negative_keywords` 是硬过滤，命中即拒绝
- `domain_boost_keywords` 只负责加分；默认不是主列表准入条件，除非显式设置 `domain_boost_can_admit=true`
- `keyword_variants` 为 `keywords` / `domain_boost_keywords` / `negative_keywords` 提供配置化别名；代码仍会自动处理大小写、常见单复数、连字符/空格差异

## 输出

完成后汇报：

- PubMed 抓取数
- bioRxiv 抓取数
- 打分后保留数
- 富化后 JSON 路径

## 约束

- 不生成推荐 Markdown
- 不生成论文笔记
- 不做 git 操作

## HARD GUARDRAILS: fetch success criteria

Fetch is successful only when all of these are true:

- `fetch_and_score.py` exits with code 0.
- `{TEMP_DIR}\daily_papers_fetch_status.json` exists and has `status == "success"`.
- The status file `window_end` matches the requested `--date`, and `days` matches the requested window.
- `{TEMP_DIR}\daily_papers_top30.json` is fresh for the same run and is a JSON array.
- `{TEMP_DIR}\daily_papers_filter_audit.json` does not report source-wide network failure in `_source_fetch_errors`.

If PubMed/bioRxiv requests fail with WinError, connection refused, timeout, DNS, HTTP, or similar source access errors, treat fetch as failed. Do not run enrich/review/notes, do not use stale temp JSON, and do not generate a formal empty recommendation page.