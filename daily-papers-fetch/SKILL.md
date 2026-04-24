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

按上游传入的 `--days N` 运行。未指定时默认当天。

## 执行顺序

1. 运行抓取与打分：

```powershell
python ..\daily-papers\fetch_and_score.py --days N
```

如有临时关键词：

```powershell
python ..\daily-papers\fetch_and_score.py --days N --keywords "kw1 kw2"
```

2. 确认 `{TEMP_DIR}\daily_papers_top30.json` 已写出。

3. 运行富化：

```powershell
python ..\daily-papers\enrich_papers.py
```

4. 确认 `{TEMP_DIR}\daily_papers_enriched.json` 已写出。

## 当前抓取语义

- 只有一套 `keywords`
- 这组词同时用于 PubMed 远程检索和本地打分
- `negative_keywords` 是硬过滤，命中即拒绝
- `domain_boost_keywords` 只负责加分，不是主列表准入条件

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
