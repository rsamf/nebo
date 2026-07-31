import nebo as nb


@nb.fn(ui={"default_tab": "metrics"})
def load_data():
    records = [{"id": i, "value": i * 0.5} for i in range(200)]
    nb.log_text("status", f"Loaded {len(records)} records")
    for r in records:
        nb.log_line("value", r["value"])
    return records


@nb.fn(ui={"default_tab": "metrics"})
def evaluate(records):
    for r in records:
        if r["value"] < 50:
            nb.log_line("value", r["value"], tags=["<50"])
            nb.log_text("findings", f"Found {r['value']} is under 50")
        else:
            nb.log_line("value", r["value"], tags=[">=50"])


@nb.fn()
def process(records):
    for r in nb.track(records, name="processing"):
        nb.log_line("value", r["value"] ** 2)


def run():
    data = load_data()
    evaluate(data)
    process(data)


if __name__ == "__main__":
    run()
