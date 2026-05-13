# 09 — Research Sources

本檔記錄建立專案骨架時參考的公開來源與日期。若 Claude Code 後續要更新，請先重新查證。

## Claude Code / Anthropic

- Claude Code memory：CLAUDE.md files and auto memory are loaded at session start; target under 200 lines for better adherence.
  - https://code.claude.com/docs/en/memory
- Claude Code settings：settings scopes, `.claude/settings.json`, `.mcp.json`, permission allow/deny, secret file deny examples.
  - https://code.claude.com/docs/en/settings
- Claude Code MCP：HTTP MCP, stdio MCP, project scope, OAuth authentication, `claude mcp add`, `add-json`.
  - https://code.claude.com/docs/en/mcp
- Claude Code monitoring：OpenTelemetry telemetry export.
  - https://code.claude.com/docs/en/monitoring-usage

## MCP

- MCP specification 2025-11-25：resources、prompts、tools、security and trust principles.
  - https://modelcontextprotocol.io/specification/2025-11-25
- MCP Security Best Practices：confused deputy、token passthrough、SSRF、session hijacking、scope minimization.
  - https://modelcontextprotocol.io/docs/tutorials/security/security_best_practices

## Google Workspace / Google API

- Google Workspace MCP servers：public developer preview, Gmail/Drive/Calendar/Chat/People MCP servers, OAuth, tool list, prompt injection warning.
  - https://developers.google.com/workspace/guides/configure-mcp-servers
- Google Workspace Updates, 2026-05-01：Workspace MCP public developer preview.
  - https://workspaceupdates.googleblog.com/2026/05/agent-tools-and-security-updates-for-workspace-developers.html
- Google Drive API move files：use `files.update` with `addParents` / `removeParents`.
  - https://developers.google.com/workspace/drive/api/guides/folder
- Google Docs API batchUpdate：applies one or more updates atomically.
  - https://developers.google.com/workspace/docs/api/reference/rest/v1/documents/batchUpdate
- Google Sheets API values.append / values.update.
  - https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets.values/append
  - https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets.values/update
- Google Calendar API create events：use `events.insert`.
  - https://developers.google.com/workspace/calendar/api/guides/create-events

## PM / workflow stack

- Plane developer docs：REST API, webhooks, OAuth apps, MCP server.
  - https://developers.plane.so/
- Windmill：open-source workflow engine, scripts, flows, webhooks, UIs.
  - https://www.windmill.dev/docs/intro
- LangGraph：durable execution and human-in-the-loop.
  - https://docs.langchain.com/oss/python/langgraph/overview
- FastAPI testing dependency overrides.
  - https://fastapi.tiangolo.com/advanced/testing-dependencies/

## MCP servers

- Playwright MCP official docs.
  - https://playwright.dev/docs/getting-started-mcp
- GitHub MCP Server.
  - https://github.com/github/github-mcp-server
- Context7.
  - https://github.com/upstash/context7
- Kubernetes MCP server example.
  - https://github.com/containers/kubernetes-mcp-server
