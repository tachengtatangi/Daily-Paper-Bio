@echo off
:: sync_to_codex.bat — 把 Project 目录的 skill 同步到 codex 运行目录
:: 用法: 在 dailypaper-skills\ 下双击运行，或在修改后调用

set SRC=E:\Claude\Project\dailypaper-skills
set DST=C:\Users\10312\.codex\skills

echo [sync] %SRC% → %DST%

:: paper-reader
xcopy /Y /Q "%SRC%\paper-reader\run_reader.py"   "%DST%\paper-reader\"
xcopy /Y /Q "%SRC%\paper-reader\pdf_fetcher.py"  "%DST%\paper-reader\"
xcopy /Y /Q "%SRC%\paper-reader\paper_daemon.py" "%DST%\paper-reader\"
xcopy /Y /Q "%SRC%\paper-reader\SKILL.md"        "%DST%\paper-reader\"
xcopy /Y /E /Q "%SRC%\paper-reader\assets\"      "%DST%\paper-reader\assets\"
xcopy /Y /E /Q "%SRC%\paper-reader\references\"  "%DST%\paper-reader\references\"

:: _shared
xcopy /Y /Q "%SRC%\_shared\user-config.json"         "%DST%\_shared\"
xcopy /Y /Q "%SRC%\_shared\user_config.py"            "%DST%\_shared\"
xcopy /Y /Q "%SRC%\_shared\cas_quartiles.py"          "%DST%\_shared\"
xcopy /Y /Q "%SRC%\_shared\moc_builder.py"            "%DST%\_shared\"
xcopy /Y /Q "%SRC%\_shared\generate_concept_mocs.py"  "%DST%\_shared\"
xcopy /Y /Q "%SRC%\_shared\generate_paper_mocs.py"    "%DST%\_shared\"
xcopy /Y /E /Q "%SRC%\_shared\data\"                  "%DST%\_shared\data\"

:: daily-papers
xcopy /Y /E /Q "%SRC%\daily-papers\"     "%DST%\daily-papers\"
xcopy /Y /E /Q "%SRC%\daily-papers-fetch\"  "%DST%\daily-papers-fetch\"
xcopy /Y /E /Q "%SRC%\daily-papers-notes\"  "%DST%\daily-papers-notes\"
xcopy /Y /E /Q "%SRC%\daily-papers-review\" "%DST%\daily-papers-review\"
xcopy /Y /E /Q "%SRC%\generate-mocs\"   "%DST%\generate-mocs\"

echo [sync] Done.
pause
