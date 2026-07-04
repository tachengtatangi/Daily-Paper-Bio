# Shared Daily Paper Bio Files

This directory contains shared configuration and helper modules used by the Daily Paper Bio Codex skills.

For installation, configuration, and usage instructions, see:

- 中文: [../README.md](../README.md)
- English: [../README.en.md](../README.en.md)

Important files:

- `user-config.json`: public, editable main configuration template.
- `user-config.local.json.example`: copy this to `user-config.local.json` for private API keys.
- `user-config.local.json`: local private override file; ignored by git.
- `cas_quartiles.py`: CAS journal quartile lookup helper.
- `data/cas_quartiles_2025.xlsx`: redistributable CAS quartile workbook used by the optional PubMed quartile filter.
- `generate_paper_mocs.py` / `generate_concept_mocs.py`: Obsidian MOC refresh helpers.

Do not commit real API keys, cookies, or personal machine paths.