#!/usr/bin/env python3

import copy
import json
from functools import lru_cache
from pathlib import Path


DEFAULT_CONFIG = {
    "llm": {
        "api_key": "",
        "base_url": "",
        "model": "gpt-4o-mini",
    },
    "paths": {
        "obsidian_vault": "~/ObsidianVault",
        "paper_notes_folder": "PaperNotes",
        "daily_papers_folder": "DailyPapers",
        "concepts_folder": "_concepts",
        "pdf_figure_folder": "AllPdfFig",
        "temp_folder": "",
        "preference_pdf_library_folder": "",
        "zotero_db": "",
        "zotero_storage": "",
    },
    "sources": {
        "pubmed_enabled": True,
        "biorxiv_enabled": True,
        "ncbi_api_key": "",
        "elsevier_api_key": "",
        "biorxiv_retmax": 300,
        "biorxiv_retmax_total_cap": 3000,
        "biorxiv_categories": [],
        "biorxiv_timeout": 30,
    },
    "daily_papers": {
        "keywords": [],
        "keyword_variants": {},
        "negative_keywords": [],
        "rejected_journals": [],
        "domain_boost_keywords": [],
        "search_retmax": 5000,
        "search_retmax_total_cap": 15000,
        "efetch_workers": 5,
        "min_score": 2,
        "min_quartile": 1,
        "reject_unknown_quartile": False,
        "domain_boost_can_admit": False,
        "top_n": 30,
        "build_review_must_score_min": 4,
        "build_review_show_appendix": False,
        "notes_parallelism": 3,
        "history_days_to_keep": 30,
        "update_profile_from_pdf_library": False,
        "profile_boost_keywords": [],
    },
    "automation": {
        "auto_refresh_indexes": True,
        "git_commit": False,
        "git_push": False,
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


@lru_cache(maxsize=1)
def load_user_config() -> dict:
    config = copy.deepcopy(DEFAULT_CONFIG)
    config_dir = Path(__file__).resolve().parent

    for filename in ("user-config.json", "user-config.local.json"):
        config_path = config_dir / filename
        if not config_path.exists():
            continue
        with config_path.open("r", encoding="utf-8-sig") as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            _deep_merge(config, loaded)

    return config


def local_user_config_path() -> Path:
    return Path(__file__).resolve().parent / "user-config.local.json"


def main_user_config_path() -> Path:
    return Path(__file__).resolve().parent / "user-config.json"


def _expand(path_value: str) -> Path:
    return Path(path_value).expanduser()


def _resolve_path(path_value: str, base: Path | None = None) -> Path:
    path = _expand(str(path_value or ""))
    if path.is_absolute():
        return path
    if base is not None:
        return (base / path).resolve()
    return path.resolve()


def paths_config() -> dict:
    return load_user_config()["paths"]


def sources_config() -> dict:
    return load_user_config().get("sources", {})


def daily_papers_config() -> dict:
    config = copy.deepcopy(load_user_config()["daily_papers"])
    config.update(sources_config())
    config["profile_enabled"] = bool(config.get("update_profile_from_pdf_library", False))
    profile_folder = str(paths_config().get("preference_pdf_library_folder", "") or "").strip()
    config["profile_pdf_folder"] = str(_resolve_path(profile_folder, obsidian_vault_path())) if profile_folder else ""
    return config


def automation_config() -> dict:
    config = load_user_config()["automation"]
    if config.get("git_push") and not config.get("git_commit"):
        config = copy.deepcopy(config)
        config["git_push"] = False
    return config


def obsidian_vault_path() -> Path:
    raw = str(paths_config().get("obsidian_vault", "") or "").strip()
    if not raw or raw.startswith("CHANGE_ME"):
        raise RuntimeError(
            "paths.obsidian_vault 未配置。请打开 _shared/user-config.json，"
            "把 obsidian_vault 改为你自己的 Obsidian 仓库绝对路径，例如："
            r" C:\Users\YourName\Documents\Obsidian\PaperVault"
        )
    return _expand(raw)


def paper_notes_dir() -> Path:
    return _resolve_path(paths_config()["paper_notes_folder"], obsidian_vault_path())


def daily_papers_dir() -> Path:
    return _resolve_path(paths_config()["daily_papers_folder"], obsidian_vault_path())


def concepts_dir() -> Path:
    return _resolve_path(paths_config()["concepts_folder"], paper_notes_dir())


def pdf_picture_root_dir() -> Path:
    folder = paths_config().get("pdf_figure_folder") or paths_config().get("pdf_picture_root") or "AllPdfFig"
    return _resolve_path(folder, obsidian_vault_path())


def zotero_db_path() -> Path:
    return _expand(paths_config()["zotero_db"])


def zotero_storage_dir() -> Path:
    return _expand(paths_config()["zotero_storage"])


def ncbi_api_key() -> str:
    """Return NCBI E-utilities API key, or empty string if not configured.

    With a key: 10 requests/second allowed.
    Without a key: 3 requests/second (NCBI limit for anonymous).
    Register free at https://www.ncbi.nlm.nih.gov/account/
    """
    config = load_user_config()
    sources = config.get("sources", {}) if isinstance(config.get("sources", {}), dict) else {}
    return str(sources.get("ncbi_api_key", "") or "").strip()


def elsevier_api_key() -> str:
    config = load_user_config()
    sources = config.get("sources", {}) if isinstance(config.get("sources", {}), dict) else {}
    if isinstance(sources, dict):
        value = str(sources.get("elsevier_api_key", "") or "").strip()
        if value:
            return value
    elsevier = config.get("elsevier", {}) if isinstance(config.get("elsevier", {}), dict) else {}
    if isinstance(elsevier, dict):
        return str(elsevier.get("api_key", "") or "").strip()
    return ""


def auto_refresh_indexes_enabled() -> bool:
    return bool(automation_config()["auto_refresh_indexes"])


def git_commit_enabled() -> bool:
    return bool(automation_config()["git_commit"])


def git_push_enabled() -> bool:
    return bool(automation_config()["git_push"])


def notes_parallelism() -> int:
    return int(daily_papers_config().get("notes_parallelism", 3))


def set_daily_papers_profile_update_flag(enabled: bool) -> bool:
    config_path = local_user_config_path()
    if not config_path.exists():
        return False
    try:
        with config_path.open("r", encoding="utf-8-sig") as f:
            loaded = json.load(f)
        if not isinstance(loaded, dict):
            return False
        daily = loaded.setdefault("daily_papers", {})
        if not isinstance(daily, dict):
            return False
        daily["update_profile_from_pdf_library"] = bool(enabled)
        config_path.write_text(json.dumps(loaded, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        load_user_config.cache_clear()
        return True
    except Exception:
        return False


# ── Temp directory (Windows/Linux compatible, mirrors original skill) ────────

def _get_temp_dir() -> Path:
    """Configured temp directory, falling back to a platform default.

    paths.temp_folder may be absolute or relative to obsidian_vault.
    Fallbacks:
    Windows : ~/tmp/
    Linux/Mac: /tmp/
    """
    import sys as _sys
    configured = str(paths_config().get("temp_folder", "") or "").strip()
    if configured:
        tmp = _resolve_path(configured, obsidian_vault_path())
    elif _sys.platform == "win32":
        tmp = Path.home() / "tmp"
    else:
        tmp = Path("/tmp")
    tmp.mkdir(parents=True, exist_ok=True)
    return tmp


def temp_dir() -> Path:
    """Return platform-appropriate temp directory."""
    return _get_temp_dir()


def temp_file_path(filename: str) -> Path:
    """Return full path for a named temp file.

    Usage:
        top30  = temp_file_path('daily_papers_top30.json')
        enriched = temp_file_path('daily_papers_enriched.json')
    """
    return temp_dir() / filename


# ── LLM config accessor ──────────────────────────────────────────────────────

def llm_config() -> dict:
    """Return the 'llm' section from user config, or empty dict."""
    cfg = load_user_config()
    llm = cfg.get("llm", {})
    return llm if isinstance(llm, dict) else {}


def ensure_vault_dirs() -> None:
    """Create all configured vault output directories if they don't exist yet.

    Call once at the start of any pipeline stage that writes to the vault.
    Failures are silently ignored so a misconfigured optional path never
    aborts the whole run.
    """
    for dir_fn in (daily_papers_dir, paper_notes_dir, pdf_picture_root_dir, concepts_dir):
        try:
            dir_fn().mkdir(parents=True, exist_ok=True)
        except Exception:
            pass


def history_days_to_keep() -> int:
    """How many days of paper history to retain for deduplication (default 30)."""
    val = load_user_config()["daily_papers"].get("history_days_to_keep", 30)
    try:
        return max(1, int(val))
    except (TypeError, ValueError):
        return 30


def set_daily_papers_profile_fields(domain_boost_keywords: list[str]) -> bool:
    """Write the auto-extracted profile boost keywords back to user-config.json.

    Stored as ``profile_boost_keywords`` immediately after ``domain_boost_keywords``
    so users can see what the library profile extracted.  These are read-only from
    the user's perspective — they are overwritten each time the profile is rebuilt.
    """
    config_path = main_user_config_path()
    if not config_path.exists():
        return False
    try:
        with config_path.open("r", encoding="utf-8-sig") as f:
            loaded = json.load(f)
        if not isinstance(loaded, dict):
            return False
        daily = loaded.setdefault("daily_papers", {})
        if not isinstance(daily, dict):
            return False
        ordered_daily: dict = {}
        for key, value in daily.items():
            if key in {"profile_keywords", "profile_domain_boost_keywords", "profile_boost_keywords"}:
                continue  # drop old / stale fields
            ordered_daily[key] = value
            if key == "domain_boost_keywords":
                ordered_daily["profile_boost_keywords"] = list(domain_boost_keywords or [])
        if "profile_boost_keywords" not in ordered_daily:
            ordered_daily["profile_boost_keywords"] = list(domain_boost_keywords or [])
        loaded["daily_papers"] = ordered_daily
        config_path.write_text(json.dumps(loaded, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        load_user_config.cache_clear()
        return True
    except Exception:
        return False
