"""Chat text for DataContractGate results and the incident log."""

from __future__ import annotations

import json

from data_room.validate import BatchReport, FailureFacts


def format_raw_check(report: BatchReport) -> str:
    facts = report.facts
    if facts.failed == 0:
        return (
            f"DataContractGate PASS  {facts.drop_path}\n"
            f"{facts.passed} / {facts.total} rows valid. No quarantine."
        )
    lines = [
        f"DataContractGate FAIL  {facts.drop_path}",
        f"contract: {facts.contract_name}",
        f"{facts.total} rows · {facts.passed} passed · {facts.failed} failed",
        "",
        "field      failed",
    ]
    for name, count in facts.by_field.items():
        lines.append(f"{name:<10} {count}")
    if report.quarantine_dir is not None:
        lines.append("")
        lines.append(f"quarantine: quarantine/{report.quarantine_dir.name}/rejected.csv")
    if facts.examples:
        lines.append("")
        lines.append("examples:")
        for example in facts.examples:
            lines.append(
                f"- row {example['row_number']} ({example['fields']}): {example['message']}"
            )
    return "\n".join(lines)


def format_recheck(report: BatchReport) -> str:
    facts = report.facts
    if facts.failed == 0:
        return (
            f"DataContractGate PASS  {facts.drop_path}\n"
            f"{facts.total} / {facts.total} rows valid "
            f"({facts.orders} orders, {facts.refunds} routed refunds, 0 quarantined)."
        )
    return (
        f"DataContractGate FAIL  {facts.drop_path}\n"
        f"{facts.failed} rows still fail after the normalizer. "
        f"{facts.orders} orders passed, {facts.refunds} refunds routed."
    )


def format_facts_for_fixer(facts: FailureFacts) -> str:
    payload = json.dumps(facts.as_dict(), indent=2)
    return (
        "Failure facts from DataContractGate. Cite only these counts, columns, "
        f"and files. Contract {facts.contract_name} stays in force.\n\n"
        f"```json\n{payload}\n```"
    )


def format_incident_log(facts: FailureFacts, *, clean: bool) -> str:
    if not clean:
        return (
            f"# Incident log\n\n"
            f"- {facts.drop_path}: {facts.failed} rows quarantined "
            f"({', '.join(facts.by_field) or 'none'}).\n"
        )
    return (
        "# Incident log\n\n"
        f"- {facts.drop_path}: resolved. {facts.total} / {facts.total} rows valid "
        f"({facts.orders} orders, {facts.refunds} routed refunds, 0 quarantined).\n"
    )
