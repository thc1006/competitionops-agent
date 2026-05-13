# CompetitionOps Agent

本專案骨架是給 **新創團隊參加競賽** 使用的本地端 AI PM 自動化平台。核心目標是讓 PM 用自然語言與 LLM 互動後，系統可以：

1. 讀取 Google Drive 裡的比賽簡章、PDF、網頁或附件。
2. 抽取 deadline、應繳資料、資格限制、評分 rubric、匿名規則。
3. 自動產生 Google Docs 提案討論事項與文件大綱。
4. 自動更新 Google Sheets 比賽追蹤表與任務矩陣。
5. 自動建立 Google Calendar deadline / checkpoint。
6. 自動建立 Plane / OpenProject Kanban 任務。
7. 所有高風險寫入動作先走 PM approval gate。

> 原則：不用 Notion。Google Drive / Docs / Sheets / Calendar 是協作層；Plane 是任務執行層；Windmill 是 workflow automation；Claude Code 是工程開發與 MCP glue code 代理。

## 目前骨架內容

```text
.
├── AGENTS.md
├── CLAUDE.md
├── README.md
├── .claude/
│   ├── settings.json
│   ├── commands/
│   │   ├── implement-next.md
│   │   ├── review-security.md
│   │   └── test-first.md
│   └── agents/
│       ├── backend-engineer.md
│       ├── pm-analyst.md
│       └── security-reviewer.md
├── .mcp.example.json
├── docs/
│   ├── 00_session_recap.md
│   ├── 01_product_requirements.md
│   ├── 02_spec.md
│   ├── 03_architecture.md
│   ├── 04_mcp_setup.md
│   ├── 05_security_oauth.md
│   ├── 06_agile_sdd_tdd_workflow.md
│   ├── 07_backlog.md
│   ├── 08_api_contracts.md
│   └── 09_research_sources.md
├── src/
│   ├── competitionops/
│   └── competitionops_mcp/
├── tests/
├── scripts/
├── infra/
│   ├── docker/
│   └── k8s/
└── pyproject.toml
```

## 快速開始

```bash
# 1) 建議使用 uv
curl -LsSf https://astral.sh/uv/install.sh | sh

# 2) 安裝依賴
uv sync

# 3) 跑測試
uv run pytest

# 4) 啟動 API
uv run uvicorn competitionops.main:app --reload

# 5) 啟動本地 MCP server（給 Claude Code）
uv run python -m competitionops_mcp.server
```

## Claude Code 開發流程

```bash
cd competitionops-agent-skeleton
claude
```

在 Claude Code 裡先要求：

```text
請先閱讀 CLAUDE.md、AGENTS.md、docs/02_spec.md、docs/06_agile_sdd_tdd_workflow.md。
接著用 TDD 實作下一個 backlog item。先寫 failing test，再實作最小可行程式碼，最後更新 docs/07_backlog.md。
```

## 安全原則

- `.env`、token、OAuth client secret、Google credential JSON 不可進 git。
- Google Drive 檔案移動、權限分享、Calendar 邀請、Docs 寫入、Sheets 覆寫，都必須先產生 dry-run action plan。
- PM approve 後才執行 write actions。
- MCP server 只能暴露高階 business action，不暴露危險低階 API，例如 `drive.files.delete`、`permissions.create`。
