"""Tests for strict JSON loading, canonical bytes, and schema vocabulary."""

from __future__ import annotations

import hashlib
import json
import unittest

from claude_multi import strict_json, validate
from claude_multi.strict_json import JSONLimits, StrictJSONError


class StrictLoaderTests(unittest.TestCase):
    def test_valid_document_round_trip(self) -> None:
        self.assertEqual(
            strict_json.loads(b'{"a": [1, 2], "b": {"c": true}}'),
            {"a": [1, 2], "b": {"c": True}},
        )

    def test_duplicate_keys_rejected(self) -> None:
        with self.assertRaisesRegex(StrictJSONError, "duplicate key"):
            strict_json.loads(b'{"a": 1, "a": 2}')

    def test_nested_duplicate_keys_rejected(self) -> None:
        with self.assertRaisesRegex(StrictJSONError, "duplicate key"):
            strict_json.loads(b'{"a": {"b": 1, "b": 2}}')

    def test_trailing_data_rejected(self) -> None:
        with self.assertRaisesRegex(StrictJSONError, "trailing data"):
            strict_json.loads(b'{"a": 1} {"b": 2}')

    def test_trailing_whitespace_allowed(self) -> None:
        self.assertEqual(strict_json.loads(b'{"a": 1}\n'), {"a": 1})

    def test_non_finite_numbers_rejected(self) -> None:
        for literal in (b"NaN", b"Infinity", b"-Infinity"):
            with self.assertRaisesRegex(StrictJSONError, "non-finite"):
                strict_json.loads(b'{"x": ' + literal + b"}")

    def test_float_overflow_rejected(self) -> None:
        for literal in (b"1e999", b"-1e999"):
            with self.assertRaisesRegex(StrictJSONError, "non-finite"):
                strict_json.loads(b'{"x": ' + literal + b"}")
            with self.assertRaisesRegex(StrictJSONError, "non-finite"):
                strict_json.loads(b"[" + literal + b"]")

    def test_finite_decimals_preserved(self) -> None:
        self.assertEqual(
            strict_json.loads(b'{"x": 1.5, "y": -2.75e10, "z": 0.1}'),
            {"x": 1.5, "y": -2.75e10, "z": 0.1},
        )
        self.assertEqual(strict_json.loads(b"1e308"), 1e308)
        self.assertEqual(strict_json.loads(b"-1e308"), -1e308)

    def test_invalid_utf8_rejected(self) -> None:
        with self.assertRaisesRegex(StrictJSONError, "invalid UTF-8"):
            strict_json.loads(b'{"x": "\xff"}')

    def test_depth_limit(self) -> None:
        limits = JSONLimits(max_depth=8)
        with self.assertRaisesRegex(StrictJSONError, "depth"):
            strict_json.loads(b'{"a":' * 10 + b"1" + b"}" * 10, limits)

    def test_collection_limit(self) -> None:
        limits = JSONLimits(max_collection=4)
        with self.assertRaisesRegex(StrictJSONError, "limit is 4"):
            strict_json.loads(b"[1, 2, 3, 4, 5]", limits)

    def test_string_limit(self) -> None:
        limits = JSONLimits(max_string=8)
        with self.assertRaisesRegex(StrictJSONError, "string length"):
            strict_json.loads(b'"' + b"x" * 16 + b'"', limits)

    def test_bytes_limit(self) -> None:
        limits = JSONLimits(max_bytes=8)
        with self.assertRaisesRegex(StrictJSONError, "bytes"):
            strict_json.loads(b'{"a": 1, "b": 2}', limits)

    def test_error_determinism(self) -> None:
        payload = b'{"a": 1, "a": 2}'
        first = second = None
        try:
            strict_json.loads(payload)
        except StrictJSONError as exc:
            first = str(exc)
        try:
            strict_json.loads(payload)
        except StrictJSONError as exc:
            second = str(exc)
        self.assertIsNotNone(first)
        self.assertEqual(first, second)


