"""Local Host, Origin and peer boundary shared by API routes."""
import ipaddress
from urllib.parse import urlsplit
from fastapi import Request, HTTPException


def is_local_origin(origin: str) -> bool:
    """Return whether an Origin header identifies a local web application."""
    try:
        parsed = urlsplit(origin)
        return parsed.scheme in {"http", "https"} and (
            parsed.hostname == "localhost" or ipaddress.ip_address(parsed.hostname or "").is_loopback
        )
    except ValueError:
        return False


async def require_loopback(request: Request) -> None:
    """Restrict meeting access to the same local browser origin and peer."""
    try:
        is_local = ipaddress.ip_address(request.client.host).is_loopback if request.client else False
    except ValueError:
        is_local = False
    if not is_local:
        raise HTTPException(status_code=403, detail="Meeting library is available only on loopback")
    host = request.url.hostname
    try:
        is_local_host = host == "localhost" or ipaddress.ip_address(host or "").is_loopback
    except ValueError:
        is_local_host = False
    if not is_local_host:
        raise HTTPException(status_code=403, detail="Meeting library requires a local Host")
    origin = request.headers.get("origin")
    if request.method in {"POST", "PUT", "PATCH", "DELETE"} and origin and not is_local_origin(origin):
        raise HTTPException(status_code=403, detail="Meeting library requires a local Origin")

