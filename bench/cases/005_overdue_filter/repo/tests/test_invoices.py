from datetime import date

from invoices import overdue_invoices

TODAY = date(2026, 6, 15)


def test_past_due_unpaid_is_overdue():
    inv = [{"id": 1, "due_date": date(2026, 6, 1), "paid": False}]
    assert [i["id"] for i in overdue_invoices(inv, TODAY)] == [1]


def test_future_due_is_not_overdue():
    inv = [{"id": 2, "due_date": date(2026, 7, 1), "paid": False}]
    assert overdue_invoices(inv, TODAY) == []
