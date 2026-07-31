"""Tests for the logging API.

After the v3 redesign, the SDK no longer mirrors metric values, image
metadata, or audio metadata in process — those flow straight to the
daemon. Tests that used to inspect ``state.loggables[*].metrics`` /
``.images`` / ``.audio`` now attach a ``CapturingClient`` (see
``tests/conftest.py``) and assert on the captured wire events. Tests
that read ``loggable.texts`` still work because the SDK keeps a
bounded ring of recent text entries for the terminal display.
"""

from __future__ import annotations

import pytest

from nebo.core.state import (
    MetricCursor,
    NodeInfo,
    SessionState,
    get_state,
)
from nebo.core.decorators import fn
from nebo.logging.logger import (
    log_bar,
    log_histogram,
    log_line,
    log_pie,
    log_scatter,
    log_text,
    md,
)
from nebo.core.config import log_cfg


def _node_by_func_name(name: str) -> NodeInfo:
    return next(
        l for l in get_state().loggables.values()
        if isinstance(l, NodeInfo) and l.func_name == name
    )


class TestLogging:
    """Tests for nb.log_text() and the typed log_* helpers."""

    def setup_method(self) -> None:
        SessionState.reset_singleton()

    def test_log_text_name_is_required(self) -> None:
        """name is a required non-empty string — there is no default."""
        import nebo as nb
        with pytest.raises(TypeError):
            nb.log_text("", "msg")
        with pytest.raises(TypeError):
            nb.log_text(None, "msg")  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            nb.log_text("msg")  # type: ignore[call-arg]  # message missing

    def test_log_text_name(self) -> None:
        import nebo as nb
        from nebo.core.state import get_state
        nb.log_text("status", "hi")
        entry = get_state().loggables["__global__"].texts[-1]
        assert entry["name"] == "status"
        assert entry["message"] == "hi"

    def test_log_text_emits_text_event_with_step(self, capturing_client) -> None:
        """log_text sends a {"type": "text"} wire event carrying name + step
        (and no level field — that concept is alerts-only now)."""
        import nebo as nb
        nb.log_text("status", "msg", step=3)
        (event,) = capturing_client.by_type("text")
        assert event["type"] == "text"
        assert event["name"] == "status"
        assert event["message"] == "msg"
        assert event["step"] == 3
        assert event["loggable_id"] == "__global__"
        assert "level" not in event

    def test_log_text_inside_step(self) -> None:
        """log_text() inside a step should attach to that node's recent texts ring."""
        @fn()
        def my_step():
            log_text("text", "hello world")

        my_step()
        node = _node_by_func_name("my_step")
        assert len(node.texts) == 1
        assert list(node.texts)[0]["message"] == "hello world"

    def test_log_line_inside_step_emits_wire_events(self, capturing_client) -> None:
        """log_line() should send one metric event per call and lock the cursor type."""
        @fn()
        def train():
            log_line("loss", 0.5, step=0)
            log_line("loss", 0.3, step=1)
            log_line("loss", 0.1, step=2)

        train()
        events = capturing_client.metrics_named("loss")
        assert [e["step"] for e in events] == [0, 1, 2]
        assert [e["value"] for e in events] == [0.5, 0.3, 0.1]
        assert all(e["metric_type"] == "line" for e in events)
        # The cursor's locked type is the only metric metadata still
        # held on the SDK side.
        node = _node_by_func_name("train")
        cursor = get_state()._metric_cursors[node.loggable_id]["loss"]
        assert isinstance(cursor, MetricCursor)
        assert cursor.type == "line"

    def test_md_outside_run_writes_script_template(self) -> None:
        """md() with no live run is declarative: it fills the script-level
        template and materializes nothing."""
        md("This is a test workflow")
        state = get_state()
        assert state._script_description == "This is a test workflow"
        assert state.workflow_description is None
        assert state._run_materialized is False

    def test_md_outside_run_appends_to_template(self) -> None:
        md("Part 1")
        md("Part 2")
        tpl = get_state()._script_description
        assert tpl is not None and "Part 1" in tpl and "Part 2" in tpl

    def test_md_inside_live_run_sets_workflow_description(self) -> None:
        log_text("text", "materialize")  # first real event opens the run
        md("This is a test workflow")
        assert get_state().workflow_description == "This is a test workflow"

    def test_md_inside_live_run_appends(self) -> None:
        log_text("text", "materialize")
        md("Part 1")
        md("Part 2")
        wd = get_state().workflow_description
        assert wd is not None and "Part 1" in wd and "Part 2" in wd


