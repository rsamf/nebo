import nebo as nb


@nb.fn()
def load_data():
    """Load raw data."""
    nb.log_text("status", "Loading data")
    return [1, 2, 3]


@nb.fn()
def transform(data):
    """Transform data."""
    nb.log_text("status", f"Transforming {len(data)} items")
    return [x * 2 for x in data]


def run():
    records = load_data()
    return transform(records)


run()
