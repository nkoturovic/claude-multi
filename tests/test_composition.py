"""Tests for the kept 2.x composition reader.

``composition.py`` is the v1 reader that ``profile migrate`` uses
(``validate_document``, ``load_composition_file``) plus the
context-policy constants; 2.x presets convert via profile migrate. The
reader stays catalog-independent, which these tests pin.
"""

from __future__ import annotations

import copy
import unittest

from claude_multi import composition, strict_json
from claude_multi.composition import CompositionError
from _catalog import FIXTURE_ROOT
from _layout import REPO_ROOT


SCHEMA = strict_json.load(FIXTURE_ROOT / "schemas" / "composition.schema.json")
V3_DEFAULT = REPO_ROOT / "tests" / "fixtures" / "v3" / "default-composition.json"


class CompositionReaderTests(unittest.TestCase):
    def test_load_composition_file_reads_a_2x_document(self) -> None:
        document = composition.load_composition_file(V3_DEFAULT, SCHEMA)
        self.assertEqual(document, strict_json.load(V3_DEFAULT))
        self.assertEqual(
            sum(1 for slot in document["slots"] if slot["role"] == composition.LEAD_ID), 1
        )

    def test_validate_document_refuses_a_newer_version(self) -> None:
        document = copy.deepcopy(strict_json.load(V3_DEFAULT))
        document["version"] = 2
        with self.assertRaises(CompositionError):
            composition.validate_document(document, SCHEMA)

    def test_validate_document_refuses_a_schema_violation(self) -> None:
        document = copy.deepcopy(strict_json.load(V3_DEFAULT))
        document["unexpected"] = True
        with self.assertRaises(CompositionError):
            composition.validate_document(document, SCHEMA)

    def test_the_reader_needs_no_catalog(self) -> None:
        # Catalog-independent: a model key no catalog knows still reads
        # (profile migrate converts it; resolution against a catalog is gone).
        document = copy.deepcopy(strict_json.load(V3_DEFAULT))
        document["slots"][0]["model"] = "no-such-line"
        self.assertIs(composition.validate_document(document, SCHEMA), document)


if __name__ == "__main__":
    unittest.main()
