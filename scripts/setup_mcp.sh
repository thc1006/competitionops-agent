#!/usr/bin/env bash
set -euo pipefail

echo "Installing project-scoped CompetitionOps MCP..."
claude mcp add --transport stdio --scope project competitionops-local -- \
  uv run python -m competitionops_mcp.server

echo "Installing project-scoped Playwright MCP..."
claude mcp add --transport stdio --scope project playwright -- \
  npx -y @playwright/mcp@latest

echo "Optional: install Context7 with:"
echo "  npx ctx7 setup --claude"

echo "Optional: install GitHub MCP with:"
echo '  claude mcp add --transport http --scope user github https://api.githubcopilot.com/mcp/ --header "Authorization: Bearer $GITHUB_PAT"'

echo "Optional: install Google Drive / Calendar MCP after configuring Google OAuth:"
echo '  claude mcp add --transport http --scope user --client-id "$GOOGLE_OAUTH_CLIENT_ID" --client-secret --callback-port 8080 google-drive https://drivemcp.googleapis.com/mcp/v1'
echo '  claude mcp add --transport http --scope user --client-id "$GOOGLE_OAUTH_CLIENT_ID" --client-secret --callback-port 8080 google-calendar https://calendarmcp.googleapis.com/mcp/v1'
