"""Unauthenticated connection diagnostics; never reads .env or API keys."""

import json
import argparse
import os
import socket
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path

from .llm import network_error


def main():
    parser = argparse.ArgumentParser(description="Unauthenticated HTTPS diagnostics; does not read .env")
    parser.add_argument("--provider", choices=("openrouter", "gemini"), default="openrouter")
    args = parser.parse_args()
    host = "generativelanguage.googleapis.com" if args.provider == "gemini" else "openrouter.ai"
    url = "https://" + host + ("/v1beta/openai/models" if args.provider == "gemini" else "/api/v1/models")
    ca = ssl.get_default_verify_paths()
    report = {"provider": args.provider, "host": host,
              "python": sys.version.split()[0], "executable": sys.executable,
              "openssl": ssl.OPENSSL_VERSION,
              "ca_file_configured": bool(ca.cafile),
              "ca_file_exists": bool(ca.cafile and Path(ca.cafile).is_file()),
              "proxy_configured": bool(urllib.request.getproxies())}
    # SSL environment paths can affect Python independently of browser certificates.
    report["ssl_cert_file_override"] = "SSL_CERT_FILE" in os.environ
    report["ssl_cert_dir_override"] = "SSL_CERT_DIR" in os.environ
    try:
        socket.getaddrinfo(host, 443)
        report["dns"] = "OK"
    except OSError as exc:
        report["dns"] = network_error(exc)
    try:
        # No authorization or inference. Google's model list may require a key;
        # HTTP 401/403 still establishes that DNS and TLS worked.
        request = urllib.request.Request(url)
        with urllib.request.urlopen(request, timeout=15) as response:
            report["https"] = "OK"
            report["http_status"] = response.status
    except urllib.error.HTTPError as exc:
        report["https"] = "OK" if exc.code in (401, 403) else "HTTP_ERROR"
        report["authentication_checked"] = False
        report["http_status"] = exc.code
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        report["https"] = network_error(exc)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["https"] == "OK" else 1


if __name__ == "__main__":
    raise SystemExit(main())
