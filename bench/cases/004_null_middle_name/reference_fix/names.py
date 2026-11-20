def format_name(user):
    first = user["first_name"]
    last = user["last_name"]
    middle = user.get("middle_name")
    if middle:
        return f"{first} {middle[0]}. {last}"
    return f"{first} {last}"
