"""Receive-only watch of the operator's EVM wallet for USDC/USDT payments.

Only a *public* address is ever stored; there is no key, no signing and no
transaction sending anywhere in the Earner. The watcher reads ERC-20
``Transfer(from, to=wallet, value)`` logs of fixed stablecoin contracts over
fixed public RPC hosts (both are code, verified on-chain on 2026-10-20:
symbol and decimals), through egress.fetch — HTTPS, public addresses, pinned
IP, no redirects.

A transfer counts once it is ``CONFIRMATIONS`` blocks deep. Each one becomes an
``earner_income`` row (unique per network/tx/log index — a rescan cannot count
it twice). It is linked to an opportunity automatically only when exactly one
delivered, unpaid opportunity claims that amount (±1 %); otherwise it waits for
the operator to link it. Only a linked on-chain transfer marks a task paid.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Callable, Dict, List, Optional

from backend.earner import egress, store

logger = logging.getLogger("hermes.earner.wallet")

TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

NETWORKS: Dict[str, Dict[str, Any]] = {
    "base": {
        "rpc": "https://base-rpc.publicnode.com", "confirmations": 30, "chunk": 1500, "lookback": 43_000,
        "explorer": "https://basescan.org/tx/",
        "tokens": {"0x833589fcd6edb6e08f4c7c32d4f71b54bda02913": ("USDC", 6),
                   "0xfde4c96c8593536e31f229ea8f37b2ada2699bb2": ("USDT", 6)},
    },
    "polygon": {
        "rpc": "https://polygon-bor-rpc.publicnode.com", "confirmations": 64, "chunk": 1500, "lookback": 43_000,
        "explorer": "https://polygonscan.com/tx/",
        "tokens": {"0x3c499c542cef5e3811e1192ce70d8cc03d5c3359": ("USDC", 6),
                   "0xc2132d05d31c914a87c6611c10748aeb04b58e8f": ("USDT", 6)},
    },
    "ethereum": {
        "rpc": "https://ethereum-rpc.publicnode.com", "confirmations": 12, "chunk": 1000, "lookback": 7_200,
        "explorer": "https://etherscan.io/tx/",
        "tokens": {"0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48": ("USDC", 6),
                   "0xdac17f958d2ee523a2206206994597c13d831ec7": ("USDT", 6)},
    },
}
RPC_HOSTS = sorted({re.sub(r"^https://([^/]+).*$", r"\1", net["rpc"]) for net in NETWORKS.values()})
MAX_CHUNKS_PER_SCAN = 6
MATCH_TOLERANCE = 0.01

Fetcher = Callable[..., egress.FetchResult]


class RpcError(RuntimeError):
    pass


def _rpc(network: str, method: str, params: List[Any], fetch: Fetcher) -> Any:
    import json

    result = fetch(NETWORKS[network]["rpc"], RPC_HOSTS, method="POST",
                   json_body={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    if result.status != 200:
        raise RpcError(f"RPC {network}: HTTP {result.status}")
    data = json.loads(result.body.decode("utf-8", errors="replace"))
    if data.get("error"):
        raise RpcError(f"RPC {network}: {str(data['error'].get('message', data['error']))[:200]}")
    return data.get("result")


def _topic_for(address: str) -> str:
    return "0x" + "0" * 24 + address.lower()[2:]


def parse_claimed_usd(text: str) -> Optional[float]:
    """First dollar-ish amount in a collector-recorded reward text."""
    match = re.search(r"(?:US\$|\$)\s?(\d[\d,]*(?:\.\d+)?)\s?([kK])?|(\d[\d,]*(?:\.\d+)?)\s?(?:USD|USDC|USDT)\b",
                      text or "")
    if not match:
        return None
    number = float((match.group(1) or match.group(3)).replace(",", ""))
    return number * 1000 if match.group(2) else number


def scan_network(network: str, address: str, fetch: Fetcher = egress.fetch) -> Dict[str, Any]:
    config = NETWORKS[network]
    head = int(_rpc(network, "eth_blockNumber", [], fetch), 16)
    safe_head = head - config["confirmations"]
    with store.read() as connection:
        cursor = connection.execute(
            "SELECT last_block FROM earner_wallet_cursor WHERE network = ? AND address = ?",
            (network, address)).fetchone()
    start = (cursor["last_block"] + 1) if cursor else max(0, safe_head - config["lookback"])
    found: List[Dict[str, Any]] = []
    scanned_to = start - 1
    for _ in range(MAX_CHUNKS_PER_SCAN):
        if scanned_to >= safe_head:
            break
        frm, to = scanned_to + 1, min(safe_head, scanned_to + config["chunk"])
        logs = _rpc(network, "eth_getLogs", [{
            "fromBlock": hex(frm), "toBlock": hex(to), "address": list(config["tokens"].keys()),
            "topics": [TRANSFER_TOPIC, None, _topic_for(address)],
        }], fetch) or []
        for log in logs:
            contract = str(log.get("address") or "").lower()
            if contract not in config["tokens"] or len(log.get("topics") or []) < 3:
                continue
            if str(log["topics"][2]).lower() != _topic_for(address):
                continue
            symbol, decimals = config["tokens"][contract]
            raw = int(log.get("data") or "0x0", 16)
            found.append({
                "network": network, "token": symbol, "amount": str(raw / 10 ** decimals),
                "amount_usd": raw / 10 ** decimals, "from_address": "0x" + str(log["topics"][1])[-40:].lower(),
                "to_address": address, "tx_hash": str(log.get("transactionHash")),
                "log_index": int(str(log.get("logIndex") or "0x0"), 16),
                "block_number": int(str(log.get("blockNumber") or "0x0"), 16),
            })
        scanned_to = to
    new = record_income(found)
    with store.tx() as connection:
        connection.execute(
            "INSERT INTO earner_wallet_cursor (network, address, last_block, checked_at, error) VALUES (?, ?, ?, ?, '') "
            "ON CONFLICT(network, address) DO UPDATE SET last_block = excluded.last_block, "
            "checked_at = excluded.checked_at, error = ''", (network, address, scanned_to, store.iso()))
    return {"network": network, "head": head, "scanned_to": scanned_to, "new": new,
            "behind": max(0, safe_head - scanned_to)}


def record_income(transfers: List[Dict[str, Any]]) -> int:
    new = 0
    with store.tx() as connection:
        for item in transfers:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO earner_income (id, network, token, amount, amount_usd, from_address, "
                "to_address, tx_hash, log_index, block_number, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (store.new_id("inc"), item["network"], item["token"], item["amount"], item["amount_usd"],
                 item["from_address"], item["to_address"], item["tx_hash"], item["log_index"],
                 item["block_number"], store.iso()))
            if cursor.rowcount != 1:
                continue
            new += 1
            income_id = connection.execute(
                "SELECT id FROM earner_income WHERE network = ? AND tx_hash = ? AND log_index = ?",
                (item["network"], item["tx_hash"], item["log_index"])).fetchone()["id"]
            store.log_event(connection, "income", income_id, "income_received",
                            f"Поступление {item['amount']} {item['token']} ({item['network']}) от {item['from_address']}",
                            {"tx_hash": item["tx_hash"]})
            _auto_link(connection, income_id, item)
    return new


def _auto_link(connection, income_id: str, item: Dict[str, Any]) -> None:
    rows = connection.execute(
        "SELECT id, claimed_reward FROM earner_opportunities WHERE delivered_at IS NOT NULL AND paid_at IS NULL "
        "AND is_demo = 0").fetchall()
    matches = []
    for row in rows:
        claimed = parse_claimed_usd(row["claimed_reward"])
        if claimed and abs(claimed - item["amount_usd"]) <= claimed * MATCH_TOLERANCE:
            matches.append(row["id"])
    if len(matches) != 1:
        return  # none or ambiguous: the operator links it by hand
    link(connection, income_id, matches[0], "auto", actor="wallet-watcher")


def link(connection, income_id: str, opp_id: str, kind: str, actor: str) -> None:
    income = connection.execute("SELECT * FROM earner_income WHERE id = ?", (income_id,)).fetchone()
    opp = store.get_opportunity(opp_id, connection)
    if income is None or opp is None:
        raise KeyError(income_id if income is None else opp_id)
    if income["opportunity_id"]:
        raise ValueError("поступление уже привязано")
    if not opp["delivered_at"]:
        raise ValueError("сначала отметьте передачу результата")
    if opp["paid_at"]:
        raise ValueError("задача уже отмечена оплаченной")
    explorer = NETWORKS.get(income["network"], {}).get("explorer", "")
    note = (f"on-chain: {income['amount']} {income['token']} ({income['network']}), "
            f"tx {explorer}{income['tx_hash']}")
    connection.execute("UPDATE earner_income SET opportunity_id = ?, link_kind = ? WHERE id = ?",
                       (opp_id, kind, income_id))
    store.set_opportunity(connection, opp_id, paid_at=store.iso(), paid_note=note)
    store.log_event(connection, "opportunity", opp_id, "paid_onchain",
                    ("Оплата подтверждена блокчейном (автосопоставление по сумме)" if kind == "auto"
                     else "Оплата подтверждена блокчейном (привязана оператором)") + f": {note}",
                    {"income_id": income_id}, actor)


def scan_all(fetch: Fetcher = egress.fetch) -> List[Dict[str, Any]]:
    settings = store.get_settings()
    address = settings["wallet"]["address"]
    results = []
    if not address:
        return results
    for network in settings["wallet"]["networks"]:
        try:
            results.append(scan_network(network, address, fetch))
        except (RpcError, egress.EgressError, ValueError, KeyError) as exc:
            logger.info("Wallet scan %s failed: %s", network, exc)
            with store.tx() as connection:
                connection.execute(
                    "UPDATE earner_wallet_cursor SET error = ?, checked_at = ? WHERE network = ? AND address = ?",
                    (str(exc)[:300], store.iso(), network, address))
            results.append({"network": network, "error": str(exc)[:300]})
    return results


def list_income(limit: int = 200) -> Dict[str, Any]:
    settings = store.get_settings()
    with store.read() as connection:
        rows = [dict(row) for row in connection.execute(
            "SELECT * FROM earner_income ORDER BY block_number DESC, created_at DESC LIMIT ?", (limit,))]
        cursors = [dict(row) for row in connection.execute(
            "SELECT * FROM earner_wallet_cursor WHERE address = ?", (settings["wallet"]["address"],))]
        totals = connection.execute(
            "SELECT COALESCE(SUM(amount_usd), 0) AS total, "
            "COALESCE(SUM(CASE WHEN opportunity_id != '' THEN amount_usd ELSE 0 END), 0) AS linked "
            "FROM earner_income").fetchone()
    for row in rows:
        row["explorer_url"] = NETWORKS.get(row["network"], {}).get("explorer", "") + row["tx_hash"]
    total = round(float(totals["total"]), 2)
    linked = round(float(totals["linked"]), 2)
    return {"wallet": settings["wallet"], "income": rows, "cursors": cursors, "total_usd": total,
            "linked_usd": linked,
            "note": "Стейблкоины USDC/USDT считаются 1:1 к доллару. Это поступления на адрес, подтверждённые "
                    "блокчейном; заработком по задаче считается только привязанное поступление."}
