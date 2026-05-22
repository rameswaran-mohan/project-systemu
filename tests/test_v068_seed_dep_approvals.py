from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from systemu.storage.sqlite.models import Base, ToolDepApproval


def test_seed_from_requirements_file(tmp_path):
    from systemu.storage.sqlite.vault import seed_tool_dep_approvals

    db = tmp_path / "v.db"
    engine = create_engine(f"sqlite:///{db}")
    Base.metadata.create_all(engine)

    reqs = tmp_path / "requirements-tools.txt"
    reqs.write_text("requests\npython-docx>=0.8\n# comment\n\n", encoding="utf-8")

    n = seed_tool_dep_approvals(database_url=f"sqlite:///{db}", requirements_path=reqs)
    assert n == 2

    with Session(engine) as s:
        rows = s.query(ToolDepApproval).all()
        names = {r.package_name for r in rows}
    assert names == {"requests", "python-docx"}
    # python-docx should carry the version spec
    versioned = {r.package_name: r.package_version_spec for r in rows}
    assert versioned["python-docx"] == ">=0.8"
    assert versioned["requests"] is None


def test_seed_is_idempotent(tmp_path):
    from systemu.storage.sqlite.vault import seed_tool_dep_approvals

    db = tmp_path / "v.db"
    engine = create_engine(f"sqlite:///{db}")
    Base.metadata.create_all(engine)
    reqs = tmp_path / "requirements-tools.txt"
    reqs.write_text("requests\npython-docx\n", encoding="utf-8")

    seed_tool_dep_approvals(database_url=f"sqlite:///{db}", requirements_path=reqs)
    seed_tool_dep_approvals(database_url=f"sqlite:///{db}", requirements_path=reqs)

    with Session(engine) as s:
        assert s.query(ToolDepApproval).count() == 2


def test_seed_missing_file_returns_zero(tmp_path):
    from systemu.storage.sqlite.vault import seed_tool_dep_approvals
    db = tmp_path / "v.db"
    engine = create_engine(f"sqlite:///{db}")
    Base.metadata.create_all(engine)
    assert seed_tool_dep_approvals(
        database_url=f"sqlite:///{db}",
        requirements_path=tmp_path / "nope.txt",
    ) == 0
