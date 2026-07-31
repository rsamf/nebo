import nebo as nb


@nb.fn()
def setup():
    """Initialize shared resources."""
    nb.log_text("status", "Setting up")


@nb.fn(depends_on=[setup])
def process():
    """Uses resources initialized by setup."""
    nb.log_text("status", "Processing")


setup()
process()
