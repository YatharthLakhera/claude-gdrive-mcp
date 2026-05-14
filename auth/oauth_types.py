"""
Type definitions for OAuth authentication.

This module provides structured types for OAuth-related parameters,
improving code maintainability and type safety.
"""

from dataclasses import dataclass
from typing import Optional, Dict, Any

from fastmcp.server.auth import AccessToken


class WorkspaceAccessToken(AccessToken):
    """AccessToken extended with workspace-specific fields."""

    session_id: Optional[str] = None
    sub: Optional[str] = None
    email: Optional[str] = None


@dataclass
class OAuthVersionDetectionParams:
    """
    Parameters used for OAuth version detection.

    Encapsulates the various signals we use to determine
    whether a client supports OAuth 2.1 or needs OAuth 2.0.
    """

    client_id: Optional[str] = None
    client_secret: Optional[str] = None
    code_challenge: Optional[str] = None
    code_challenge_method: Optional[str] = None
    code_verifier: Optional[str] = None
    authenticated_user: Optional[str] = None
    session_id: Optional[str] = None

    @classmethod
    def from_request(
        cls, request_params: Dict[str, Any]
    ) -> "OAuthVersionDetectionParams":
        """Create from raw request parameters."""
        return cls(
            client_id=request_params.get("client_id"),
            client_secret=request_params.get("client_secret"),
            code_challenge=request_params.get("code_challenge"),
            code_challenge_method=request_params.get("code_challenge_method"),
            code_verifier=request_params.get("code_verifier"),
            authenticated_user=request_params.get("authenticated_user"),
            session_id=request_params.get("session_id"),
        )

    @property
    def has_pkce(self) -> bool:
        """Check if PKCE parameters are present."""
        return bool(self.code_challenge or self.code_verifier)

    @property
    def is_public_client(self) -> bool:
        """Check if this appears to be a public client (no secret)."""
        return bool(self.client_id and not self.client_secret)
