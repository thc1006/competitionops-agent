# /test-first

請依照 TDD 流程實作目前任務：

1. 先讀 `docs/02_spec.md` 和 `docs/07_backlog.md`。
2. 選擇下一個未完成 P0 或 P1 backlog item。
3. 先寫 failing test。
4. 執行 `uv run pytest`。
5. 實作最小可行程式碼。
6. 再執行 `uv run pytest`、`uv run ruff check .`、`uv run mypy src`。
7. 更新 `docs/07_backlog.md`。
8. 回報：測試、變更、剩餘風險。
