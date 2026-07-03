[中文说明](README.zh-CN.md) | English

# Daily Paper Bio

Daily Paper Bio is a Codex skill suite for life-science paper triage and Obsidian note taking. It fetches PubMed and bioRxiv papers, scores them against your research interests, writes a reviewer-style recommendation draft, and generates structured notes for must-read papers.

This repository is a set of Codex skills, not a Python package. Install it by copying the skill directories into your Codex skills folder.

## What It Does

- Fetches recent papers from PubMed and bioRxiv.
- Scores papers with configurable keywords, negative keywords, journal filters, and optional CAS journal quartiles.
- Builds daily recommendation drafts with `must-read`, `worth reading`, and `skip` buckets.
- Lets the Codex agent write the final reviewer-style comments before publishing to Obsidian.
- Generates full notes only for must-read papers through `paper-reader`.
- Reads PubMed URLs, DOI links, publisher pages, local PDFs, and bioRxiv PDFs.
- Uses MinerU for PDF text extraction when available, with fallback to local PDF parsers.
- Keeps API keys and local machine paths in an ignored local config file.

## Repository Layout

| Path | Purpose |
|---|---|
| `_shared/` | Shared config loader, Obsidian paths, MOC builders, optional data files. |
| `daily-papers/` | Fetching, scoring, enrichment, draft generation, history, cleanup. |
| `daily-papers-fetch/` | Codex skill wrapper for the fetch stage. |
| `daily-papers-review/` | Codex skill wrapper for the review/finalization stage. |
| `daily-papers-notes/` | Codex skill wrapper for must-read note generation. |
| `paper-reader/` | Single-paper reading and note generation. |
| `generate-mocs/` | Obsidian MOC regeneration skill. |
| `playwright/` | Browser-control helper skill and references. |

## Requirements

- Python 3.10 or newer.
- Codex with local skill support.
- An Obsidian vault.
- Optional: `patchright` Chromium for publisher pages that require a browser session.
- Optional: MinerU on `PATH` for better PDF text extraction.
- Optional: NCBI and Elsevier API keys.

Install Python dependencies from the repository root:

```powershell
python -m pip install -r daily-papers/requirements.txt
```

Install the optional browser runtime:

```powershell
patchright install chromium
```

MinerU is optional. If `mineru.exe` or `mineru` is found on `PATH`, `paper-reader` tries it first for PDF text extraction. The effective command is:

```powershell
mineru -p "<pdf_path>" -o "<output_root>" --method auto --backend pipeline
```

Non-Markdown MinerU artifacts are cleaned after a successful note write; the generated Markdown can be kept for inspection.

## Installation

Clone the repository anywhere outside the Codex runtime folder:

```powershell
git clone https://github.com/tachengtatangi/Daily-Paper-Bio.git
cd Daily-Paper-Bio
```

Then copy the skill folders into Codex:

```powershell
.\sync_to_codex.bat
```

The sync script preserves existing `_shared/user-config.json` and `_shared/user-config.local.json` in your Codex runtime, so updating the skills will not overwrite your local paths or API keys.

Manual Windows target:

```text
%USERPROFILE%\.codex\skills\
```

Manual Unix-like target:

```text
~/.codex/skills/
```

Copy these directories directly under `skills/`: `_shared`, `daily-papers`, `daily-papers-fetch`, `daily-papers-review`, `daily-papers-notes`, `paper-reader`, `generate-mocs`, and `playwright`.

## Configuration

Edit `_shared/user-config.json` after installing the skills. At minimum, set your Obsidian vault path and research keywords:

```json
{
  "paths": {
    "obsidian_vault": "C:\\Users\\YourName\\Documents\\Obsidian\\PaperVault"
  },
  "daily_papers": {
    "keywords": ["genome evolution", "comparative genomics"],
    "domain_boost_keywords": ["mammalian", "sensory receptor"],
    "negative_keywords": ["crop", "bacteria"]
  }
}
```

Put secrets in `_shared/user-config.local.json`, not in `user-config.json`:

```powershell
copy _shared\user-config.local.json.example _shared\user-config.local.json
```

Then fill only the keys you need:

```json
{
  "sources": {
    "ncbi_api_key": "",
    "elsevier_api_key": ""
  },
  "llm": {
    "api_key": "",
    "base_url": "",
    "model": "gpt-4o-mini"
  }
}
```

`user-config.local.json` is ignored by git. Do not commit real API keys, browser cookies, or local private paths.

## CAS Quartile Data

This repository includes a redistributable CAS journal quartile workbook used by the optional PubMed journal filter:

```text
_shared/data/cas_quartiles_2025.xlsx
```

If you remove or replace the workbook, the code still runs. When the file is missing, CAS lookup returns no match and quartile filtering should be loosened by setting `daily_papers.min_quartile` to `4` during debugging.

## Usage in Codex

Ask Codex in natural language:

```text
今日论文推荐
过去3天论文推荐
过去一周论文推荐
2026-06-13 到 2026-06-16 论文推荐
今日论文推荐，关键词 convergent evolution taste receptor
```

The high-level skill runs three stages:

1. `daily-papers-fetch`: fetch, score, deduplicate, enrich, and write temporary JSON.
2. `daily-papers-review`: build a structured draft, let the agent write comments, publish the final Markdown, and update history.
3. `daily-papers-notes`: generate notes only for must-read papers, backfill links, and refresh MOCs.

For a safe smoke test that does not publish the final Obsidian recommendation page:

```powershell
python daily-papers\run_pipeline.py --date 2026-07-03 --days 3 --notes-limit 0
```

This writes a draft under your configured temp folder. Final publishing is intentionally left to the review skill so the agent must replace all `TODO_AGENT` placeholders with real comments.

## Single-Paper Reading

Examples:

```text
读一下这篇论文 https://pubmed.ncbi.nlm.nih.gov/41803465/
快速看一下这篇论文 10.1101/2024.01.01.123456
批判性分析这篇论文 https://example.com/article.pdf
```

For a local PDF without network metadata enrichment:

```powershell
python paper-reader\run_reader.py "D:\papers\paper.pdf" --mode standard --local-only
```

## GitHub Setup Notes

When creating a new GitHub repository for an existing local repo, leave GitHub's `Add README`, `.gitignore`, and `License` options off. This repository already contains those files locally; letting GitHub create them would make a separate first commit and force you to merge unrelated histories.

After creating an empty GitHub repo, push from this local repository:

```powershell
cd /d path\to\Daily-Paper-Bio
git remote rename origin local-origin
git remote add origin https://github.com/tachengtatangi/Daily-Paper-Bio.git
git branch -M main
git push -u origin main
```

## Troubleshooting

| Symptom | What to check |
|---|---|
| No papers returned | Loosen `min_score`, set `min_quartile` to `4`, and inspect `daily_papers_filter_audit.json`. |
| PubMed is slow or rate-limited | Add `sources.ncbi_api_key` in `user-config.local.json`. |
| Must-read note only has an abstract | Install browser support, configure Elsevier API if needed, or provide a local PDF. |
| PDF text is poor | Install MinerU and make sure `mineru` is on `PATH`. |
| Final recommendation still has `TODO_AGENT` | Stop and rerun the review step; drafts are not publishable final pages. |

## License

The project is licensed under Apache-2.0. See [LICENSE](LICENSE).

Some bundled references and browser helper assets retain their own notices, including the files under `playwright/`.