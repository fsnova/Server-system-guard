# -*- coding: utf-8 -*-
"""check-host.net API helper (async).

We only implement what we need for "Ping" monitoring.
API reference: https://check-host.net/about/api

JSON notes (ping):
- /check-ping?host=...&node=... returns a request_id and a permanent_link.
- /check-result/<id> returns a dict: node_name -> results
  * while still running: value is null
  * when done: value is a list (one element per resolved address)
    each element is a list of ping attempts like:
      ["OK", 0.044, "1.2.3.4"]
      ["TIMEOUT", 3.005]

We treat a node as "4/4" only if all 4 attempts are "OK".
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import aiohttp


class CheckHostError(Exception):
    pass


@dataclass
class PingCheckResult:
    request_id: str
    report_url: str
    total_nodes: int
    ok_nodes: int
    per_node_ok_counts: Dict[str, int]  # node -> ok_count (0..4)

    # --- Backward-compatible aliases ---
    # Earlier iterations of this project referenced different attribute names.
    # Keeping these properties avoids breaking the bot when the helper is updated.
    @property
    def permanent_link(self) -> str:
        return self.report_url

    @property
    def per_node_ok(self) -> Dict[str, int]:
        return self.per_node_ok_counts

    @property
    def packets_per_node(self) -> int:
        # check-host "ping" endpoint performs 4 ping attempts per node.
        return 4


def _extract_ok_count(node_payload: Any) -> Optional[int]:
    """Return ok_count (0..4) if node has a finished payload.

    Returns None if still running (payload is None).
    """
    if node_payload is None:
        return None

    # Finished payload is usually a list: [addr_results, addr_results2, ...]
    # Each addr_results is a list of ping attempts.
    if not isinstance(node_payload, list) or len(node_payload) == 0:
        return 0

    first_addr = node_payload[0]
    if first_addr is None:
        return 0

    # Sometimes it's [[null]] when host can't be resolved.
    if isinstance(first_addr, list) and len(first_addr) == 1 and first_addr[0] is None:
        return 0

    if not isinstance(first_addr, list):
        return 0

    ok = 0
    for attempt in first_addr:
        if isinstance(attempt, list) and len(attempt) >= 1 and attempt[0] == "OK":
            ok += 1
    return ok


async def run_ping_check(
    host: str,
    nodes: List[str],
    *,
    max_wait_sec: int = 60,
    poll_interval_sec: float = 2.0,
    request_timeout_sec: int = 30,
) -> PingCheckResult:
    """Run a ping check against a given set of nodes and wait until results are ready."""

    if not host or not isinstance(host, str):
        raise CheckHostError("host is empty")

    if not nodes:
        raise CheckHostError("nodes list is empty")

    headers = {
        "Accept": "application/json",
        "User-Agent": "ServerSystemGuardBot/1.0 (+https://t.me/)"
    }

    timeout = aiohttp.ClientTimeout(total=request_timeout_sec)

    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        # Create check request
        params = [("host", host)]
        for n in nodes:
            params.append(("node", n))

        try:
            async with session.get("https://check-host.net/check-ping", params=params) as resp:
                if resp.status != 200:
                    txt = await resp.text()
                    raise CheckHostError(f"check-ping HTTP {resp.status}: {txt[:200]}")
                data = await resp.json()
        except asyncio.TimeoutError as e:
            raise CheckHostError("check-ping timeout") from e
        except aiohttp.ClientError as e:
            raise CheckHostError(f"check-ping network error: {e}") from e

        request_id = data.get("request_id")
        report_url = data.get("permanent_link") or ""
        if not request_id:
            raise CheckHostError(f"invalid response from check-host: {data}")

        # Poll results
        deadline = asyncio.get_event_loop().time() + max_wait_sec
        last_payload = None
        while True:
            try:
                async with session.get(f"https://check-host.net/check-result/{request_id}") as resp:
                    if resp.status != 200:
                        txt = await resp.text()
                        raise CheckHostError(f"check-result HTTP {resp.status}: {txt[:200]}")
                    payload = await resp.json()
                    last_payload = payload
            except asyncio.TimeoutError:
                payload = last_payload
            except aiohttp.ClientError:
                payload = last_payload

            if isinstance(payload, dict):
                done = True
                per_node_ok: Dict[str, int] = {}
                for n in nodes:
                    okc = _extract_ok_count(payload.get(n))
                    if okc is None:
                        done = False
                        break
                    per_node_ok[n] = int(okc)
                if done:
                    ok_nodes = sum(1 for v in per_node_ok.values() if v == 4)
                    return PingCheckResult(
                        request_id=str(request_id),
                        report_url=str(report_url),
                        total_nodes=len(nodes),
                        ok_nodes=int(ok_nodes),
                        per_node_ok_counts=per_node_ok,
                    )

            if asyncio.get_event_loop().time() >= deadline:
                # Timeout waiting. Treat missing nodes as 0/4.
                per_node_ok = {}
                if isinstance(last_payload, dict):
                    for n in nodes:
                        okc = _extract_ok_count(last_payload.get(n))
                        per_node_ok[n] = int(okc or 0)
                else:
                    for n in nodes:
                        per_node_ok[n] = 0
                ok_nodes = sum(1 for v in per_node_ok.values() if v == 4)
                return PingCheckResult(
                    request_id=str(request_id),
                    report_url=str(report_url),
                    total_nodes=len(nodes),
                    ok_nodes=int(ok_nodes),
                    per_node_ok_counts=per_node_ok,
                )

            await asyncio.sleep(poll_interval_sec)
