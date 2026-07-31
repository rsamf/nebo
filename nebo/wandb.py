"""Drop-in compatibility shim for wandb.

Lets existing wandb scripts run unchanged on nebo:

    import nebo.wandb as wandb
    wandb.init(project="my_proj", name="run-1", config={"lr": 0.001})
    wandb.log({"loss": 0.5, "accuracy": 0.92})
    wandb.finish()

Only the most common surface is implemented (init, log, finish, config, run).
Anything exotic (artifacts, sweeps, model registry, watch, save, ...) is not
supported.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import nebo as nb

logger = logging.getLogger(__name__)


class _Config:
    """Dict-like proxy that mirrors wandb.config behavior.

    Both attribute and item access are supported, and updates are forwarded
    to ``nb.log_cfg`` so the values land on the run's global config.
    """

    def __init__(self) -> None:
        object.__setattr__(self, "_data", {})

    def update(self, d: Optional[dict] = None, **kwargs: Any) -> None:
        merged: dict = dict(d) if d else {}
        merged.update(kwargs)
        self._data.update(merged)
        if merged:
            nb.log_cfg(merged)

    def _set_quiet(self, d: dict) -> None:
        """Populate values without re-emitting to nebo (used by init() when
        the values are already being sent via start_run(config=...))."""
        self._data.update(d)

    def __setattr__(self, k: str, v: Any) -> None:
        if k.startswith("_"):
            object.__setattr__(self, k, v)
            return
        self._data[k] = v
        nb.log_cfg({k: v})

    def __getattr__(self, k: str) -> Any:
        if k.startswith("_"):
            raise AttributeError(k)
        try:
            return self._data[k]
        except KeyError:
            raise AttributeError(k)

    def __setitem__(self, k: str, v: Any) -> None:
        self._data[k] = v
        nb.log_cfg({k: v})

    def __getitem__(self, k: str) -> Any:
        return self._data[k]

    def get(self, k: str, default: Any = None) -> Any:
        return self._data.get(k, default)

    def items(self):
        return self._data.items()

    def keys(self):
        return self._data.keys()

    def __iter__(self):
        return iter(self._data)

    def __contains__(self, k: object) -> bool:
        return k in self._data

    def as_dict(self) -> dict:
        return dict(self._data)


config = _Config()


class _Run:
    """Minimal stub for ``wandb.run``."""

    def __init__(self) -> None:
        self.id: Optional[str] = None
        self.name: Optional[str] = None
        self.project: Optional[str] = None

    def __repr__(self) -> str:
        return f"<wandb-shim Run id={self.id!r} name={self.name!r} project={self.project!r}>"


run = _Run()


def init(
    project: Optional[str] = None,
    name: Optional[str] = None,
    config: Optional[dict] = None,
    **kwargs: Any,
) -> _Run:
    """Drop-in replacement for ``wandb.init``.

    Auto-initializes nebo, starts a run with the given ``name``/``config``,
    and populates the module-level ``run`` and ``config`` proxies.

    Extra ``kwargs`` (entity, group, tags, mode, ...) are accepted for API
    compatibility but ignored.
    """
    if kwargs:
        logger.debug("nebo.wandb.init: ignoring unsupported kwargs: %s", list(kwargs))
    # Use the lazy auto-init path so calling wandb.init() repeatedly (or
    # after another nb.* call already initialized) doesn't trigger nebo's
    # "already initialized" warning.
    nb._ensure_init()
    ctx = nb.start_run(name=name, config=config)
    run.id = ctx.run_id
    run.name = name
    run.project = project
    if config:
        # start_run already forwards `config` to nebo as run_config; populate
        # the proxy locally so `wandb.config.lr` reads work without
        # re-emitting the same values.
        globals()["config"]._set_quiet(dict(config))
    return run


def _looks_like_image(v: Any) -> bool:
    """Best-effort: numpy ndarray with 2 or 3 dims, or PIL.Image.Image."""
    type_name = type(v).__name__
    module = (type(v).__module__ or "")
    if type_name == "Image" and "PIL" in module:
        return True
    if type_name == "ndarray" and "numpy" in module:
        shape = getattr(v, "shape", ())
        return len(shape) in (2, 3)
    return False


def log(data: dict, step: Optional[int] = None, commit: bool = True) -> None:
    """Drop-in replacement for ``wandb.log``.

    Each key/value is dispatched by value type:
      - numeric → ``nb.log_line`` (line chart)
      - image-like (numpy 2D/3D, PIL Image) → ``nb.log_image``
      - other → cast to float if possible, else logged via ``nb.log_text``

    The ``commit`` kwarg is accepted for API parity but ignored — every call
    is a single emission.
    """
    del commit
    for k, v in data.items():
        if isinstance(v, bool):
            # bool is an int subclass; record as a 0/1 metric.
            nb.log_line(k, int(v), step=step)
            continue
        if isinstance(v, (int, float)):
            nb.log_line(k, v, step=step)
            continue
        if _looks_like_image(v):
            nb.log_image(v, name=k, step=step)
            continue
        try:
            nb.log_line(k, float(v), step=step)
        except (TypeError, ValueError):
            nb.log_text(k, str(v), step=step)


def finish(exit_code: int = 0, *, quiet: Optional[bool] = None) -> None:
    """Drop-in replacement for ``wandb.finish``. No-op — nebo's run lifecycle
    is handled by ``nb.start_run``."""
    del exit_code, quiet