class TestLogNumpy:
    """Tests for log_text() with numpy arrays — read off the recent texts deque."""

    def setup_method(self) -> None:
        SessionState.reset_singleton()

    def test_log_numpy_array(self) -> None:
        try:
            import numpy as np
        except ImportError:
            pytest.skip("numpy not installed")

        @fn()
        def check_array():
            arr = np.zeros((3, 224, 224), dtype=np.float32)
            log_text("text", arr)

        check_array()
        node = _node_by_func_name("check_array")
        msg = list(node.texts)[0]["message"]
        assert "ndarray" in msg
        assert "(3, 224, 224)" in msg
        assert "float32" in msg
        assert "min:" in msg and "max:" in msg and "mean:" in msg

    def test_log_numpy_preserves_string(self) -> None:
        @fn()
        def my_step():
            log_text("text", "plain text message")

        my_step()
        node = _node_by_func_name("my_step")
        assert list(node.texts)[0]["message"] == "plain text message"


class TestLogCfg:
    """Tests for nb.log_cfg() — config still lives on the node."""

    def setup_method(self) -> None:
        SessionState.reset_singleton()

    def test_log_cfg_stores_params(self) -> None:
        @fn()
        def train():
            log_cfg({"lr": 0.001, "batch_size": 32})

        train()
        node = _node_by_func_name("train")
        assert node.params["lr"] == 0.001
        assert node.params["batch_size"] == 32

    def test_log_cfg_merges(self) -> None:
        @fn()
        def train():
            log_cfg({"lr": 0.001})
            log_cfg({"batch_size": 32})

        train()
        node = _node_by_func_name("train")
        assert node.params["lr"] == 0.001
        assert node.params["batch_size"] == 32

    def test_log_cfg_later_call_overwrites(self) -> None:
        @fn()
        def train():
            log_cfg({"lr": 0.001, "epochs": 10})
            log_cfg({"lr": 0.01})

        train()
        node = _node_by_func_name("train")
        assert node.params["lr"] == 0.01
        assert node.params["epochs"] == 10

    def test_log_cfg_filters_non_serializable(self) -> None:
        @fn()
        def train():
            log_cfg({"lr": 0.001, "callback": lambda x: x})

        train()
        node = _node_by_func_name("train")
        assert "lr" in node.params
        assert "callback" not in node.params


