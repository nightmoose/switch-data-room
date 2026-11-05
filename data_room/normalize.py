"""Route negative quantities out of the order feed and uppercase currency codes.

``check`` does not call this. ``recheck`` does, after AISchemaGate has passed
and a person has replied ``approve patch``.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from data_room.drop import COLUMNS

_CURRENCY = {
    "usd": "USD",
    "eur": "EUR",
    "gbp": "GBP",
}


def normalize_batch(
    rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    orders: list[dict[str, Any]] = []
    refunds: list[dict[str, Any]] = []
    for row in rows:
        kind, normalized = normalize_row(row)
        if kind == "refund":
            refunds.append(normalized)
        else:
            orders.append(normalized)
    return orders, refunds


def normalize_row(row: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    updated = dict(row)
    raw_currency = str(row.get("currency", ""))
    updated["currency"] = _CURRENCY.get(raw_currency.lower(), raw_currency)
    quantity = int(row["quantity"])
    updated["quantity"] = quantity
    if quantity < 1:
        return "refund", updated
    return "order", updated


def write_split(
    orders: list[dict[str, Any]],
    refunds: list[dict[str, Any]],
    *,
    orders_path: Path,
    refunds_path: Path,
) -> None:
    _write(orders_path, orders)
    _write(refunds_path, refunds)


def _write(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
