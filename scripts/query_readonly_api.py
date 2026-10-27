"""Query the HTTPS integration with the inherited proxy and secure token binding."""

from __future__ import annotations

import argparse
import json
import os
from urllib.parse import urlsplit

import requests


class QueryError(Exception):
    pass


def query(base_url, endpoint, *, params=None, token=None, session=None):
    parts = urlsplit(base_url)
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
        or parts.path not in {"", "/"}
        or parts.port not in {None, 443}
    ):
        raise QueryError("Base URL must be an HTTPS origin with a valid certificate")
    if endpoint not in {"health", "dates", "quotes", "candidates", "signals", "logs/summary"}:
        import re

        if not re.fullmatch(r"minutes/\d{6}(?:\.(?:SH|SZ|BJ))?", endpoint):
            raise QueryError("Unsupported readonly endpoint")
    token = token or os.getenv("READONLY_API_TOKEN", "")
    if not token:
        raise QueryError("READONLY_API_TOKEN is missing; configure it in secure credential settings")
    transport = session or requests
    try:
        result = transport.get(
            base_url.rstrip("/") + "/api/v1/readonly/" + endpoint,
            params=params or {},
            headers={"Authorization": "Bearer " + token},
            timeout=(5, 15),
            allow_redirects=False,
        )
    except requests.exceptions.SSLError as exc:
        raise QueryError("HTTPS certificate verification failed; fix the server certificate") from exc
    except requests.exceptions.RequestException as exc:
        raise QueryError("HTTPS network request failed; check destination policy and server availability") from exc
    if result.status_code != 200:
        raise QueryError(f"Readonly API returned HTTP {result.status_code}; redirects are not followed")
    if len(result.content) > 2 * 1024 * 1024:
        raise QueryError("API response exceeds size limit")
    try:
        payload = result.json()
    except ValueError as exc:
        raise QueryError("Unexpected API response") from exc
    if not isinstance(payload, dict) or payload.get("read_only") is not True:
        raise QueryError("Unexpected API contract")
    return payload


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("endpoint", choices=["health", "dates", "quotes", "candidates", "signals", "minutes", "logs"])
    parser.add_argument("--base-url", default=os.getenv("READONLY_API_BASE_URL", "https://astockreview.cn"))
    parser.add_argument("--date", default="")
    parser.add_argument("--code", default="")
    parser.add_argument("--codes", default="")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--kind", choices=["preopen", "postclose"], default="preopen")
    args = parser.parse_args()
    endpoint = {"minutes": "minutes/" + args.code, "logs": "logs/summary"}.get(args.endpoint, args.endpoint)
    params = {}
    if args.endpoint in {"candidates", "signals", "minutes", "logs"}:
        params["date"] = args.date
    if args.endpoint in {"candidates", "signals", "minutes"}:
        params.update(limit=args.limit, offset=args.offset)
    if args.code and args.endpoint in {"candidates", "signals"}:
        params["code"] = args.code
    if args.endpoint == "quotes":
        params["codes"] = args.codes
    if args.endpoint == "logs":
        params["kind"] = args.kind
    try:
        payload = query(args.base_url, endpoint, params=params)
    except QueryError as exc:
        parser.exit(1, str(exc) + "\n")
    print(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