class TestImageSerializer:
    """Tests for image serialization via nb.log_image."""

    def setup_method(self) -> None:
        SessionState.reset_singleton()

    def test_serialize_numpy_array_uint8_hwc(self) -> None:
        import numpy as np
        from nebo.logging.serializers import serialize_image

        arr = np.zeros((10, 10, 3), dtype=np.uint8)
        png = serialize_image(arr)
        assert isinstance(png, bytes)
        assert png[:8] == b"\x89PNG\r\n\x1a\n"

    def test_serialize_numpy_grayscale(self) -> None:
        import numpy as np
        from nebo.logging.serializers import serialize_image

        arr = np.zeros((8, 8), dtype=np.uint8)
        png = serialize_image(arr)
        assert png[:8] == b"\x89PNG\r\n\x1a\n"

    def test_serialize_pil_image(self) -> None:
        from PIL import Image
        from nebo.logging.serializers import serialize_image

        img = Image.new("RGB", (4, 4), color=(10, 20, 30))
        png = serialize_image(img)
        assert png[:8] == b"\x89PNG\r\n\x1a\n"

    def test_serialize_unsupported_type_raises_typeerror(self) -> None:
        from nebo.logging.serializers import serialize_image

        with pytest.raises(TypeError, match="Cannot serialize"):
            serialize_image("not an image")

    def test_log_image_emits_wire_event(self, capturing_client) -> None:
        """nb.log_image() now sends straight to the daemon — assert on the wire event."""
        import numpy as np
        from nebo.logging.logger import log_image

        @fn()
        def f():
            log_image(np.zeros((10, 10, 3), dtype=np.uint8), name="x")

        f()
        images = capturing_client.by_type("image")
        assert len(images) == 1
        assert images[0]["name"] == "x"
        # The caller thread only validates + copies; PNG bytes are encoded
        # by the transport flush thread (resolve_media). The wire event
        # carries a PendingMedia until then.
        from nebo.logging.serializers import PendingMedia

        assert isinstance(images[0]["data"], PendingMedia)
        assert images[0]["data"].encode().startswith(b"\x89PNG")
        # The SDK no longer keeps an ``images`` list.
        node = _node_by_func_name("f")
        assert not hasattr(node, "images")

    def test_log_audio_emits_pending_media(self, capturing_client) -> None:
        import numpy as np
        from nebo.logging.logger import log_audio
        from nebo.logging.serializers import PendingMedia

        @fn()
        def f():
            log_audio(np.zeros(160, dtype=np.float32), sr=16000, name="a")

        f()
        (audio,) = capturing_client.by_type("audio")
        assert isinstance(audio["data"], PendingMedia)
        assert audio["data"].encode()[:4] == b"RIFF"


def test_log_text_outside_fn_routes_to_global():
    import nebo as nb
    nb.get_state().reset()
    nb.log_text("text", "hello from top-level")
    g = nb.get_state().loggables["__global__"]
    assert len(g.texts) == 1
    entry = list(g.texts)[0]
    assert entry["message"] == "hello from top-level"
    assert entry["loggable_id"] == "__global__"


def test_log_line_outside_fn_routes_to_global(capturing_client):
    import nebo as nb
    nb.log_line("top_lvl_metric", 3.14)
    events = capturing_client.metrics_named("top_lvl_metric")
    assert len(events) == 1
    assert events[0]["loggable_id"] == "__global__"
    assert events[0]["value"] == 3.14
    cursor = nb.get_state()._metric_cursors["__global__"]["top_lvl_metric"]
    assert cursor.type == "line"


def test_log_text_inside_fn_still_routes_to_node():
    import nebo as nb
    nb.get_state().reset()

    @nb.fn()
    def inner():
        nb.log_text("text", "from inner")
        return 1

    inner()
    state = nb.get_state()
    assert "__global__" in state.loggables
    assert len(state.loggables["__global__"].texts) == 0
    inner_loggable = next(
        lg for lg in state.loggables.values()
        if getattr(lg, "func_name", None) == "inner"
    )
    assert len(inner_loggable.texts) == 1


def test_log_image_accepts_labels_and_emits_them(capturing_client):
    import numpy as np
    from PIL import Image
    import nebo as nb

    img = Image.fromarray(np.zeros((8, 8, 3), dtype=np.uint8))
    nb.log_image(
        img,
        name="edges",
        points=nb.labels.Points([[45, 80]], color="#facc15"),
        boxes=nb.labels.Boxes([[10, 10, 50, 50], [60, 60, 70, 70]], color="#22d3ee"),
        circles=nb.labels.Circles([30, 30, 5], color="#f472b6"),
        polygons=nb.labels.Polygons([[[0, 0], [1, 0], [1, 1]]], color="#86efac"),
    )

    image_events = capturing_client.by_type("image")
    assert len(image_events) == 1
    labels = image_events[0]["labels"]
    assert labels["points"] == [{"data": [[45, 80]], "color": "#facc15"}]
    assert labels["boxes"] == [
        {"data": [[10, 10, 50, 50], [60, 60, 70, 70]], "color": "#22d3ee"}
    ]
    assert labels["circles"] == [{"data": [[30, 30, 5]], "color": "#f472b6"}]
    assert labels["polygons"] == [
        {"data": [[[0, 0], [1, 0], [1, 1]]], "color": "#86efac", "fill": True}
    ]
    assert "bitmasks" not in labels


