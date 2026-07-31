import nebo as nb

nb.md("""
# Image Classification Pipeline

Loads images from disk, runs inference with a pretrained ResNet,
and exports predictions to a JSON file.
""")


@nb.fn()
def classify():
    nb.log_text("status", "Classifying images")
    nb.log_line("accuracy", 0.92)


classify()
