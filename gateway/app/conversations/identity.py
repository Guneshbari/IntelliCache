"""
Authentication and principal identity derivation for persistent conversation ownership.
"""

from dataclasses import dataclass
import hashlib
import hmac
import ipaddress
import logging
from typing import Optional

from fastapi import Depends, HTTPException, Request, status

from ..config import Settings, get_settings

logger = logging.getLogger("intellicache.gateway.conversations.identity")


def _is_loopback_address(host: Optional[str]) -> bool:
    """
    Checks if a host/IP string represents a loopback address.
    Supports IPv4 (127.0.0.0/8), IPv6 (::1), and loopback test hosts.
    """
    if not host:
        return False
    host_clean = host.strip().lower()
    if host_clean in ("localhost", "testclient", "testserver"):
        return True
    try:
        ip = ipaddress.ip_address(host_clean)
        return ip.is_loopback
    except ValueError:
        return False


@dataclass(frozen=True)
class AuthenticatedClient:
    """Represents the authenticated caller with its server-authorized namespace and opaque principal ID."""
    token: str
    namespace: str
    principal_id: str


def derive_principal_id(client_token: str, identity_secret: str) -> str:
    """
    Derives a deterministic, opaque 64-character hex principal ID
    from the client API token and the server-side identity secret using HMAC-SHA256.
    Never persists or logs the raw token.
    """
    if not identity_secret or len(identity_secret.strip()) < 16:
        raise ValueError("CONVERSATION_IDENTITY_SECRET must be at least 16 characters long.")
    return hmac.new(
        identity_secret.encode("utf-8"),
        client_token.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def verify_conversation_client(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> AuthenticatedClient:
    """
    FastAPI dependency that authenticates the client API token, resolves its
    authorized cache namespace, and derives the opaque client principal identity.
    """
    token_map = settings.get_client_token_map()
    secret = settings.conversation_identity_secret

    # 1. Extract token candidate from headers
    token_candidate: Optional[str] = request.headers.get("X-Client-Token")
    if not token_candidate:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.lower().startswith("bearer "):
            token_candidate = auth_header[7:].strip()

    # 2. Verify token candidate against configured token_map
    if token_candidate:
        if token_map:
            matched_namespace: Optional[str] = None
            for valid_token, ns in token_map.items():
                if hmac.compare_digest(token_candidate, valid_token):
                    matched_namespace = ns
                    break
            if matched_namespace is not None:
                principal_id = derive_principal_id(token_candidate, secret)
                return AuthenticatedClient(
                    token=token_candidate,
                    namespace=matched_namespace,
                    principal_id=principal_id,
                )

        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized: Invalid or missing client API token.",
        )

    # 3. Handle explicit unauthenticated loopback if enabled
    if settings.allow_unauthenticated_loopback:
        forwarded_headers = ("x-forwarded-for", "x-forwarded-proto", "x-forwarded-host", "forwarded")
        for hdr in forwarded_headers:
            if hdr in request.headers:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Unauthorized: Unauthenticated loopback access rejected when forwarded headers are present.",
                )

        client_host = request.client.host if request.client else None
        if not _is_loopback_address(client_host):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Unauthorized: Direct connection peer is not a valid loopback address.",
            )

        server_info = request.scope.get("server")
        server_host = server_info[0] if (server_info and len(server_info) > 0) else None
        if not _is_loopback_address(server_host) or not _is_loopback_address(settings.host):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Unauthorized: Server address is not a valid loopback address.",
            )

        loopback_token = f"loopback:{settings.cache_namespace}"
        principal_id = derive_principal_id(loopback_token, secret)
        return AuthenticatedClient(
            token=loopback_token,
            namespace=settings.cache_namespace,
            principal_id=principal_id,
        )

    # 4. Default: Require authentication
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Unauthorized: Invalid or missing client API token.",
    )
