def overdue_invoices(invoices, today):
    """Invoices whose due_date is a datetime.date; `today` is a datetime.date."""
    return [i for i in invoices if i["due_date"] <= today]
