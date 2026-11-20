def format_name(user):
    first = user["first_name"]
    last = user["last_name"]
    middle = user["middle_name"][0] + "."
    return f"{first} {middle} {last}"
