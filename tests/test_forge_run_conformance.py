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


def test_conformance_check_never_executes_forged_module_level_code():
    """SECURITY (Lane-1 audit): the conformance gate must introspect run()'s
    signature STATICALLY (AST) and NEVER exec the forged module — else top-level
    egress in freshly-generated untrusted code would run at forge time, bypassing
    the R-A14a forged-network hard-DENY."""
    import sys
    sentinel = "_forge_conformance_side_effect_ran"
    sys.modules.pop(sentinel, None)
    # A module whose TOP LEVEL would set a global marker + raise if executed.
    impl = (
        "import builtins\n"
        "builtins." + sentinel + " = True\n"
        "raise RuntimeError('module-level code executed — the exec hole is back')\n"
        "def run(source_path):\n"
        "    return {'success': True}\n"
    )
    import builtins
    if hasattr(builtins, sentinel):
        delattr(builtins, sentinel)
    # AST path: returns cleanly on the signature (source_path is declared),
    # and the top-level statements NEVER run (no marker, no RuntimeError).
    assert check_run_conformance(impl, ["source_path"]) is None
    assert not hasattr(builtins, sentinel), "forged module-level code was EXECUTED"


def test_kwonly_required_param_not_in_schema_rejected():
    """A keyword-only required param outside the schema is unfillable (AST parity
    with inspect.signature's KEYWORD_ONLY handling)."""
    err = check_run_conformance(
        "def run(source_path, *, secret_key):\n return {'success': True}\n",
        ["source_path"])
    assert err is not None and "secret_key" in err


def test_async_run_signature_is_checked():
    """`async def run(...)` is introspected the same as a sync def."""
    assert check_run_conformance(
        "async def run(source_path):\n return {'success': True}\n", ["source_path"]) is None
    err = check_run_conformance(
        "async def run(source_path, missing_p):\n return {}\n", ["source_path"])
    assert err is not None and "missing_p" in err
