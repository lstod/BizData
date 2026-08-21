"""The RFC 8414 document, served only if the client cannot find Cognito's own.

Step 4 left one question open that no amount of documentation answers, because it is a fact
about a client rather than about a specification. Cowork's connector form has fields for an
OAuth client id and secret and **no field for an authorization endpoint, a token endpoint or
scopes**, so it must discover all three from the server URL. The chain a modern MCP client
follows is:

1. call the endpoint unauthenticated, and read ``WWW-Authenticate`` on the 401, which names
   the protected resource metadata URL — the SDK does this part
2. fetch that document, RFC 9728, and read ``authorization_servers`` — the SDK does this too
3. fetch the authorization server's metadata, and *this* is the uncertain step

Step 3 is uncertain because there are two paths and Cognito only serves one of them. RFC
8414 puts authorization server metadata at ``/.well-known/oauth-authorization-server``.
OpenID Connect Discovery puts substantially the same document at
``/.well-known/openid-configuration``. **Cognito serves the OIDC one and not the RFC 8414
one.** A client that tries both finds Cognito unaided; a client that tries only RFC 8414
finds nothing, and the connector fails at a point that looks like a server bug.

Hence two modes, and the cheap one first:

``cognito``
    Advertise Cognito's issuer and serve nothing here. Correct, in the sense that Cognito
    really is the authorization server, and it means no document to keep in step with the
    pool. Works if the client falls back to OIDC discovery.

``self``
    Advertise this server as its own authorization server and serve the RFC 8414 document
    below, whose ``authorization_endpoint`` and ``token_endpoint`` point straight at
    Cognito's hosted UI. Nothing proxies the flow — the client is sent directly to Cognito
    and the tokens are Cognito's — so this is a discovery shim rather than an
    authorization server, and it exists purely because of where the file sits.

The mode is an environment variable rather than a code change, so switching it is a
Terraform apply and the finding is recorded either way.
"""

from __future__ import annotations

from typing import Any

from server.auth import CognitoSettings

WELL_KNOWN_PATH = "/.well-known/oauth-authorization-server"


def authorization_server_metadata(settings: CognitoSettings) -> dict[str, Any]:
    """An RFC 8414 document for this resource, delegating to Cognito's hosted UI.

    ``issuer`` is this server rather than Cognito, and it has to be: RFC 8414 requires the
    issuer in the document to match the authorization server the client was sent to, by
    exact string comparison, and the client was sent here. Claiming Cognito's issuer in a
    document served from this host is the mismatch the specification exists to catch.
    """
    base = settings.resource_url.removesuffix("/mcp").rstrip("/")

    return {
        "issuer": base,
        "authorization_endpoint": settings.authorization_endpoint,
        "token_endpoint": settings.token_endpoint,
        "jwks_uri": settings.jwks_url,
        "scopes_supported": list(settings.scopes) + ["openid"],
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "token_endpoint_auth_methods_supported": [
            "client_secret_basic",
            "client_secret_post",
        ],
        # PKCE is mandatory in OAuth 2.1 and Cognito supports it. Advertising S256 only,
        # because "plain" is permitted by the RFC and is not protection.
        "code_challenge_methods_supported": ["S256"],
        "service_documentation": "https://github.com/bizdata/mcp-server",
    }


def register(mcp: Any, settings: CognitoSettings) -> bool:
    """Attach the RFC 8414 route when the mode calls for it. Returns whether it did."""
    if settings.as_mode != "self":
        return False

    from starlette.responses import JSONResponse

    document = authorization_server_metadata(settings)

    @mcp.custom_route(WELL_KNOWN_PATH, methods=["GET"])
    async def oauth_authorization_server(request: Any) -> JSONResponse:
        # Unauthenticated by design: a client reads this precisely because it does not yet
        # have a token. custom_route is exempt from RequireAuthMiddleware for this reason.
        return JSONResponse(document)

    return True
