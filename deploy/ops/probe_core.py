#!/usr/bin/env python3
"""Run in the bot image/network. No Telegram or model calls, no application writes."""
import argparse
import ipaddress
from pathlib import Path

import httpx

parser = argparse.ArgumentParser()
parser.add_argument("--core-vpn-ip", default="10.77.0.1")
parser.add_argument("--core-public-ip", default="178.217.98.101")
parser.add_argument("--service-token-file", default="/run/secrets/service")
args = parser.parse_args()
vpn = ipaddress.IPv4Address(args.core_vpn_ip)
public = ipaddress.IPv4Address(args.core_public_ip)
if not vpn.is_private or not public.is_global:
    raise SystemExit("Use private VPN and public IPv4 addresses")
core = f"http://{vpn}:18000"
token = Path(args.service_token_file).read_text().strip()
with httpx.Client(timeout=5, trust_env=False, follow_redirects=False) as client:
    assert client.get(core + "/health").status_code == 200
    endpoint = core + "/internal/v1/telegram/updates"
    for headers in ({}, {"Authorization": "Bearer synthetic-wrong-token"}):
        assert client.post(endpoint, json={}, headers=headers).status_code == 401
    # Authenticated malformed input stops at validation, before any write or job.
    assert client.post(endpoint, json={}, headers={"Authorization": "Bearer " + token}).status_code == 422
    for path in ("/internal", "/internal/v1/telegram/updates"):
        assert client.post(f"http://{public}" + path, json={}).status_code == 404
    assert client.get(f"http://{public}/").status_code == 503
    try:
        client.get(f"http://{public}:18000/health", timeout=3)
    except (httpx.ConnectError, httpx.TimeoutException):
        pass
    else:
        raise AssertionError("Public API has HTTP access")
print("Container VPN health/auth OK; public API has no HTTP access; internal routes 404; maintenance 503")
