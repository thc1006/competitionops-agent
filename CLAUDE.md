# CLAUDE.md — CompetitionOps Agent

你是這個 repo 的 Claude Code 開發代理。你的任務是用 SDD + TDD + Agile 的方式，逐步實作「新創競賽 PM 自動化平台」。

## 專案定位

CompetitionOps Agent 是一個本地端優先的 PM 自動化工具。它串接 Google Drive、Google Docs、Google Sheets、Google Calendar、Plane / OpenProject 與自訂 MCP server，幫 PM 從比賽簡章產生作戰計畫、任務拆分、行事曆 checkpoint 與文件骨架。

## 絕對規則

1. 不要使用 Notion，也不要新增 Notion 相關依賴。
2. 不要把任何 secrets commit 進 repo。
3. 所有 Google / Plane / Calendar 寫入動作都先做 dry-run。
4. 高風險動作必須通過 approval gate：
   - 移動或刪除 Drive 檔案
   - 變更 Drive 權限
   - 建立或更新 Calendar event
   - 寫入 Google Docs / Sheets
   - 建立或指派 Plane issue
   - 寄出或草擬對外 email
5. 實作任何功能前，先讀 `docs/02_spec.md` 與 `docs/08_api_contracts.md`。
6. 任何新增功能必須先新增或更新測試。
7. 不要為了通過測試硬編假邏輯；測試要描述真實 PM workflow。
8. API schema 以 Pydantic model 為 source of truth。
9. 所有外部 integration 先以 interface / port 抽象化，再提供 mock 與 real adapter。
10. 回覆使用繁體中文，程式碼與 API 命名使用英文。

## 開發命令

```bash
uv sync
uv run pytest
uv run ruff check .
uv run mypy src
uv run uvicorn competitionops.main:app --reload
uv run python -m competitionops_mcp.server
```

## TDD 流程

每個 backlog item 必須照這個順序：

1. 寫 failing test。
2. 執行 `uv run pytest` 確認失敗原因符合預期。
3. 實作最小可行程式碼。
4. 執行 `uv run pytest`。
5. 執行 `uv run ruff check .` 與 `uv run mypy src`。
6. 更新文件與 backlog 狀態。
7. 寫出 concise implementation note。

## SDD 流程

需求先進文件，再進程式碼：

1. `docs/01_product_requirements.md`：產品需求。
2. `docs/02_spec.md`：規格與 acceptance criteria。
3. `docs/08_api_contracts.md`：API contract。
4. `tests/`：測試。
5. `src/`：實作。

## MCP 使用規則

- 若需要最新套件或 API 文件，先用 Context7 或 web research MCP。
- 若需要操作 GitHub issue / PR，使用 GitHub MCP。
- 若需要讀 Google Drive / Calendar，優先使用官方 Google Workspace MCP。
- Docs / Sheets 寫入功能若官方 MCP 不完整，使用本 repo 的 `competitionops_mcp` 高階工具。
- 若需要抓競賽網站，使用 Playwright MCP 或 Crawl4AI adapter。
- 若需要操作 Kubernetes，只能用 read-only kubeconfig 或受限 service account。

## 架構原則

- Hexagonal architecture：domain 不依賴 Google / Plane SDK。
- Dry-run first：任何 integration 預設不改外部狀態。
- Idempotent：建立資料夾、文件、calendar event、task 時要能重跑。
- Observable：每個 action 都要產生 audit log。
- Human-in-the-loop：PM 可以修改 action plan 後再 approve。

## 不要做

- 不要直接刪除任何外部資料。
- 不要把個人 Google account 當成 multi-user admin 權限。
- 不要用 service account 模擬 Workspace domain-wide delegation，除非使用者明確提供 Workspace 管理員條件。
- 不要自動提交比賽文件。
- 不要自動寄信給主辦單位。
