"""GitHub log masking for individual fields inside structured Secrets."""

import os

from fallback_proxy import parse_fallback_proxies


def mask_workflow_secrets(accounts=()):
    if os.environ.get("GITHUB_ACTIONS", "").lower() != "true":
        return
    values = {os.environ.get(key, "") for key in (
        "SUB_URL", "PROXY_URL", "TG_BOT_TOKEN", "TG_CHAT_ID", "KATABUMP_EMAIL", "KATABUMP_PASSWORD",
    )}
    for account in accounts:
        values.update((account.get("email", ""), account.get("password", "")))
    try:
        for _, outbound in parse_fallback_proxies(os.environ.get("FALLBACK_PROXIES", "")):
            values.update((outbound.get("username", ""), outbound.get("password", "")))
    except ValueError:
        pass  # Config validation reports the entry number, not its contents.
    for value in sorted(values):
        if value:
            escaped = value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
            # This is a GitHub runner command; the value is registered for
            # redaction before subsequent Selenium/requests diagnostics.
            print(f"::add-mask::{escaped}", flush=True)
