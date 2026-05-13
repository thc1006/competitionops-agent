# 04 — MCP Setup

## Recommended MCP Servers

### P0 — 必裝

1. `competitionops-local`
   - 本 repo 自訂 MCP server。
   - 只暴露安全高階工具。
   - 負責：解析簡章、產生 action plan、dry-run、approve selected actions。

2. Google Drive / Google Calendar official MCP
   - 讀 Drive 檔案。
   - 建立 / 查詢 Calendar events。
   - 注意：官方 Workspace MCP public developer preview 目前主要列出 Gmail、Drive、Calendar、People、Chat。Docs / Sheets 深度寫入應使用本 repo 的 Google API adapter。

3. GitHub MCP
   - 管理 issue / PR / repo context。
   - 用於 Claude Code 開發流程。

4. Playwright MCP
   - 讀競賽網站與表單。
   - 用 structured accessibility snapshot，不依賴 vision screenshot。

5. Context7
   - 查最新套件文件。
   - 用於 FastAPI、Google API client、MCP SDK、LangGraph 等文件查詢。

### P1 — 視需求安裝

6. Plane MCP / Plane API adapter
   - 若 Plane MCP 成熟可直接用。
   - 否則先走 REST API adapter。

7. Kubernetes MCP
   - 僅限 read-only 或受限 service account。
   - 不建議讓 Claude Code 有 `kubectl apply/delete` 權限。

8. Postgres MCP / DBHub
   - 用 read-only DB user 查資料。
   - 不要讓 Claude 直接改 production database。

## Install Commands

先不要盲目全部安裝。建議依序安裝。

### Local CompetitionOps MCP

```bash
claude mcp add --transport stdio --scope project competitionops-local -- \
  uv run python -m competitionops_mcp.server
```

### Playwright MCP

```bash
claude mcp add --transport stdio --scope project playwright -- \
  npx -y @playwright/mcp@latest
```

### Context7

```bash
npx ctx7 setup --claude
```

或手動 HTTP MCP：

```bash
claude mcp add --transport http --scope user context7 https://mcp.context7.com/mcp \
  --header "CONTEXT7_API_KEY: $CONTEXT7_API_KEY"
```

### GitHub MCP

```bash
claude mcp add --transport http --scope user github https://api.githubcopilot.com/mcp/ \
  --header "Authorization: Bearer $GITHUB_PAT"
```

### Google Drive MCP

```bash
claude mcp add --transport http --scope user \
  --client-id "$GOOGLE_OAUTH_CLIENT_ID" \
  --client-secret \
  --callback-port 8080 \
  google-drive https://drivemcp.googleapis.com/mcp/v1
```

### Google Calendar MCP

```bash
claude mcp add --transport http --scope user \
  --client-id "$GOOGLE_OAUTH_CLIENT_ID" \
  --client-secret \
  --callback-port 8080 \
  google-calendar https://calendarmcp.googleapis.com/mcp/v1
```

接著在 Claude Code 內執行：

```text
/mcp
```

完成 OAuth authentication。

## Notes

- `.mcp.json` 可以 check in，但不可放 secret。
- 使用 `${VAR}` 做環境變數展開。
- 對 Google MCP，若 OAuth flow 在 Claude Code 上不穩，先使用官方文件支援明確的 Claude.ai / Claude Desktop 或 Gemini CLI 驗證，再回來接 custom adapter。
- Docs / Sheets 寫入功能應優先由本地 adapter 實作，因為可加入 approval gate、idempotency、audit log。
