"""Tests for nb.show() — the notebook iframe renderable."""

from __future__ import annotations

import pytest

import nebo as nb
from nebo.core.state import SessionState, get_state


def _reset() -> None:
    SessionState.reset_singleton()


class TestShow:
    def setup_method(self) -> None:
        _reset()

    def test_no_active_run_returns_hint(self) -> None:
        handle = nb.show()
        assert handle.url is None
        html = handle._repr_html_()
        assert "No active run" in html
        # Doesn't render an iframe.
        assert "<iframe" not in html

    def test_default_url_is_run_view(self) -> None:
        state = get_state()
        state._active_run_id = "abc123"
        handle = nb.show()
        assert handle.url is not None
        # Bare `?run=...` is the full-run dashboard; no extra slice flags.
        assert "run=abc123" in handle.url
        assert "view=" not in handle.url
        assert "<iframe" in handle._repr_html_()

    def test_node_alone_is_node_detail(self) -> None:
        state = get_state()
        state._active_run_id = "r1"
        handle = nb.show(node="train")
        assert "run=r1" in handle.url
        assert "node=train" in handle.url

    def test_single_metric(self) -> None:
        state = get_state()
        state._active_run_id = "r1"
        handle = nb.show(node="train", metric="loss")
        assert "run=r1" in handle.url
        assert "node=train" in handle.url
        assert "metric=loss" in handle.url

    def test_metrics_gallery(self) -> None:
        state = get_state()
        state._active_run_id = "r1"
        handle = nb.show(metric=True)
        assert "run=r1" in handle.url
        assert "metrics" in handle.url
        # Gallery emits the bare flag, not metric=
        assert "metric=" not in handle.url

    def test_dag_flag(self) -> None:
        state = get_state()
        state._active_run_id = "r1"
        handle = nb.show(dag=True)
        assert "dag" in handle.url

    def test_text_flag_with_node_filter(self) -> None:
        state = get_state()
        state._active_run_id = "r1"
        handle = nb.show(node="train", text=True)
        # True emits the bare flag, not text=
        assert "text" in handle.url
        assert "text=" not in handle.url
        assert "node=train" in handle.url

    def test_image_and_audio_singular(self) -> None:
        state = get_state()
        state._active_run_id = "r1"
        h_img = nb.show(image="hero.png")
        assert "image=hero.png" in h_img.url
        h_aud = nb.show(audio="bell.wav")
        assert "audio=bell.wav" in h_aud.url

    def test_at_most_one_slice(self) -> None:
        state = get_state()
        state._active_run_id = "r1"
        with pytest.raises(ValueError):
            nb.show(metric=True, text=True)

    def test_explicit_run_overrides_active(self) -> None:
        state = get_state()
        state._active_run_id = "active"
        handle = nb.show(run="other")
        assert "run=other" in handle.url

    def test_iframe_dimensions(self) -> None:
        state = get_state()
        state._active_run_id = "r1"
        handle = nb.show(width=800, height=400)
        html = handle._repr_html_()
        assert 'width="800px"' in html
        assert 'height="400px"' in html

    def test_url_uses_state_port(self) -> None:
        state = get_state()
        state._active_run_id = "r1"
        state.port = 9999
        handle = nb.show()
        assert "http://localhost:9999/" in handle.url