class CanonicalFormTests(unittest.TestCase):
    def test_canonical_bytes_sorted_compact(self) -> None:
        document = {"b": 1, "a": [2, {"d": True, "c": None}]}
        self.assertEqual(
            strict_json.canonical_bytes(document),
            b'{"a":[2,{"c":null,"d":true}],"b":1}',
        )

    def test_canonical_file_bytes_newline_terminated(self) -> None:
        self.assertEqual(
            strict_json.canonical_file_bytes({"a": 1}), b'{"a":1}\n'
        )

    def test_bundle_digest_matches_manual_hash(self) -> None:
        document = {"x": [1, 2, 3]}
        expected = "sha256:" + hashlib.sha256(
            strict_json.canonical_bytes(document)
        ).hexdigest()
        self.assertEqual(strict_json.bundle_digest(document), expected)

    def test_digest_is_order_insensitive_and_content_sensitive(self) -> None:
        self.assertEqual(
            strict_json.bundle_digest({"a": 1, "b": 2}),
            strict_json.bundle_digest({"b": 2, "a": 1}),
        )
        self.assertNotEqual(
            strict_json.bundle_digest({"a": 1}),
            strict_json.bundle_digest({"a": 2}),
        )


class SchemaVocabularyTests(unittest.TestCase):
    def test_unknown_keyword_rejected(self) -> None:
        with self.assertRaisesRegex(validate.SchemaError, "unsupported schema keyword"):
            validate.check_schema({"version": 1, "patternProperties": {"a": {}}})

    def test_unknown_keyword_nested_rejected(self) -> None:
        with self.assertRaisesRegex(validate.SchemaError, "unsupported schema keyword"):
            validate.check_schema(
                {"version": 1, "properties": {"a": {"format": "uri"}}}
            )

    def test_unknown_type_rejected(self) -> None:
        with self.assertRaisesRegex(validate.SchemaError, "unknown type"):
            validate.check_schema({"version": 1, "type": "frobnicator"})

    def test_version_must_be_supported(self) -> None:
        with self.assertRaisesRegex(validate.SchemaError, "unsupported schema version"):
            validate.check_schema({"version": 99, "type": "object"})

    def test_invalid_pattern_rejected(self) -> None:
        with self.assertRaisesRegex(validate.SchemaError, "invalid regular expression"):
            validate.check_schema({"version": 1, "pattern": "([unclosed"})

    def test_inverted_bounds_rejected(self) -> None:
        with self.assertRaisesRegex(validate.SchemaError, "exceeds"):
            validate.check_schema({"version": 1, "minItems": 4, "maxItems": 2})

    def test_one_of_must_be_non_empty(self) -> None:
        with self.assertRaisesRegex(validate.SchemaError, "oneOf"):
            validate.check_schema({"version": 1, "oneOf": []})

    def test_instance_validation_basics(self) -> None:
        schema = {
            "version": 1,
            "type": "object",
            "required": ["a"],
            "properties": {"a": {"type": "integer", "minimum": 2}},
            "additionalProperties": False,
        }
        self.assertEqual(validate.validate({"a": 3}, schema), [])
        problems = validate.validate({"a": 1, "b": 2}, schema)
        self.assertTrue(any("below minimum" in problem for problem in problems))
        self.assertTrue(any("unexpected key 'b'" in problem for problem in problems))
        self.assertTrue(
            any("missing required key" in problem for problem in validate.validate({}, schema))
        )

    def test_const_and_enum_use_exact_equality(self) -> None:
        schema = {"version": 1, "const": 1}
        self.assertEqual(validate.validate(1, schema), [])
        self.assertTrue(validate.validate(1.0, schema))
        enum_schema = {"version": 1, "enum": ["a", 2]}
        self.assertEqual(validate.validate("a", enum_schema), [])
        self.assertTrue(validate.validate("b", enum_schema))

    def test_one_of_exactly_one(self) -> None:
        schema = {
            "version": 1,
            "oneOf": [{"type": "string"}, {"type": "integer"}],
        }
        self.assertEqual(validate.validate("x", schema), [])
        self.assertEqual(validate.validate(2, schema), [])
        self.assertTrue(validate.validate(True, schema))

    def test_unique_items(self) -> None:
        schema = {"version": 1, "type": "array", "uniqueItems": True}
        self.assertEqual(validate.validate([1, 2, 3], schema), [])
        self.assertTrue(validate.validate([1, 2, 1], schema))

    def test_type_list(self) -> None:
        schema = {"version": 1, "type": ["string", "null"]}
        self.assertEqual(validate.validate("x", schema), [])
        self.assertEqual(validate.validate(None, schema), [])
        self.assertTrue(validate.validate(3, schema))


if __name__ == "__main__":
    unittest.main()
