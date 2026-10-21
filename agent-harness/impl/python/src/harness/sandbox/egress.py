"""Egress decision logic (§10.6): default deny, host/port grants, SSRF blocking after resolution.

The network proxy binary itself is not implemented; this is its policy core, testable in isolation.
"""
from __future__ import annotations
import ipaddress
from ..policy import glob_match

METADATA = [ipaddress.ip_network("169.254.0.0/16"), ipaddress.ip_network("fd00:ec2::/32")]


def normalize_host(h: str) -> str:
    h = h.strip().lower().rstrip(".")
    try:
        return h.encode("idna").decode("ascii")
    except UnicodeError:
        return h


def is_forbidden_ip(ip: str) -> bool:
    a = ipaddress.ip_address(ip)
    return a.is_private or a.is_loopback or a.is_link_local or a.is_multicast or a.is_unspecified or a.is_reserved or any(a in n for n in METADATA)


def decide(host: str, port: int, grants: list[dict], resolved_ips: list[str] | None = None, method: str | None = None) -> dict:
    h = normalize_host(host)
    for g in grants:
        if not glob_match(g["host_glob"].lower(), h) or port not in g["ports"]:
            continue
        if g.get("methods") and method and method not in g["methods"]:
            continue
        explicit = g.get("allow_private", False)
        if any(is_forbidden_ip(ip) for ip in (resolved_ips or [])) and not explicit:
            return {"decision": "deny", "reason": "ssrf_blocked_address"}
        return {"decision": "allow", "reason": "grant", "grant": g["host_glob"]}
    return {"decision": "deny", "reason": "default_deny"}
