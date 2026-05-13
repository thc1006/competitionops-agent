# /review-security

請針對目前 diff 做 security review，特別檢查：

- secrets 是否外洩
- OAuth scopes 是否過寬
- MCP tools 是否暴露低階危險 API
- 是否有 prompt injection / confused deputy 風險
- write actions 是否都有 dry-run 與 approval gate
- audit log 是否完整