def test_log_image_polygons_fill_flag_round_trips(capturing_client):
    import numpy as np
    from PIL import Image
    import nebo as nb

    img = Image.fromarray(np.zeros((8, 8, 3), dtype=np.uint8))
    nb.log_image(
        img,
        name="outline_only",
        polygons=nb.labels.Polygons(
            [[[0, 0], [1, 0], [1, 1]]],
            color="#86efac",
            fill=False,
        ),
    )

    image_events = capturing_client.by_type("image")
    labels = image_events[-1]["labels"]
    assert labels["polygons"] == [
        {"data": [[[0, 0], [1, 0], [1, 1]]], "color": "#86efac", "fill": False}
    ]


def test_log_image_accepts_list_of_groups_with_distinct_colors(capturing_client):
    import numpy as np
    from PIL import Image
    import nebo as nb

    img = Image.fromarray(np.zeros((8, 8, 3), dtype=np.uint8))
    nb.log_image(
        img,
        name="preds_vs_gt",
        boxes=[
            nb.labels.Boxes([[1, 1, 2, 2]], color="#22d3ee"),
            nb.labels.Boxes([[3, 3, 4, 4]], color="#22c55e"),
        ],
    )

    image_events = capturing_client.by_type("image")
    labels = image_events[0]["labels"]
    assert labels["boxes"] == [
        {"data": [[1, 1, 2, 2]], "color": "#22d3ee"},
        {"data": [[3, 3, 4, 4]], "color": "#22c55e"},
    ]


def test_log_image_rejects_raw_list_with_helpful_error(capturing_client):
    import numpy as np
    import pytest
    from PIL import Image
    import nebo as nb

    img = Image.fromarray(np.zeros((8, 8, 3), dtype=np.uint8))
    with pytest.raises(TypeError, match="nb.labels.Boxes"):
        nb.log_image(img, name="bad", boxes=[[10, 10, 20, 20]])


def test_log_image_bitmask_stored_as_media_reference(capturing_client):
    import numpy as np
    from PIL import Image
    import nebo as nb

    img = Image.fromarray(np.zeros((8, 8, 3), dtype=np.uint8))
    mask = np.zeros((8, 8), dtype=np.uint8)
    mask[2:5, 2:5] = 1
    nb.log_image(img, name="seg", bitmasks=nb.labels.Bitmasks(mask, color="#a78bfa"))

    image_events = capturing_client.by_type("image")
    assert len(image_events) == 1
    labels = image_events[0]["labels"]
    assert "bitmasks" in labels
    assert len(labels["bitmasks"]) == 1
    group = labels["bitmasks"][0]
    assert group["color"] == "#a78bfa"
    assert len(group["data"]) == 1
    entry = group["data"][0]
    assert entry["width"] == 8
    assert entry["height"] == 8
    assert "data" in entry  # inline base64


def test_log_line_emits_scalar_event(capturing_client):
    import nebo as nb
    nb.log_line("loss", 0.5)
    events = capturing_client.metrics_named("loss")
    assert events[-1]["metric_type"] == "line"
    assert events[-1]["value"] == 0.5


def test_log_bar_emits_dict_value(capturing_client):
    import nebo as nb
    nb.log_bar("class_counts", {"cat": 3, "dog": 5, "bird": 2})
    events = capturing_client.metrics_named("class_counts")
    assert events[-1]["metric_type"] == "bar"
    assert events[-1]["value"] == {"cat": 3, "dog": 5, "bird": 2}


