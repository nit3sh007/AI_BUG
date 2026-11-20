ITEMS = {}


def create_item(name):
    if name in ITEMS:
        return {"status": 409, "error": "item exists"}
    ITEMS[name] = {"id": len(ITEMS) + 1, "name": name}
    return {"status": 201, "item": ITEMS[name]}
