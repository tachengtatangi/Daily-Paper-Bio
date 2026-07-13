---
name: daily-papers
description: |
  生物方向每日论文推荐总入口。用户说“今日论文推荐”“过去3天论文推荐”“过去一周论文推荐”“最近7天论文”时使用。
  这个 skill 负责串联 3 个独立步骤：fetch → review → notes。
---

# Daily Papers

这是面向用户的一句话入口。默认不要直接跑内部一键脚本；要按原版架构顺序串联独立 skill。

## 触发场景

- `今日论文推荐`
- `过去3天论文推荐`
- `过去一周论文推荐`
- `最近7天论文`

## 执行原则

1. 先解析时间范围。
2. 然后调用 `daily-papers-fetch`。
3. fetch 完成后调用 `daily-papers-review`。
4. review 完成后调用 `daily-papers-notes`。
5. 最后向用户汇报：
   - 推荐文件路径
   - 必读 / 值得看 / 可跳过各多少篇
   - 生成了多少篇必读笔记
   - 是否刷新了索引

## 时间范围

- `今日论文推荐` / `今日论文` -> `--days 1`
- `过去3天` / `过去三天` / `最近三天` / `近三天` -> `--days 3`
- `过去一周` / `最近7天` / `最近七天` / `过去七天` / `近7天` / `这周` -> `--days 7`
- `过去两周` -> `--days 14`
- `过去一个月` / `最近30天` -> `--days 30`

如果用户给出明确闭区间，例如 `2025-3-21 到 2025-3-24 论文推荐`，先规范化日期为 `YYYY-MM-DD`，再转换为：

```powershell
--date 2025-03-24 --days 4
```

`--date` 永远表示窗口结束日期（含当天），`--days` 是闭区间天数。

## 临时关键词

如果用户明确给了临时关键词，例如：

`今日论文推荐，关键词 convergent evolution taste receptor`

则把这组词作为本次 `--keywords` 临时覆盖传给 fetch 步骤。当前抓取语义是：PubMed 先按日期抓宽候选池，再本地按关键词筛选和打分；当宽候选池触发 retmax 截断时，会用这组关键词追加补充检索。bioRxiv 先按日期和配置中的 `biorxiv_categories` 取候选，再本地筛选和打分。

## 约束

- 不要把 `run_pipeline.py` 当作默认入口；它只保留给调试或批处理。
- 不要要求用户手动拆分三步；这是 skill 内部的组织方式。
- review 阶段的评论由 Agent 自己写，不依赖 Python 批量生成点评。

## HARD GUARDRAILS: pipeline success

The pipeline must not interpret network failure as "no papers". Continue from fetch to review only when fetch wrote a successful `{TEMP_DIR}\daily_papers_fetch_status.json` for the requested date/window. If fetch fails, stop and report the failure; never use stale `daily_papers_enriched.json` and never publish an empty formal recommendation page.
If every source fetch succeeds but top_count is zero, this is a valid no-match result rather than a pipeline failure. Enrich must write a fresh empty daily_papers_enriched.json, review must generate the dedicated non-empty no-match report, notes must be skipped, and the final summary must state that zero papers met the configured filters. Do not relax filters merely to avoid a zero result.