def test_log_pie_emits_dict_value(capturing_client):
    import nebo as nb
    nb.log_pie("budget", {"prompt": 800, "completion": 200})
    events = capturing_client.metrics_named("budget")
    assert events[-1]["metric_type"] == "pie"
    assert events[-1]["value"] == {"prompt": 800, "completion": 200}


def test_log_line_tags_attached_to_emission(capturing_client):
    import nebo as nb
    nb.log_line("loss", 0.1, tags=["schedule:warmup"])
    nb.log_line("loss", 0.05, tags=["schedule:main"])
    events = capturing_client.metrics_named("loss")
    assert events[0]["tags"] == ["schedule:warmup"]
    assert events[1]["tags"] == ["schedule:main"]


def test_log_bar_does_not_accept_tags():
    """Tags only apply to accumulating metrics (line, scatter); the
    snapshot helpers (bar/pie/histogram) reject them."""
    import nebo as nb
    nb.get_state().reset()
    with pytest.raises(TypeError):
        nb.log_bar("counts", {"a": 1}, tags=["x"])  # type: ignore[call-arg]


def test_metric_type_locks_after_first_emission(capturing_client):
    import nebo as nb
    nb.log_line("m", 1.0)
    with pytest.raises(ValueError, match="type"):
        nb.log_bar("m", {"a": 1})


def test_log_histogram_emits_labeled_samples(capturing_client):
    import nebo as nb
    import numpy as np
    rng = np.random.default_rng(0)
    nb.log_histogram(
        "latencies",
        {"p50": rng.normal(size=100).tolist(), "p99": rng.normal(size=50).tolist()},
    )
    events = capturing_client.metrics_named("latencies")
    assert events[-1]["metric_type"] == "histogram"
    value = events[-1]["value"]
    assert set(value.keys()) == {"p50", "p99"}
    assert len(value["p50"]) == 100 and len(value["p99"]) == 50


def test_log_histogram_rejects_legacy_flat_list():
    """Bare list samples are no longer accepted — histogram requires a
    labeled dict."""
    import nebo as nb
    nb.get_state().reset()
    with pytest.raises(TypeError):
        nb.log_histogram("latencies", [1.0, 2.0, 3.0])


def test_log_bar_rejects_step_kwarg():
    """Step is for accumulating metrics (line, scatter) only;
    bar/pie/histogram don't accept it."""
    import nebo as nb
    nb.get_state().reset()
    with pytest.raises(TypeError):
        nb.log_bar("counts", {"a": 1}, step=2)  # type: ignore[call-arg]


def test_snapshot_metrics_strip_step_and_tags_on_wire(capturing_client):
    """log_bar / log_pie / log_histogram never carry step or tags on
    the wire — those concepts only apply to accumulating metrics."""
    import nebo as nb
    nb.log_bar("b", {"a": 1})
    nb.log_pie("p", {"a": 1})
    nb.log_histogram("h", {"d": [1.0, 2.0]})
    for mname in ("b", "p", "h"):
        ev = capturing_client.metrics_named(mname)[-1]
        assert ev["step"] is None
        assert ev["tags"] == []


def test_repeated_non_line_emission_overwrites_prior_value(capturing_client):
    """Re-emitting a snapshot metric must replace the prior entry, not
    accumulate. The wire payload still goes out twice; the daemon /
    UI store collapses them."""
    import nebo as nb
    nb.log_bar("counts", {"a": 1})
    nb.log_bar("counts", {"a": 2, "b": 3})
    events = capturing_client.metrics_named("counts")
    # Two events on the wire — the SDK doesn't dedupe.
    assert len(events) == 2
    assert events[0]["value"] == {"a": 1}
    assert events[1]["value"] == {"a": 2, "b": 3}


