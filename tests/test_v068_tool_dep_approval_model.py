import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from systemu.storage.sqlite.models import Base, ToolDepApproval


def test_tool_dep_approval_round_trip(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path/'t.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(ToolDepApproval(
            id="dep_001",
            package_name="requests",
            package_version_spec=">=2.31,<3",
            approved_by="operator",
            source="wizard",
            baked_in_image=True,
        ))
        s.commit()
    with Session(engine) as s:
        row = s.query(ToolDepApproval).one()
        assert row.package_name == "requests"
        assert row.baked_in_image is True
        assert row.source == "wizard"


def test_tool_dry_run_evidence_field_present():
    from systemu.storage.sqlite.models import ToolRow
    assert "dry_run_evidence" in ToolRow.__table__.columns
