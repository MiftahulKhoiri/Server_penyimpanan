from __future__ import annotations

from dataclasses import dataclass

from fastapi import Request


@dataclass(frozen=True)
class ClientInfo:
    ip_address: str | None
    user_agent: str | None


def client_info(request: Request) -> ClientInfo:
    # request.client.host sudah memperhitungkan X-Forwarded-For bila uvicorn
    # dijalankan dengan proxy_headers dan FORWARDED_ALLOW_IPS yang benar.
    ip = request.client.host if request.client else None
    ua = request.headers.get("user-agent")
    return ClientInfo(ip_address=ip[:45] if ip else None, user_agent=ua[:255] if ua else None)
