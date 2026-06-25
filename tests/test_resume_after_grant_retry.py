import inspect
from systemu.runtime import supervisor


def test_resume_after_grant_does_not_preconsume_a_retry():
    src = inspect.getsource(supervisor.Supervisor.resume_after_grant)
    # The grant-resume re-queue must NOT hardcode retry_count=1 (that burns a
    # MAX_RETRIES slot for a SUCCESSFUL grant). retry_count=0 = forward progress.
    assert "retry_count=1" not in src.replace(" ", "")
    assert "retry_count=0" in src.replace(" ", "")
