"""Who may call, which is a shorter question here than it looks.

This service authenticates **services**, not people. A front end authenticates its
own user -- a person through their session, a reviewer through the institution's
role model -- and then vouches for the call. That is what keeps a package in the
collective free of Shibboleth, of one university's role names, and of any opinion
about who is allowed to approve a photograph.

There is exactly one route without a token, and it is not an oversight: a wallet
provider fetches the `current` URL without credentials, long after a pass was
issued.
"""

import hmac

from fastapi import Header, HTTPException, Request, status


async def require_service_token(request: Request, authorization: str = Header(default="")) -> str:
    """Accept a configured service token and return the caller's name.

    Compared with `hmac.compare_digest` rather than `==`: token comparison that
    returns early on the first differing byte is measurable over enough requests.
    """
    tokens: dict[str, str] = request.app.state.service_tokens
    if not authorization.startswith("Bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "a service token is required")
    presented = authorization.removeprefix("Bearer ")
    for name, token in tokens.items():
        if hmac.compare_digest(presented, token):
            return name
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "unknown service token")
