---
name: daily-papers-notes
description: |
  论文笔记流程的第 3 步。只为“必读”论文生成完整笔记，回填笔记链接，并按配置刷新 MOC。
---

# Daily Papers Notes

这是 3 步流水线的第 3 步。职责边界要清晰：notes 负责必读笔记、回填和索引，不负责抓取或点评。

## 前置检查

先确认：

- 当日推荐文件存在
- `paper-reader` 可用

如果推荐文件不存在，停止并提示先跑 `daily-papers-review`。

## 读取配置

需要拿到：

- `VAULT_PATH`
- `NOTES_PATH`
- `DAILY_PAPERS_PATH`
- `AUTO_REFRESH_INDEXES`
- `GIT_COMMIT_ENABLED`
- `GIT_PUSH_ENABLED`

## 执行顺序

1. 读取当日推荐文件，只筛出 `必读` 论文。
2. 对每篇必读论文运行：

```powershell
python ..\paper-reader\run_reader.py "{PUBMED_URL_OR_DOI}" --mode standard --prefer-visible-browser
```

> `--prefer-visible-browser` 优先通过 patchright / 浏览器路径尝试获取出版社全文与 PDF，并尽量沿用真实 Chrome profile 中的机构 cookies。只有在无浏览器、CI 或明确只想跑 PMC/API 摘要模式时，才临时加 `--no-playwright`。

3. 如果已有旧笔记但内容明显不完整，不要直接删除；先重命名为 `*.bak-YYYYMMDD-HHMMSS.md`，再重生成。
4. 运行回填：

```powershell
python ..\daily-papers\backfill_links.py --date YYYY-MM-DD
```

5. 如果 `AUTO_REFRESH_INDEXES=true`，刷新：

```powershell
python ..\_shared\generate_paper_mocs.py
python ..\_shared\generate_concept_mocs.py
```

## 质量要求

每篇必读笔记生成后至少检查：

- 文件不是空壳
- 有摘要/背景
- 有方法或结果部分

不满足时重新生成。

## Git 提交（按配置执行）

所有必读笔记写入、backfill 完成、MOC 刷新后，如果 `GIT_COMMIT_ENABLED=true`：

```powershell
cd "{VAULT_PATH}"
git add "PaperNotes\" "DailyPapers\YYYY-MM-DD-论文推荐.md"
git commit -m "notes YYYY-MM-DD: N篇必读笔记写入"
```

如果 `GIT_PUSH_ENABLED=true`，追加执行：

```powershell
git push
```

git 操作失败时只打印警告，不中断流程（笔记已写入）。

## 约束

- 只为 `必读` 生成笔记
- 不手写简化版笔记替代 `paper-reader`
- 不生成推荐评论