def test_log_scatter_colors_flag_on_wire(capturing_client):
    import nebo as nb
    nb.log_scatter("e", {"c1": [(0.0, 0.0)]})           # default
    nb.log_scatter("e2", {"c1": [(0.0, 0.0)]}, colors=True)
    default_event = capturing_client.metrics_named("e")[-1]
    colored_event = capturing_client.metrics_named("e2")[-1]
    assert default_event.get("colors") is False
    assert colored_event["colors"] is True


def test_log_histogram_colors_flag_on_wire(capturing_client):
    import nebo as nb
    nb.log_histogram("h1", {"a": [1.0, 2.0]})
    nb.log_histogram("h2", {"a": [1.0, 2.0]}, colors=True)
    assert capturing_client.metrics_named("h1")[-1].get("colors") is False
    assert capturing_client.metrics_named("h2")[-1]["colors"] is True


def test_log_bar_does_not_emit_colors_flag(capturing_client):
    """Bar / pie don't take a colors kwarg — the wire payload omits it."""
    import nebo as nb
    nb.log_bar("counts", {"a": 1})
    nb.log_pie("budget", {"a": 1})
    bar = capturing_client.metrics_named("counts")[-1]
    pie = capturing_client.metrics_named("budget")[-1]
    assert "colors" not in bar
    assert "colors" not in pie


def test_log_scatter_emits_labeled_points(capturing_client):
    """{label: list[(x, y)]} → {label: {"x": [...], "y": [...]}} on the wire."""
    import nebo as nb
    nb.log_scatter(
        "embed",
        {
            "cluster_a": [(1, 2), (3, 4)],
            "cluster_b": [(5, 6), (7, 8), (9, 10)],
        },
    )
    events = capturing_client.metrics_named("embed")
    assert events[-1]["metric_type"] == "scatter"
    assert events[-1]["value"] == {
        "cluster_a": {"x": [1, 3], "y": [2, 4]},
        "cluster_b": {"x": [5, 7, 9], "y": [6, 8, 10]},
    }


def test_log_scatter_rejects_legacy_xy_dict():
    """Legacy {"x": [...], "y": [...]} format is no longer accepted."""
    import nebo as nb
    nb.get_state().reset()
    with pytest.raises(TypeError):
        nb.log_scatter("embed", {"x": [1, 2, 3], "y": [4, 5, 6]})


def test_log_scatter_rejects_flat_pair_list():
    import nebo as nb
    nb.get_state().reset()
    with pytest.raises(TypeError):
        nb.log_scatter("embed", [(1, 2), (3, 4)])


def test_log_line_auto_step_uses_cursor(capturing_client):
    """Without an explicit step, log_line takes the cursor's next_step
    counter — even though the SDK no longer keeps the entries list."""
    import nebo as nb
    for _ in range(5):
        nb.log_line("x", 1.0)
    steps = [e["step"] for e in capturing_client.metrics_named("x")]
    assert steps == [0, 1, 2, 3, 4]
    assert nb.get_state()._metric_cursors["__global__"]["x"].next_step == 5


def test_log_scatter_accumulates_with_auto_step(capturing_client):
    """Scatter is accumulating: every call appends another emission to
    the same series, with step auto-incrementing per (loggable, name).
    """
    import nebo as nb
    nb.log_scatter("sex_vs_age", {"dog": [(0, 8)]})
    nb.log_scatter("sex_vs_age", {"cat": [(1, 4)]})
    nb.log_scatter("sex_vs_age", {"dog": [(0, 9)], "cat": [(1, 5)]})
    events = capturing_client.metrics_named("sex_vs_age")
    assert [e["step"] for e in events] == [0, 1, 2]
    assert all(e["metric_type"] == "scatter" for e in events)
    assert nb.get_state()._metric_cursors["__global__"]["sex_vs_age"].next_step == 3


def test_log_scatter_explicit_step_advances_cursor(capturing_client):
    """An explicit step jumps the cursor; the next auto-step picks up
    after the highest step seen."""
    import nebo as nb
    nb.log_scatter("e", {"a": [(0, 0)]})           # auto -> step 0
    nb.log_scatter("e", {"a": [(1, 1)]}, step=10)  # explicit
    nb.log_scatter("e", {"a": [(2, 2)]})           # auto -> step 11
    steps = [e["step"] for e in capturing_client.metrics_named("e")]
    assert steps == [0, 10, 11]


