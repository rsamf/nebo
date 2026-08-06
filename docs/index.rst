Nebo
####

.. rst-class:: lead

    A modern, local-first logging SDK for multi-modal experiment data built for humans and AI agents.

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

Nebo is a light-weight, multimodal logging SDK that lets you track experiments without you needing to create an account.

.. code-block:: python

    import math

    for step in range(50):
        nb.log_line("sine", math.sin(step / 5))
        nb.log_line("cosine", math.cos(step / 5))

.. raw:: html

    <iframe
        src="https://rsamf-nebo-demos.hf.space/?run=docs-index-pipeline&flat&metrics"
        width="100%" height="400"
        style="margin-top: 10px; border: 1px solid var(--color-border, #e5e7eb); border-radius: 8px;"
        loading="lazy">
    </iframe>

Nebo also supports function-level logging which allows you to decorate functions with ``@nb.fn()``, and nebo automatically infers the DAG from your runtime calls.
and inspect metrics with function-level granularity.

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
        src="https://rsamf-nebo-demos.hf.space/?run=docs-index-multi-modal&dag"
        width="100%" height="500"
        style="margin-top: 10px; border: 1px solid var(--color-border, #e5e7eb); border-radius: 8px;"
        loading="lazy">
    </iframe>


Why Nebo?
*********

*
    Following the Tensorboard model, Nebo is **local-first**, so you don't need to start another separate service, or worse, create an account to log data.
    Each run is stored in one .nebo file, a self-contained file format for simplicity, so that managing them is easy.

*
    The UI is mobile-first supporting live viewing of metrics while you walk away from your desk.

*
    Nebo agent skills are released with every version and can be installed with ``nebo skills install``
    allowing coding agents to understand the SDK, monitor the logs, and author its own logs. Nebo allows for fully autonomous experiments with your favorite coding agent.

*
    Nebo introduces function-level logging, ideal for visualizing the flow of inputs and outputs across DAG- or pipeline-like code.

*
    You can also easily deploy Nebo as a remote service and emit logs to it. An easy one-command `nebo deploy` brings your logs to Hugging Face Spaces.

*
    See the full features below...


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
* **Groups**: Organize your runs into a tree of groups (e.g. projects), like a filesystem

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
