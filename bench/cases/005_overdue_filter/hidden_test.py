from datetime import date

from invoices import overdue_invoices

TODAY = date(2026, 6, 15)


def test_due_today_is_not_overdue():
    inv = [{"id": 1, "due_date": TODAY, "paid": False}]
    assert overdue_invoices(inv, TODAY) == []


def test_paid_invoice_is_not_overdue():
    inv = [{"id": 2, "due_date": date(2026, 6, 1), "paid": True}]
    assert overdue_invoices(inv, TODAY) == []


def test_mixed():
    inv = [
        {"id": 1, "due_date": date(2026, 6, 1), "paid": False},
        {"id": 2, "due_date": TODAY, "paid": False},
        {"id": 3, "due_date": date(2026, 6, 1), "paid": True},
    ]
    assert [i["id"] for i in overdue_invoices(inv, TODAY)] == [1]
