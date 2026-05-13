# AGENTS.md — Agent Operating System

本檔案給 Claude Code、Codex、Gemini CLI 或其他 coding agent 使用。Claude Code 主要讀 `CLAUDE.md`，但本檔提供跨 agent 的共同規範。

## 角色分工

### PM Analyst Agent

負責：
- 解析比賽簡章。
- 抽出 deadline、deliverables、eligibility、rubric、anonymous rules。
- 建立 WBS、RACI、風險矩陣。
- 產出 dry-run action plan。

輸入：
- PDF / Google Doc / 網頁文字。
- 團隊能力表。
- PM 自然語言需求。

輸出：
- `CompetitionBrief`
- `ActionPlan`
- `TaskDraft[]`
- `CalendarEventDraft[]`

### Backend Engineer Agent

負責：
- FastAPI endpoint。
- Pydantic schema。
- Google / Plane adapter。
- MCP server tools。
- 測試與 CI。

### Security Reviewer Agent

負責：
- OAuth scopes 最小化。
- MCP prompt injection / confused deputy 風險。
- secrets hygiene。
- approval gate 規則。
- audit log schema。

## 交付標準

每個功能合併前必須完成：

```bash
uv run pytest
uv run ruff check .
uv run mypy src
```

並更新：
- `docs/02_spec.md`
- `docs/07_backlog.md`
- `docs/08_api_contracts.md`（如果 API 有變）

## Branch / Commit 規則

建議 commit prefix：

- `spec:` 規格更新
- `test:` 測試
- `feat:` 功能
- `fix:` 修正
- `security:` 安全修正
- `docs:` 文件
- `infra:` Docker/K8s/CI

## Definition of Done

- 測試通過。
- 有 dry-run 模式。
- 有 approval gate 或清楚標示 read-only。
- 有 audit log。
- 沒有 secrets。
- 文件已更新。
