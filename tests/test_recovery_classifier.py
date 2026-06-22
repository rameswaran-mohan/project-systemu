from systemu.recovery.classifier import classify_dry_run_error


def test_classifies_import_error_as_dep_pending():
    out = classify_dry_run_error("ImportError: No module named 'requests'")
    assert out.kind == "DEP_PENDING"
    assert out.missing_package == "requests"


def test_classifies_modulenotfounderror_as_dep_pending():
    out = classify_dry_run_error("ModuleNotFoundError: No module named 'python-docx'")
    assert out.kind == "DEP_PENDING"
    assert out.missing_package == "python-docx"


def test_classifies_permission_error_as_fs_permission():
    out = classify_dry_run_error("PermissionError: [Errno 13] Permission denied: '/foo'")
    assert out.kind == "FS_PERMISSION"


def test_unknown_error_is_dry_run_failed_bug():
    out = classify_dry_run_error("ValueError: bad shape (3, 4)")
    assert out.kind == "DRY_RUN_FAILED_BUG"
    assert out.missing_package is None


def test_empty_error_is_bug():
    out = classify_dry_run_error("")
    assert out.kind == "DRY_RUN_FAILED_BUG"


def test_raw_import_name_preserved():
    out = classify_dry_run_error("ModuleNotFoundError: No module named 'docx'")
    assert out.kind == "DEP_PENDING"
    assert out.missing_package == "docx"
