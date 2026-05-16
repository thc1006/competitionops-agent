"""StaticTokenProvider — operator-wired bearer, no refresh.

Preserves the pre-refresh-port behaviour: an operator pastes a
short-lived access token (typically minted via the OAuth 2.0
Playground) into ``GOOGLE_OAUTH_ACCESS_TOKEN`` and re-supplies it once
it expires. ``GoogleOAuthTokenProvider`` is the zero-maintenance
alternative for operators who wire a refresh token instead.
"""

from __future__ import annotations


class StaticTokenProvider:
    """Returns a fixed access token verbatim. Satisfies ``TokenProvider``."""

    def __init__(self, access_token: str) -> None:
        self._access_token = access_token

    async def get_access_token(self) -> str:
        return self._access_token
