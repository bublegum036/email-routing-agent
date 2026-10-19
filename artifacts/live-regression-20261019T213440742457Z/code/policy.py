"""Conservative data-delivery boundary: critical content is handled by staff."""

SOURCE_KINDS = ("messages", "threads", "customers", "contacts", "contracts", "orders", "tickets", "recipients")


def critical_record(record, source_kind=None):
    """Trusted source metadata, never model claims or instructions in the body.

    Personal data, customer contracts and document workflow require staff.
    Payment invoices are excluded from the document-workflow category, but
    personal data inside an invoice still requires staff. Unclassified is restricted.
    """
    classification = record.get("data_classification", "restricted")
    if classification == "synthetic":
        return False
    if source_kind == "contracts":
        return True
    if classification != "operational" or record.get("contains_personal_data") is not False:
        return True
    document_type = record.get("document_type")
    return bool(document_type and document_type != "payment_invoice")


def cloud_context_allowed(store):
    """Conservative whole-dataset gate until source-specific redaction is implemented."""
    return all(not critical_record(record, kind) for kind in SOURCE_KINDS for record in store.all(kind))


def public_report(report):
    """Export references only; full critical content remains in the staff's local store."""
    report["redacted_for_staff"] = True
    report["message"] = {k: report["message"][k] for k in ("id", "thread_id")}
    report["issues"] = [dict(i, payload={"subject": "Содержимое доступно только сотруднику"}) for i in report["issues"]]
    report["events"] = [dict(e, payload=e["payload"] if e["kind"] == "run_end" else {"redacted": "staff_only"})
                        for e in report["events"]]
    report["actions"] = [{k: v for k, v in a.items() if k != "recipient_address"} for a in report["actions"]]
    report["outgoing"] = [{k: v for k, v in o.items() if k in {"id", "message_id", "issue_id", "kind"}} for o in report["outgoing"]]
    report["reviews"] = [{k: v for k, v in r.items() if k in {"id", "message_id", "issue_id", "reason", "state", "kind", "queue_ref"}}
                         for r in report["reviews"]]
    return report


def routing_notice(message_id, issue_id, ticket_id, recipient_ref, identifier):
    """No source text, model summary, customer facts or attachments in automatic mail."""
    return {"id": identifier, "message_id": message_id, "issue_id": issue_id,
            "ticket_id": ticket_id, "recipient_ref": recipient_ref,
            "kind": "staff_routing_notice", "attachments": [],
            "text": "Входящее обращение назначено сотруднику. Передача содержимого требует его проверки.",
            "content_delivery": "requires_staff"}
