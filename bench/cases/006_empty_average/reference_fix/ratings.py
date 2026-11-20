def average_rating(reviews):
    if not reviews:
        return None
    return round(sum(r["stars"] for r in reviews) / len(reviews), 1)
