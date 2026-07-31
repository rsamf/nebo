import nebo as nb

nb.ui(layout="horizontal", view="dag", theme="dark")


@nb.fn(ui={"color": "#fb923c"})
def data_loader():
    nb.log_text("status", "Loading data")
    return [1, 2, 3]


@nb.fn(ui={"color": "#34d399", "default_tab": "metrics"})
def train(data):
    nb.log_text("status", f"Training on {len(data)} items")
    for step in range(20):
        nb.log_line("loss", 1.0 / (step + 1))


train(data_loader())
