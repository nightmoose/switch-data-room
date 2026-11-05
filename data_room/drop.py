"""Deterministic partner-order CSV drops for the room demo."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

from data_room.paths import DEFAULT_DROP

COLUMNS = ["order_id", "sku", "quantity", "currency", "status"]
CURRENCIES = ["USD", "EUR", "GBP"]
STATUSES = ["placed", "paid", "shipped"]


def generate_orders(
    total: int = 240,
    currency_failures: int = 59,
    quantity_failures: int = 4,
) -> list[dict[str, object]]:
    """Build a drop with disjoint scripted failures.

    The default batch is the room story: 240 rows, 59 lowercase ``usd``
    values, and 4 negative quantities. Those 63 rows fail ``orders_contract``;
    the other 177 pass.
    """
    if total < 1:
        raise ValueError("total must be positive")
    if currency_failures < 0 or quantity_failures < 0:
        raise ValueError("failure counts must be non-negative")
    if currency_failures + quantity_failures > total:
        raise ValueError("failure counts exceed total rows")

    rows: list[dict[str, object]] = []
    for index in range(total):
        currency = CURRENCIES[index % len(CURRENCIES)]
        quantity: int = 1 + (index % 20)
        if index < currency_failures:
            currency = "usd"
        elif index < currency_failures + quantity_failures:
            quantity = -1 * (1 + (index % 3))
        rows.append(
            {
                "order_id": f"ORD-{index + 1:06d}",
                "sku": f"SKU-{_sku(index)}",
                "quantity": quantity,
                "currency": currency,
                "status": STATUSES[index % len(STATUSES)],
            }
        )
    return rows


def write_drop(path: Path, rows: list[dict[str, object]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return path


def _sku(index: int) -> str:
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    chars = []
    value = index + 1
    for _ in range(4):
        chars.append(alphabet[value % len(alphabet)])
        value //= len(alphabet)
    return "".join(reversed(chars))


def main() -> None:
    parser = argparse.ArgumentParser(description="Write a dirty orders CSV drop.")
    parser.add_argument("--output", type=Path, default=DEFAULT_DROP)
    parser.add_argument("--rows", type=int, default=240)
    args = parser.parse_args()
    path = write_drop(args.output, generate_orders(total=args.rows))
    print(path)


if __name__ == "__main__":
    main()
