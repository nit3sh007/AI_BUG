def paginate(items, page, per_page):
    """Return the items on a 1-based page number."""
    start = page * per_page
    return list(items)[start:start + per_page]
