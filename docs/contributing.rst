.. _repository: https://github.com/rsamf/nebo

.. _contributing:

Contributing
############

To learn how to run Nebo in development mode, visit our Github repository_.

If you would like to contribute to the development of Nebo, please fork the repo and submit your PR.

Building the docs locally
**************************

The documentation is built with Sphinx. The build dependencies live in the
``dev`` dependency group, so a normal dev setup already has them::

    uv sync --all-groups

Then build the HTML from the ``docs/`` directory::

    cd docs
    uv run sphinx-build -b html . _build/html

(``uv run make html`` works the same way if you have ``make`` installed.)

Open ``docs/_build/html/index.html`` in a browser to preview. Sphinx only
rebuilds pages that changed, so re-run the same command after edits; delete
``docs/_build`` first if you want a clean rebuild.
