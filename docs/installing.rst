.. _Installing:

Installing
##########

Requirements
============

* Python 3.10+

Install from PyPI
=================

.. code-block:: bash

    pip install nebo

Or with `uv <https://docs.astral.sh/uv/>`_:

.. code-block:: bash

    uv add nebo

Optional extras
---------------

To use ``nebo deploy`` (push the daemon to a Hugging Face Space),
also install ``huggingface_hub``:

.. code-block:: bash

    pip install 'nebo[deploy]'

Agent skills
============

While nebo allows humans to have a very flexible viewing experience, one of nebo's primary goals is to give coding agents the full capacity to build.

Nebo provides 2 agent skills and can be installed automatically with the nebo CLI.

* **instrumentation** - used when agents are building nebo-integrated Python code, so they understand how to log and what to log.
* **runs-qa** - used when agents are asked about nebo runs and asked to generate derived metrics on the active nebo service.

Claude Code
-----------

Install all of the skills onto Claude Code.

.. code-block:: bash

    nebo skills install

Codex
-----

Install all of the skills onto Codex CLI (they land in
``~/.codex/skills``, where Codex auto-discovers them; add ``--project``
for ``.codex/skills`` in the current repo).

.. code-block:: bash

    nebo skills install --platform codex

Other platforms
---------------

Install all of the skills the cross-platform way.

.. code-block:: bash

    nebo skills install --platform agents-md

Quick Start
===========

Run a pipeline (writes events to ``./.nebo/`` by default, no daemon needed):

.. code-block:: bash

    python my_pipeline.py

Or start the daemon for live observability with a web UI:

.. code-block:: bash

    nebo serve

Then visit http://localhost:7861 to see the UI.


Install from Source
===================

.. _uv: https://docs.astral.sh/uv/
.. _Node.js: https://nodejs.org/

Installing from source requires uv_ and Node.js_.

.. code-block:: bash

    git clone https://github.com/graphbookai/nebo.git
    cd nebo
    uv sync --all-groups

To build the web UI from source:

.. code-block:: bash

    cd ui
    npm ci
    npm run build
    cp -r dist/* ../nebo/server/static/
