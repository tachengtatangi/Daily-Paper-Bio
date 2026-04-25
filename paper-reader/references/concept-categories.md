# 概念自动归类规则

概念库位置：`{CONCEPTS_PATH}`

先查看 `{CONCEPTS_PATH}` 下已有子目录，再按下表分类。目录名偏向生物科学笔记，不沿用原版 CS / 机器人分类。

| 子目录 | 归类标准 | 示例 |
|--------|----------|------|
| `1-比较基因组学` | 跨物种基因组比较、基因组收敛、基因组分化、共线性 | comparative genomics, genomic convergence, synteny |
| `2-进化生物学` | 适应、趋同进化、适应辐射、物种形成、性状演化 | convergent evolution, adaptive radiation, speciation |
| `3-分子适应与选择` | 正选择、分子收敛、加速区、谱系特异性变化 | positive selection, molecular convergence, accelerated region |
| `4-感官与受体演化` | 嗅觉、味觉、视觉、听觉、感官受体谱系 | olfactory receptor, taste receptor, opsin, echolocation |
| `5-基因家族与新基因` | 基因家族扩张/收缩、假基因、de novo gene、CNV | gene family evolution, de novo gene, pseudogene |
| `6-调控与表观组` | 增强子、非编码元件、染色质可及性、cis-regulatory evolution | enhancer evolution, conserved noncoding element, ATAC-seq |
| `7-免疫与宿主适应` | 免疫基因组、病毒耐受、宿主-病原互作 | comparative immunogenomics, bat immunity, immune tolerance |
| `8-组学方法` | 转录组、单细胞、多组学、群体基因组方法 | RNA-seq, comparative transcriptomics, pangenome |
| `9-数据资源与数据库` | 数据集、数据库、资源型论文、注释资源 | genome assembly, annotation database, atlas |
| `10-统计与计算方法` | 系统发育模型、选择检验、比较方法、可复用工具 | phylogenomics, dN/dS, GWAS, enrichment |
| `0-uncategorized` | **仅在完全无法判断时**才用，应尽量避免 | — |

## 概念笔记模板

```markdown
---
type: concept
aliases: [中文别名, 英文别名]
---

# 概念名称

## 定义
{一句话定义}

## 生物学问题
{这个概念主要解释什么生物现象或机制}

## 常用证据
- ...
- ...

## 代表工作
- [[Paper1]]: ...
- [[Paper2]]: ...

## 相关概念
- [[相关概念1]]
- [[相关概念2]]
```
