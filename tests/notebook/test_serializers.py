"""Round-trip tests for all serializers."""

from pathlib import Path

import pandas as pd
import pytest

from kbutillib.notebook.serialization import (
    auto_dispatch,
    get_serializer,
    list_serializers,
)


class TestJsonSerializer:
    def test_round_trip_list(self, tmp_path: Path):
        ser = get_serializer("json")
        obj = [1, 2, 3, "hello"]
        path = tmp_path / "test.json"
        meta = ser.serialize(obj, path)
        assert meta == {}
        result = ser.deserialize(path, meta)
        assert result == obj

    def test_round_trip_int(self, tmp_path: Path):
        ser = get_serializer("json")
        path = tmp_path / "test.json"
        ser.serialize(42, path)
        assert ser.deserialize(path, {}) == 42

    def test_round_trip_bool(self, tmp_path: Path):
        ser = get_serializer("json")
        path = tmp_path / "test.json"
        ser.serialize(True, path)
        assert ser.deserialize(path, {}) is True

    def test_round_trip_none(self, tmp_path: Path):
        ser = get_serializer("json")
        path = tmp_path / "test.json"
        ser.serialize(None, path)
        assert ser.deserialize(path, {}) is None

    def test_auto_dispatch_list(self):
        ser = auto_dispatch([1, 2, 3])
        assert ser.type_name == "json"


class TestDictSerializer:
    def test_round_trip(self, tmp_path: Path):
        ser = get_serializer("dict")
        obj = {"key": "value", "nested": {"a": 1}}
        path = tmp_path / "test.json"
        meta = ser.serialize(obj, path)
        assert meta == {}
        result = ser.deserialize(path, meta)
        assert result == obj

    def test_auto_dispatch(self):
        ser = auto_dispatch({"a": 1})
        assert ser.type_name == "dict"


class TestDataFrameSerializer:
    def test_round_trip(self, tmp_path: Path):
        ser = get_serializer("dataframe")
        df = pd.DataFrame({"a": [1, 2, 3], "b": [4.0, 5.0, 6.0]})
        path = tmp_path / "test.parquet"
        meta = ser.serialize(df, path)
        assert meta["shape"] == [3, 2]
        assert meta["columns"] == ["a", "b"]
        result = ser.deserialize(path, meta)
        pd.testing.assert_frame_equal(result, df)

    def test_auto_dispatch(self):
        df = pd.DataFrame({"x": [1]})
        ser = auto_dispatch(df)
        assert ser.type_name == "dataframe"


class TestTextSerializer:
    def test_round_trip(self, tmp_path: Path):
        ser = get_serializer("text")
        obj = "hello world\nline two"
        path = tmp_path / "test.txt"
        meta = ser.serialize(obj, path)
        assert "length" in meta
        result = ser.deserialize(path, meta)
        assert result == obj

    def test_no_auto_dispatch(self):
        # Text serializer does not auto-dispatch (str goes to json)
        ser = auto_dispatch("hello")
        assert ser.type_name == "json"


class TestMSExpressionSerializer:
    """Test with a mock MSExpression-like object (avoid modelseedpy dependency)."""

    def test_round_trip(self, tmp_path: Path):
        ser = get_serializer("msexpression")
        # Create a mock object that looks like MSExpression
        df = pd.DataFrame({"gene1": [1.5, 2.3], "gene2": [0.8, 1.1]})

        class MockExpr:
            pass

        obj = MockExpr()
        obj.__class__.__name__ = "MSExpression"  # type: ignore[attr-defined]
        # Set type name to match
        type.__setattr__(type(obj), "__name__", "MSExpression")
        obj._data = df
        obj.genome_ref = "genome123"
        obj.expression_set_ref = "expr_set_456"
        obj.condition = "glucose"

        path = tmp_path / "test.parquet"
        meta = ser.serialize(obj, path)
        # Metadata should contain the attributes (no sidecar file)
        assert meta["genome_ref"] == "genome123"
        assert meta["expression_set_ref"] == "expr_set_456"
        assert meta["condition"] == "glucose"
        # No .meta.json sidecar should exist
        assert not (tmp_path / "test.meta.json").exists()

    def test_can_handle(self):
        ser = get_serializer("msexpression")

        class FakeExpr:
            _data = None

        # Rename class
        FakeExpr.__name__ = "MSExpression"
        obj = FakeExpr()
        assert ser.can_handle(obj) is True
        assert ser.can_handle({"not": "an expression"}) is False


class TestSerializerRegistry:
    def test_list_serializers(self):
        names = list_serializers()
        assert "json" in names
        assert "dict" in names
        assert "dataframe" in names
        assert "text" in names
        assert "msexpression" in names

    def test_get_unknown_raises(self):
        with pytest.raises(ValueError, match="No serializer"):
            get_serializer("nonexistent_type")

    def test_auto_dispatch_unknown_raises(self):
        class Unknown:
            pass

        with pytest.raises(TypeError, match="No serializer can handle"):
            auto_dispatch(Unknown())
