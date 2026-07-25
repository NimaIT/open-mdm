"""Model import/export — the Git-friendly portable representation."""
import json

import pytest
import yaml

from app.models import Entity
from app.services.model_io import (
    SCHEMA_VERSION,
    apply_import,
    diff_against_existing,
    entity_to_dict,
    export_models,
    parse_document,
    validate_document,
)
from tests.conftest import requires_db

SAMPLE_YAML = """
mdm_schema_version: "1.0"
entities:
  - name: io_product
    display_name: Product
    domain: catalog
    attributes:
      - name: sku
        data_type: string
        length: 40
        is_required: true
        is_unique: true
        is_business_key: true
        normalization: [trim, upper]
      - name: price
        data_type: decimal
        numeric_precision: 12
        numeric_scale: 2
        validation: {min: 0}
      - name: kind
        data_type: enum
        length: 30
        validation: {enum: [hardware, software]}
"""


class TestParse:
    def test_sniffs_yaml(self):
        doc = parse_document(SAMPLE_YAML)
        assert doc["entities"][0]["name"] == "io_product"

    def test_sniffs_json(self):
        doc = parse_document('{"entities":[{"name":"x"}]}')
        assert doc["entities"][0]["name"] == "x"

    def test_rejects_empty(self):
        with pytest.raises(ValueError, match="empty"):
            parse_document("   ")

    def test_rejects_malformed(self):
        with pytest.raises(ValueError):
            parse_document("{not valid json")

    def test_rejects_non_mapping(self):
        with pytest.raises(ValueError, match="mapping"):
            parse_document("[1, 2, 3]")


class TestValidateDocument:
    def test_accepts_valid(self):
        defs, errors = validate_document(parse_document(SAMPLE_YAML))
        assert errors == []
        assert len(defs) == 1 and len(defs[0]["attributes"]) == 3

    def test_defaults_display_names(self):
        defs, _ = validate_document(
            {"entities": [{"name": "thing", "attributes": [
                {"name": "some_col", "data_type": "string",
                 "is_business_key": True}]}]}
        )
        assert defs[0]["attributes"][0]["display_name"] == "Some Col"

    def test_positions_are_assigned_in_order(self):
        defs, _ = validate_document(parse_document(SAMPLE_YAML))
        assert [a["position"] for a in defs[0]["attributes"]] == [0, 1, 2]

    def test_accepts_single_bare_entity(self):
        defs, errors = validate_document(
            {"name": "solo", "attributes": [
                {"name": "c", "data_type": "string", "is_business_key": True}]}
        )
        assert [d["name"] for d in defs] == ["solo"]

    @pytest.mark.parametrize(
        "entity_name",
        ["Bad Name", "1abc", "select", "x; DROP TABLE y --", ""],
    )
    def test_rejects_unsafe_entity_names(self, entity_name):
        defs, errors = validate_document(
            {"entities": [{"name": entity_name, "attributes": [
                {"name": "c", "data_type": "string"}]}]}
        )
        assert defs == [] and errors

    def test_rejects_reserved_and_bad_columns(self):
        defs, errors = validate_document(
            {"entities": [{"name": "ok_ent", "attributes": [
                {"name": "mdm_id", "data_type": "string"},
                {"name": 'inj"ect', "data_type": "string"},
                {"name": "bad_type_col", "data_type": "nonsense"},
                {"name": "good", "data_type": "string", "is_business_key": True},
            ]}]}
        )
        # Only the safe attribute survives; each problem is reported.
        assert [a["name"] for a in defs[0]["attributes"]] == ["good"]
        assert len(errors) >= 3

    def test_rejects_duplicate_entity(self):
        defs, errors = validate_document(
            {"entities": [
                {"name": "dup", "attributes": [
                    {"name": "a", "data_type": "string"}]},
                {"name": "dup", "attributes": [
                    {"name": "b", "data_type": "string"}]},
            ]}
        )
        assert any("duplicated" in e for e in errors)

    def test_rejects_duplicate_attribute(self):
        defs, errors = validate_document(
            {"entities": [{"name": "e1", "attributes": [
                {"name": "a", "data_type": "string"},
                {"name": "a", "data_type": "integer"},
            ]}]}
        )
        assert any("duplicated" in e for e in errors)

    def test_requires_attributes(self):
        defs, errors = validate_document(
            {"entities": [{"name": "e2", "attributes": []}]}
        )
        assert defs == [] and errors

    def test_warns_when_no_business_key(self):
        defs, errors = validate_document(
            {"entities": [{"name": "e3", "attributes": [
                {"name": "c", "data_type": "string"}]}]}
        )
        assert defs and any("business key" in e for e in errors)

    def test_rejects_incompatible_schema_version(self):
        _defs, errors = validate_document(
            {"mdm_schema_version": "9.0", "entities": [
                {"name": "e4", "attributes": [
                    {"name": "c", "data_type": "string"}]}]}
        )
        assert any("incompatible" in e for e in errors)

    def test_no_entities(self):
        defs, errors = validate_document({"entities": []})
        assert defs == [] and errors

    def test_coerces_string_normalization_to_list(self):
        defs, _ = validate_document(
            {"entities": [{"name": "e5", "attributes": [
                {"name": "c", "data_type": "string", "normalization": "trim",
                 "is_business_key": True}]}]}
        )
        assert defs[0]["attributes"][0]["normalization"] == ["trim"]


