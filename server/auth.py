"""Cognito as the authorization server, this server as the resource server.

Step 14 in the build plan was "OAuth 2.1 with Cognito, or a written finding about why not",
budgeted as a day. Step 4's reconnaissance collapsed most of it — Cowork's connector form
accepts a pre-registered client id and secret, so there is no dynamic client registration to
build — and reading the SDK collapsed the rest. ``mcp.server.auth`` already ships the
resource-server half of the specification: ``RequireAuthMiddleware`` around the endpoint,
RFC 9728 protected resource metadata on a well-known route, and a ``WWW-Authenticate``
header on rejection that tells a client where to go and authenticate.

So what is left is the one thing the SDK cannot know: how to check a Cognito token. That is
this file, and it is a class with one method.

**The claims that are checked, and why each one.** A JWT that merely parses proves nothing;
each of these closes a specific hole.

- *Signature*, against the pool's published JWKS, fetched over HTTPS and cached by key id.
  Without it every other claim is attacker-controlled.
- *``iss``* must equal the configured issuer exactly. A signature check alone accepts a
  correctly-signed token from somebody else's Cognito pool.
- *``token_use == "access"``*. Cognito issues ID tokens and access tokens from the same
  pool with the same signing keys, and an ID token is not an authorization to call an API.
  This is the check most often missing from Cognito integrations, because omitting it
  breaks nothing visible.
- *``client_id``*, against the set this server accepts, so that a correctly-signed token
  minted for some other application in the same pool is still refused. Note the asymmetry
  that trips people up here: a Cognito **access** token carries ``client_id`` and no
  ``aud``, while an **ID** token carries ``aud`` and no ``client_id``. Validating ``aud``
  on an access token therefore fails against a token that is perfectly valid, which sends
  people to ``options={"verify_aud": False}`` and quietly removes the audience check
  altogether — the fix is to check ``client_id``, not to check nothing.
- *``exp``* and ``nbf``, which PyJWT enforces during decode.

Scopes are returned rather than enforced here. ``AuthSettings.required_scopes`` is what
enforces them, so that a rejection for insufficient scope comes back through the SDK's own
path with the right status and the right ``WWW-Authenticate`` header.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from mcp.server.auth.provider import AccessToken

# Cognito rotates signing keys rarely and publishes both during a rotation, so a long cache
# is safe. PyJWKClient refetches on an unknown kid regardless, which is the case that
# matters; this only bounds how long a retired key stays cached.
JWKS_CACHE_SECONDS = 3600
JWKS_CACHE_KEYS = 16


class AuthConfigError(RuntimeError):
    """Authentication was switched on without enough configuration to do it."""


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise AuthConfigError(
            f"BIZDATA_AUTH is set but {name} is not. Every value comes from "
            f"`terraform output` with enable_auth = true; see infra/main/cognito.tf."
        )
    return value


@dataclass(frozen=True)
class CognitoSettings:
    issuer: str
    jwks_url: str

    # More than one, because the pool holds the connector's authorization_code client and
    # a client_credentials client that scripts/check_auth.py uses to prove the chain
    # without a browser. Both are legitimate callers; nothing else in the pool is.
    client_ids: tuple[str, ...]

    resource_url: str
    scopes: tuple[str, ...]

    # "cognito" advertises Cognito's own issuer and relies on the client falling back to
    # OIDC discovery. "self" advertises this server and serves an RFC 8414 document. See
    # server/oauth_metadata.py for which one Cowork actually needs.
    as_mode: str

    authorization_endpoint: str
    token_endpoint: str

    @classmethod
    def from_env(cls) -> CognitoSettings:
        issuer = _require("BIZDATA_OAUTH_ISSUER").rstrip("/")
        scopes = tuple(s for s in os.environ.get("BIZDATA_OAUTH_SCOPES", "").split(",") if s)
        return cls(
            issuer=issuer,
            jwks_url=os.environ.get("BIZDATA_OAUTH_JWKS_URL") or f"{issuer}/.well-known/jwks.json",
            client_ids=tuple(c.strip() for c in _require("BIZDATA_OAUTH_CLIENT_ID").split(",") if c.strip()),
            resource_url=_require("BIZDATA_RESOURCE_URL"),
            scopes=scopes,
            as_mode=os.environ.get("BIZDATA_OAUTH_AS_MODE", "cognito"),
            authorization_endpoint=os.environ.get("BIZDATA_OAUTH_AUTHORIZATION_ENDPOINT", ""),
            token_endpoint=os.environ.get("BIZDATA_OAUTH_TOKEN_ENDPOINT", ""),
        )


def enabled() -> bool:
    """Whether to put the server behind a token at all.

    Absent by default, which is what keeps `uvicorn server.app:app` working on a laptop with
    no AWS account, no Cognito pool and no network. The deployed function sets it from
    Terraform, and only when var.enable_auth is true.
    """
    return os.environ.get("BIZDATA_AUTH", "").lower() in ("cognito", "1", "true")


class CognitoTokenVerifier:
    """Verify a Cognito user pool access token. Implements the SDK's TokenVerifier."""

    def __init__(self, settings: CognitoSettings) -> None:
        from jwt import PyJWKClient

        self.settings = settings
        self._jwks = PyJWKClient(
            settings.jwks_url,
            cache_keys=True,
            max_cached_keys=JWKS_CACHE_KEYS,
            lifespan=JWKS_CACHE_SECONDS,
        )

    async def verify_token(self, token: str) -> AccessToken | None:
        """Return the token's authority, or None if it has none.

        None rather than an exception for every failure, including malformed input. The
        SDK turns None into a 401 with the WWW-Authenticate header a client needs in order
        to discover where to authenticate; an exception escaping here would be a 500, which
        tells an honest client nothing and a dishonest one slightly more than nothing.
        """
        import jwt

        try:
            signing_key = self._jwks.get_signing_key_from_jwt(token)
            claims: dict[str, Any] = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                issuer=self.settings.issuer,
                # A Cognito access token has no `aud`, so there is nothing to verify here
                # and the client identity is checked below against `client_id` instead.
                # Turned off explicitly, with the reason, rather than being buried in a
                # copied options dict.
                options={"verify_aud": False, "require": ["exp", "iss", "sub"]},
            )
        except Exception:
            return None

        # An ID token from the same pool carries the same signature and the same issuer.
        # It is not an API authorization and must not be accepted as one.
        if claims.get("token_use") != "access":
            return None

        if claims.get("client_id") not in self.settings.client_ids:
            return None

        return AccessToken(
            token=token,
            client_id=str(claims.get("client_id")),
            scopes=str(claims.get("scope", "")).split(),
            expires_at=claims.get("exp"),
            resource=self.settings.resource_url,
            subject=claims.get("sub"),
            claims=claims,
        )
