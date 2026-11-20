def average_rating(reviews):
    return round(sum(r["stars"] for r in reviews) / len(reviews), 1)
