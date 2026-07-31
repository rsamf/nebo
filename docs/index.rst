Nebo
####

.. rst-class:: lead

    A modern logging SDK for multi-modal data.

.. code-block:: python

    import nebo as nb

    nb.log_text("hello", "Hello world!")

.. raw:: html

    <iframe
        src="https://rsamf-nebo-demos.hf.space/?run=docs-index-hello-world&text"
        width="100%" height="100"
        style="margin-top: 10px; border: 1px solid var(--color-border, #e5e7eb); border-radius: 8px;"
        loading="lazy">
    </iframe>

Nebo is a modern logging SDK that lets you track experiments containing multi-modal data (text, images, audio) and inspect metrics with function-level granularity.

.. code-block:: python

    @nb.fn()
    def load_images():
        images = []
        for i in range(4):
            im = _make_synthetic_image(i)
            images.append(im)
            nb.log_image(Image.fromarray(im), name="images", step=i)
        return images

    @nb.fn()
    def log_brightness(images):
        for im in images:
            nb.log_line("brightness", im.mean())
        

    def run():
        data = load_images()
        log_brightness(data)

    if __name__ == "__main__":
        run()

.. raw:: html

    <iframe
        src="https://rsamf-nebo-demos.hf.space/?run=docs-index-multi-modal"
        width="100%" height="500"
        style="margin-top: 10px; border: 1px solid var(--color-border, #e5e7eb); border-radius: 8px;"
        loading="lazy">
    </iframe>


Following the Tensorboard model, Nebo is local-first, so you don't need to start another separate service, or worse, create an account to log data.
Each run is stored in one .nebo file, a self-contained file format for simplicity, so that managing them is easy.


Nebo also supports function-level logging which allows you to decorate functions with ``@nb.fn()``, and nebo automatically infers the DAG from your runtime calls.

.. code-block:: python

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
            nb.log_line("value", r["value"]**2)

    def run():
        data = load_data()
        evaluate(data)
        process(data)

    if __name__ == "__main__":
        run()

.. raw:: html

    <iframe
        src="https://rsamf-nebo-demos.hf.space/?run=docs-index-pipeline"
        width="100%" height="500"
        style="margin-top: 10px; border: 1px solid var(--color-border, #e5e7eb); border-radius: 8px;"
        loading="lazy">
    </iframe>

Text, metrics, images, and audio are captured and surfaced through a web UI, an MCP server, and a nebo CLI that comes with agent skills.
The UI is mobile-first supporting live viewing of metrics while you walk away from your desk.

Features
********

* **Multimodal logging**: Text, scalar metrics, images (PIL/numpy/torch), and audio
* **Progress tracking**: ``nb.track()`` for tqdm-like progress bars in the and UI
* **Persistent .nebo files**: Append-only binary log files using MessagePack for crash-safe persistence
* **Web UI**: Mobile-first viewing of metrics charting, image/audio viewers, run comparison, and DAG visualization
* **Skills & MCP integration**: A full nebo CLI, 2 agent skills, and MCP server for AI agents to observe, control, and *push data into* pipelines (incl. ``log_line`` / ``log_image`` / ``log_audio`` / ``nb.log_text``)
* **UI configuration from code**: ``nb.ui()`` and ``@nb.fn(ui={})`` set display defaults
* **Notebook embedding**: ``nb.show()`` returns a Jupyter-renderable iframe of any slice of a run
* **Hugging Face Spaces deploy**: ``nebo deploy`` ships the daemon to a Space with shared-secret auth and configurable public/private read+write modes
* **Decorator-based**: Add ``@nb.fn()`` to functions or classes for function-level logging
* **Automatic DAG inference**: Edges are created from data flow between decorated functions

.. toctree::
   :caption: Docs
   :titlesonly:
   :maxdepth: 3
   :hidden:

   installing
   guide
   reference
   cli
   contributing
