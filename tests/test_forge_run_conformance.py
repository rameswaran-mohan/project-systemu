from systemu.pipelines.tool_forge import check_run_conformance

DECLARED = ["source_path", "password", "output_path"]


def test_kwargs_catchall_passes():
    assert check_run_conformance("def run(**params):\n return {'success': True}\n", DECLARED) is None


def test_keyword_params_matching_schema_pass():
    assert check_run_conformance(
        "def run(source_path, password, output_path=None):\n return {'success': True}\n", DECLARED
    ) is None


def test_required_positional_not_in_schema_rejected():
    err = check_run_conformance(
        "def run(source_path, password, secret_salt):\n return {'success': True}\n", DECLARED
    )
    assert err is not None and "secret_salt" in err


def test_missing_run_rejected():
    assert check_run_conformance("def helper():\n return 1\n", DECLARED) is not None


def test_optional_extra_param_with_default_passes():
    # An extra param not in the schema but with a default is fillable -> OK.
    assert check_run_conformance(
        "def run(source_path, extra=7):\n return {'success': True}\n", DECLARED
    ) is None


def test_uncompilable_code_rejected():
    assert check_run_conformance("def run(:\n pass\n", DECLARED) is not None
