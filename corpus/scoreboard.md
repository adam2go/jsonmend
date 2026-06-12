# Scoreboard

Pass rates over the conformance corpus (see [README.md](README.md) for pass criteria).

Versions: jsonmend 0.1.1, json_repair 0.60.1 (PyPI `json-repair`), jsonrepair 3.14.0 (npm). Regenerate with `python tools/referee.py --write`.

| category | cases | jsonmend | json_repair | jsonrepair |
|---|---|---|---|---|
| adversarial | 5 | 5 | 4 | 5 |
| ai-formats | 25 | 25 | 19 | 11 |
| commas | 24 | 24 | 20 | 22 |
| comments | 15 | 15 | 13 | 13 |
| concat | 14 | 14 | 5 | 10 |
| degraded | 72 | 72 | 56 | 36 |
| escapes | 11 | 11 | 7 | 11 |
| keys | 21 | 21 | 17 | 16 |
| literals | 24 | 24 | 11 | 20 |
| llm-output | 20 | 20 | 17 | 8 |
| markdown-fence | 18 | 18 | 18 | 11 |
| numbers | 22 | 22 | 13 | 14 |
| quotes | 33 | 33 | 24 | 32 |
| structure | 28 | 28 | 24 | 16 |
| truncation | 28 | 28 | 15 | 25 |
| truncation-sweep | 88 | 88 | 41 | 80 |
| unicode | 16 | 16 | 14 | 14 |
| unrecoverable | 5 | 5 | 5 | 5 |
| valid | 16 | 16 | 16 | 16 |
| **total** | **485** | **485 (100.0%)** | **339 (69.9%)** | **365 (75.3%)** |