class TestExport:
    def test_json_and_yaml_agree(self):
        defs, _ = validate_document(parse_document(SAMPLE_YAML))

        class _E:
            name = defs[0]["name"]
            display_name = defs[0]["display_name"]
            description = None
            domain = defs[0]["domain"]
            requires_approval = True
            soft_delete = True
            auto_approve_threshold = None
            retention_days = None
            attributes = [type("A", (), a)() for a in defs[0]["attributes"]]

        js = export_models([_E()], fmt="json")
        ym = export_models([_E()], fmt="yaml")
        # Compare the entity payloads; exported_at differs by microseconds.
        assert json.loads(js)["entities"] == yaml.safe_load(ym)["entities"]

    def test_rejects_unknown_format(self):
        with pytest.raises(ValueError):
            export_models([], fmt="xml")

    def test_includes_schema_version(self):
        doc = json.loads(export_models([], fmt="json"))
        assert doc["mdm_schema_version"] == SCHEMA_VERSION


@requires_db
class TestImportAgainstDatabase:
    def test_creates_then_reports_no_change(self, db):
        defs, _ = validate_document(parse_document(SAMPLE_YAML))
        try:
            report = diff_against_existing(db, defs)
            assert report[0]["action"] == "create"

            results = apply_import(
                db, [dict(d, attributes=list(d["attributes"])) for d in defs],
                actor="pytest",
            )
            assert results[0]["action"] == "created"
            db.commit()

            defs2, _ = validate_document(parse_document(SAMPLE_YAML))
            assert diff_against_existing(db, defs2)[0]["action"] == "no_change"
        finally:
            ent = db.query(Entity).filter(Entity.name == "io_product").one_or_none()
            if ent:
                db.delete(ent)
                db.commit()

    def test_import_leaves_entity_as_draft(self, db):
        """Importing must never silently mutate the physical database."""
        defs, _ = validate_document(parse_document(SAMPLE_YAML))
        try:
            apply_import(db, [dict(d, attributes=list(d["attributes"]))
                              for d in defs], actor="pytest")
            db.commit()
            ent = db.query(Entity).filter(Entity.name == "io_product").one()
            assert ent.status == "draft"
        finally:
            ent = db.query(Entity).filter(Entity.name == "io_product").one_or_none()
            if ent:
                db.delete(ent)
                db.commit()

    def test_round_trip_preserves_definition(self, db):
        defs, _ = validate_document(parse_document(SAMPLE_YAML))
        try:
            apply_import(db, [dict(d, attributes=list(d["attributes"]))
                              for d in defs], actor="pytest")
            db.commit()
            ent = db.query(Entity).filter(Entity.name == "io_product").one()

            exported = export_models([ent], fmt="yaml")
            redefs, errors = validate_document(parse_document(exported))
            assert errors == []
            original = {a["name"]: a for a in defs[0]["attributes"]}
            reimported = {a["name"]: a for a in redefs[0]["attributes"]}
            assert set(original) == set(reimported)
            for name, attr in original.items():
                for field in ("data_type", "length", "is_required", "is_unique",
                              "is_business_key", "validation", "normalization"):
                    assert attr[field] == reimported[name][field], (name, field)
        finally:
            ent = db.query(Entity).filter(Entity.name == "io_product").one_or_none()
            if ent:
                db.delete(ent)
                db.commit()

    def test_diff_flags_destructive_removal(self, db, entity_factory):
        ent = entity_factory()
        shrunk = [{
            "name": ent.name,
            "display_name": ent.display_name,
            "description": None, "domain": None,
            "requires_approval": True, "soft_delete": True,
            "auto_approve_threshold": None, "retention_days": None,
            "attributes": [{
                "name": "code", "data_type": "string", "length": 40,
                "numeric_precision": None, "numeric_scale": None,
                "is_required": True, "is_unique": True, "is_business_key": True,
                "is_match_key": False, "is_indexed": False, "is_pii": False,
                "default_value": None, "validation": {},
                "normalization": ["trim", "upper"], "ref_entity": None,
                "ref_attribute": None, "position": 0,
            }],
        }]
        report = diff_against_existing(db, shrunk)[0]
        assert report["action"] == "update"
        assert set(report["attributes_removed"]) >= {"label", "amount"}
        assert report["warning"] and "destructive" in report["warning"]
