"""Tests for the transport-level metric coalescer (nebo/core/coalesce.py)."""

from __future__ import annotations

from nebo.core.coalesce import MAX_BATCH_POINTS, coalesce, expand_metric_batch


def _pt(name, value, step, *, mtype="line", tags=None, colors=None, lid="a"):
    e = {
        "type": "metric",
        "loggable_id": lid,
        "name": name,
        "metric_type": mtype,
        "value": value,
        "step": step,
        "tags": list(tags or []),
        "timestamp": 100.0 + step,
    }
    if colors is not None:
        e["colors"] = colors
    return e


class TestCoalesce:
    def test_interleaved_series_batch_separately(self):
        events = [
            _pt("loss", 0.5, 0), _pt("acc", 0.1, 0),
            _pt("loss", 0.4, 1), _pt("acc", 0.2, 1),
            _pt("loss", 0.3, 2),
        ]
        out = coalesce(events)
        assert [e["type"] for e in out] == ["metric_batch", "metric_batch"]
        loss, acc = out
        assert loss["name"] == "loss"
        assert loss["steps"] == [0, 1, 2]
        assert loss["values"] == [0.5, 0.4, 0.3]
        assert loss["timestamps"] == [100.0, 101.0, 102.0]
        assert acc["steps"] == [0, 1]
        assert acc["values"] == [0.1, 0.2]

    def test_singleton_passes_through_as_metric(self):
        events = [_pt("loss", 0.5, 0), _pt("acc", 0.1, 0), _pt("loss", 0.4, 1)]
        out = coalesce(events)
        assert [e["type"] for e in out] == ["metric_batch", "metric"]
        assert out[1]["name"] == "acc"
        assert out[1] == events[1]

    def test_non_metric_events_untouched_in_order(self):
        text1 = {"type": "text", "message": "a"}
        text2 = {"type": "text", "message": "b"}
        events = [text1, _pt("loss", 0.5, 0), text2, _pt("loss", 0.4, 1)]
        out = coalesce(events)
        # Batch lands at its first member's position; texts keep their order.
        assert [e["type"] for e in out] == ["text", "metric_batch", "text"]
        assert out[0] is text1
        assert out[2] is text2

    def test_tags_change_cuts_batch(self):
        events = [
            _pt("loss", 0.5, 0, tags=["train"]),
            _pt("loss", 0.4, 1, tags=["train"]),
            _pt("loss", 0.9, 2, tags=["val"]),
            _pt("loss", 0.8, 3, tags=["val"]),
        ]
        out = coalesce(events)
        assert [e["type"] for e in out] == ["metric_batch", "metric_batch"]
        assert out[0]["tags"] == ["train"]
        assert out[0]["steps"] == [0, 1]
        assert out[1]["tags"] == ["val"]
        assert out[1]["steps"] == [2, 3]

    def test_colors_change_cuts_batch(self):
        events = [
            _pt("pts", {"a": {"x": [1], "y": [1]}}, 0, mtype="scatter", colors=False),
            _pt("pts", {"a": {"x": [2], "y": [2]}}, 1, mtype="scatter", colors=False),
            _pt("pts", {"a": {"x": [3], "y": [3]}}, 2, mtype="scatter", colors=True),
            _pt("pts", {"a": {"x": [4], "y": [4]}}, 3, mtype="scatter", colors=True),
        ]
        out = coalesce(events)
        assert len(out) == 2
        assert out[0]["colors"] is False
        assert out[1]["colors"] is True

    def test_snapshot_types_never_batch(self):
        """Snapshots never become metric_batch frames; re-emissions within
        a window coalesce last-wins (they overwrite daemon-side anyway)."""
        events = [
            _pt("dist", {"x": 1}, 0, mtype="bar"),
            _pt("dist", {"x": 2}, 1, mtype="bar"),
            _pt("h", {"a": [1, 2]}, 0, mtype="histogram"),
            _pt("h", {"a": [3]}, 1, mtype="histogram"),
            _pt("p", {"x": 1}, 0, mtype="pie"),
        ]
        out = coalesce(events)
        assert all(e["type"] == "metric" for e in out)
        assert [e["name"] for e in out] == ["dist", "h", "p"]
        assert out[0]["value"] == {"x": 2}
        assert out[1]["value"] == {"a": [3]}

    def test_max_batch_points_split(self):
        events = [_pt("loss", float(i), i) for i in range(MAX_BATCH_POINTS + 3)]
        out = coalesce(events)
        assert [e["type"] for e in out] == ["metric_batch", "metric_batch"]
        assert len(out[0]["steps"]) == MAX_BATCH_POINTS
        assert len(out[1]["steps"]) == 3
        assert out[1]["steps"][0] == MAX_BATCH_POINTS

    def test_expand_roundtrip(self):
        events = [
            _pt("loss", 0.5, 0, tags=["t"]),
            _pt("loss", 0.4, 1, tags=["t"]),
            _pt("pts", {"a": {"x": [1], "y": [2]}}, 0, mtype="scatter", colors=True),
            _pt("pts", {"a": {"x": [3], "y": [4]}}, 1, mtype="scatter", colors=True),
        ]
        out = coalesce(events)
        expanded = []
        for e in out:
            expanded.extend(expand_metric_batch(e) if e["type"] == "metric_batch" else [e])
        assert expanded == events

    def test_different_loggables_batch_separately(self):
        events = [
            _pt("loss", 0.5, 0, lid="a"), _pt("loss", 0.1, 0, lid="b"),
            _pt("loss", 0.4, 1, lid="a"), _pt("loss", 0.2, 1, lid="b"),
        ]
        out = coalesce(events)
        assert len(out) == 2
        assert {e["loggable_id"] for e in out} == {"a", "b"}

    def test_empty_and_no_metrics(self):
        assert coalesce([]) == []
        texts = [{"type": "text", "message": "x"}]
        assert coalesce(texts) == texts


