"""Tiered failover: subscription -> optional PROXY_URL -> standby proxies.

Only accounts still failing are carried into the next tier. Standby nodes
are neither prepared nor contacted when the primary tier succeeds.
"""

from dataclasses import dataclass
import json
import os
from pathlib import Path
import time

import auto_proxy
from proxy_runtime import (
    PROXY_URL, pin_node, route_is_reachable, start_proxy, stop_process,
)


@dataclass
class RunResult:
    total: int
    failed: list
    sources_used: list

    @property
    def succeeded(self):
        return self.total - len(self.failed)


def int_setting(name, default, maximum):
    raw = os.environ.get(name, "").strip() or str(default)
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name} must be an integer") from None
    if not 1 <= value <= maximum:
        raise ValueError(f"{name} must be between 1 and {maximum}")
    return value


def configured_sources():
    sources = [source for source, key in (("subscription", "SUB_URL"),
                                          ("proxy_url", "PROXY_URL"))
               if os.environ.get(key, "").strip()]
    if not sources:
        sources.append("external" if os.environ.get("IS_PROXY", "").lower() == "true" else "direct")
    if os.environ.get("FALLBACK_PROXIES", "").strip():
        sources.append("fallback")
    return sources


def run_renewals(accounts, run_account, *, node_attempts=3):
    pending = list(enumerate(accounts, 1))
    sources_used = []
    deadline = time.monotonic() + int_setting("RUN_BUDGET_SECONDS", 2400, 3000)
    for source in configured_sources():
        if not pending or time.monotonic() >= deadline:
            break
        process = None
        print(f"\n=== Route source: {source}; pending accounts: {len(pending)} ===", flush=True)
        sources_used.append(source)
        try:
            managed = source not in ("direct", "external")
            proxy_url = None
            if managed:
                pool = auto_proxy.prepare_source(source)
                if not pool:
                    raise RuntimeError("Proxy source returned an empty pool")
                process = start_proxy()
                proxy_url = PROXY_URL
            elif source == "external":
                proxy_url = os.environ.get("PROXY_SERVER", "").strip() or PROXY_URL
                if Path("ranked_pool.json").is_file() and proxy_url == PROXY_URL:
                    pool = json.loads(Path("ranked_pool.json").read_text(encoding="utf-8"))
                else:
                    pool = [{"tag": None}] * node_attempts
            else:
                pool = [{"tag": None}] * node_attempts
            limit = (int_setting("FALLBACK_ATTEMPTS", len(pool), 50)
                     if source == "fallback" else node_attempts)
            # No wrap-around into a failed node or an unpinned "auto" group.
            candidates = pool[:limit]
            kwargs = {"uc": True, "headless": False}
            if proxy_url:
                kwargs["proxy"] = proxy_url
            # Node-first ordering gives each pending account a fair chance
            # before a slow account consumes the whole workflow budget.
            for attempt, candidate in enumerate(candidates, 1):
                if not pending or time.monotonic() >= deadline:
                    break
                print(f"Route {source}: node {attempt}/{len(candidates)}.", flush=True)
                try:
                    if candidate.get("tag"):
                        pin_node(candidate["tag"])
                    if proxy_url and not route_is_reachable(proxy_url):
                        print("Route cannot reach the target; skipping without opening Chrome.", flush=True)
                        continue
                except Exception as exc:
                    print(f"Route selection failed ({type(exc).__name__}); trying next node.", flush=True)
                    continue
                for account_id, account in list(pending):
                    if time.monotonic() >= deadline:
                        break
                    print(f"Processing account {account_id}/{len(accounts)} via {source}.", flush=True)
                    try:
                        success = run_account(dict(kwargs), account["email"], account["password"])
                    except Exception as exc:
                        print(f"Account attempt failed ({type(exc).__name__}).", flush=True)
                        success = False
                    if success:
                        pending.remove((account_id, account))
                if not pending:
                    break
        except Exception as exc:
            print(f"Source {source} failed ({type(exc).__name__}); advancing to next tier.", flush=True)
        finally:
            if process is not None:
                stop_process(process)
    if pending and time.monotonic() >= deadline:
        print("Renewal time budget exhausted; remaining accounts are failures, not successes.", flush=True)
    result = RunResult(len(accounts), [index for index, _ in pending], sources_used)
    summary = ("## Katabump renewal\n\n"
               f"- Accounts completed (renewed or not due): {result.succeeded}/{result.total}\n"
               f"- Failed accounts: {len(result.failed)}\n"
               f"- Sources attempted in order: {', '.join(sources_used)}\n")
    print(summary, flush=True)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as stream:
            stream.write(summary)
    return result
