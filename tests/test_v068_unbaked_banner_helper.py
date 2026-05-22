"""list_unbaked_approvals helper backs the /tools banner."""

from sqlalchemy import create_engine
from sqlalchemy.orm import Session


def test_unbaked_dep_approvals_counter(tmp_path, monkeypatch):
    """Helper returns runtime-approved (not baked) deps only."""
    from systemu.storage.sqlite.models import Base, ToolDepApproval
    db = tmp_path / "v.db"
    eng = create_engine(f"sqlite:///{db}")
    Base.metadata.create_all(eng)
    with Session(eng) as s:
        s.add(ToolDepApproval(id="d1", package_name="requests", source="wizard",
                              baked_in_image=True))
        s.add(ToolDepApproval(id="d2", package_name="newpkg", source="dashboard",
                              baked_in_image=False))
        s.commit()

    monkeypatch.setenv("SYSTEMU_DATABASE_URL", f"sqlite:///{db}")
    from systemu.runtime.dep_approvals import list_unbaked_approvals
    unbaked = list_unbaked_approvals()
    assert len(unbaked) == 1
    assert unbaked[0].package_name == "newpkg"