class TestSnapshotCoalescing:
    def test_progress_last_wins_per_loggable(self):
        events = [
            {"type": "progress", "loggable_id": "a", "data": {"current": 1}},
            {"type": "text", "message": "between"},
            {"type": "progress", "loggable_id": "a", "data": {"current": 2}},
            {"type": "progress", "loggable_id": "b", "data": {"current": 9}},
            {"type": "progress", "loggable_id": "a", "data": {"current": 3}},
        ]
        out = coalesce(events)
        progress = [e for e in out if e["type"] == "progress"]
        assert len(progress) == 2
        by_lid = {e["loggable_id"]: e["data"]["current"] for e in progress}
        assert by_lid == {"a": 3, "b": 9}
        # Survivor sits at the first occurrence's position (before the text).
        assert out[0]["type"] == "progress" and out[0]["data"]["current"] == 3
        assert out[1]["type"] == "text"

    def test_snapshot_metrics_last_wins_per_series(self):
        events = [
            _pt("dist", {"x": 1}, 0, mtype="bar"),
            _pt("dist", {"x": 2}, 1, mtype="bar"),
            _pt("other", {"y": 5}, 0, mtype="pie"),
            _pt("dist", {"x": 3}, 2, mtype="bar"),
        ]
        out = coalesce(events)
        assert len(out) == 2
        dist = next(e for e in out if e["name"] == "dist")
        assert dist["value"] == {"x": 3}
        assert next(e for e in out if e["name"] == "other")["value"] == {"y": 5}

    def test_accumulating_metrics_unaffected_by_snapshot_rule(self):
        events = [
            _pt("loss", 0.5, 0), _pt("loss", 0.4, 1),
            _pt("dist", {"x": 1}, 0, mtype="bar"),
            _pt("dist", {"x": 2}, 1, mtype="bar"),
        ]
        out = coalesce(events)
        assert [e["type"] for e in out] == ["metric_batch", "metric"]
        assert out[0]["steps"] == [0, 1]
        assert out[1]["value"] == {"x": 2}


class TestNodeExecutedBatching:
    @staticmethod
    def _exec(lid, caller=None):
        data = {"loggable_id": lid}
        if caller is not None:
            data["caller"] = caller
        return {"type": "node_executed", "loggable_id": lid, "data": data}

    def test_folds_same_node_into_count(self):
        events = [self._exec("a") for _ in range(100)]
        out = coalesce(events)
        assert len(out) == 1
        assert out[0]["data"]["count"] == 100
        assert out[0]["data"]["loggable_id"] == "a"

    def test_distinct_callers_stay_separate(self):
        events = [
            self._exec("a", caller="p"), self._exec("a", caller="q"),
            self._exec("a", caller="p"),
        ]
        out = coalesce(events)
        assert len(out) == 2
        by_caller = {
            e["data"].get("caller"): e["data"].get("count", 1) for e in out
        }
        assert by_caller == {"p": 2, "q": 1}

    def test_singleton_has_no_count_key(self):
        out = coalesce([self._exec("a")])
        assert len(out) == 1
        assert "count" not in out[0]["data"]

    def test_register_order_preserved(self):
        reg = {"type": "loggable_register", "loggable_id": "a",
               "data": {"loggable_id": "a", "kind": "node"}}
        out = coalesce([reg, self._exec("a"), self._exec("a")])
        assert [e["type"] for e in out] == ["loggable_register", "node_executed"]
        assert out[1]["data"]["count"] == 2