def test_log_scatter_tags_attached_to_emission(capturing_client):
    """Scatter accepts tags now; they ride on each emission like line."""
    import nebo as nb
    nb.log_scatter("e", {"a": [(0, 0)]}, tags=["warmup"])
    nb.log_scatter("e", {"a": [(1, 1)]}, tags=["main"])
    events = capturing_client.metrics_named("e")
    assert events[0]["tags"] == ["warmup"]
    assert events[1]["tags"] == ["main"]


def test_log_line_auto_step_advances_past_explicit_step(capturing_client):
    """An explicit step=N pushes the auto-step counter to N+1 so the
    next implicit emission can't collide with what the user just sent."""
    import nebo as nb
    nb.log_line("x", 1.0, step=10)
    nb.log_line("x", 2.0)
    steps = [e["step"] for e in capturing_client.metrics_named("x")]
    assert steps == [10, 11]


def test_high_volume_emissions_dont_grow_sdk_state(capturing_client):
    """The whole point of the v3 SDK redesign: 1k metric/text/image
    emissions must not grow loggable.metrics/.images/.audio (which
    were dropped) and the recent-texts ring stays bounded."""
    import nebo as nb
    import numpy as np

    @nb.fn()
    def emit():
        for i in range(1000):
            nb.log_line("v", float(i))
            nb.log_text("text", f"step {i}")
            nb.log_image(np.zeros((4, 4, 3), dtype=np.uint8), name="img")

    emit()
    node = _node_by_func_name("emit")
    # No metrics/images/audio mirrors at all.
    assert not hasattr(node, "metrics")
    assert not hasattr(node, "images")
    assert not hasattr(node, "audio")
    # Texts are bounded; the deque's maxlen is the cap regardless of N.
    from nebo.core.state import RECENT_TEXTS_MAXLEN
    assert len(node.texts) == RECENT_TEXTS_MAXLEN
    # The wire received every event.
    assert len(capturing_client.metrics_named("v")) == 1000
    assert len(capturing_client.by_type("image")) == 1000
    # Type-lock cursor still tracks the metric we emitted.
    assert get_state()._metric_cursors[node.loggable_id]["v"].next_step == 1000


class TestLogDeprecatedShim:
    """nb.log() temporarily forwards to nb.log_text() with a one-time warning.

    Nebo ships no other backwards-compat shims; this one exists so
    pre-rename pipelines keep running and is slated for removal.
    """

    @pytest.fixture(autouse=True)
    def _fresh_warning_gate(self, monkeypatch):
        import nebo.logging.logger as logger_mod
        monkeypatch.setattr(logger_mod, "_log_deprecation_warned", False)

    def test_forwards_old_argument_order(self) -> None:
        import nebo as nb
        from nebo.core.state import get_state
        with pytest.warns(FutureWarning, match="nb.log_text"):
            nb.log("plain message")
        entry = get_state().loggables["__global__"].texts[-1]
        assert entry["type"] == "text"
        assert entry["name"] == "text"  # old default stream name
        assert entry["message"] == "plain message"

    def test_forwards_name_and_step(self, capturing_client) -> None:
        import nebo as nb
        with pytest.warns(FutureWarning):
            nb.log("msg", name="status", step=7)
        (event,) = capturing_client.by_type("text")
        assert event["name"] == "status"
        assert event["message"] == "msg"
        assert event["step"] == 7

    def test_warns_exactly_once_per_process(self) -> None:
        import warnings as warnings_mod
        import nebo as nb
        with pytest.warns(FutureWarning):
            nb.log("first")
        with warnings_mod.catch_warnings():
            warnings_mod.simplefilter("error")  # a second warning would raise
            nb.log("second")
