import deployment
from deployment import Check, CheckOutcome, Status


def passing(name="ok"):
    return Check(name=name, probe=lambda: CheckOutcome(Status.PASS, "fine"), remedy="none")


def failing(name="bad", fix=None):
    return Check(
        name=name,
        probe=lambda: CheckOutcome(Status.FAIL, "broken"),
        remedy="run the thing",
        fix=fix,
    )


def test_run_checks_returns_one_outcome_per_check_in_order():
    checks = [passing("first"), failing("second")]
    results = deployment.run_checks(checks)
    assert [check.name for check, _ in results] == ["first", "second"]
    assert [outcome.status for _, outcome in results] == [Status.PASS, Status.FAIL]


def test_run_checks_reports_a_raising_probe_as_unknown_not_fail():
    def explode():
        raise OSError("no idea")

    check = Check(name="flaky", probe=explode, remedy="try later")
    (_, outcome), = deployment.run_checks([check])
    assert outcome.status is Status.UNKNOWN
    assert "no idea" in outcome.detail


def test_format_report_prefixes_each_line_with_its_status():
    report = deployment.format_report(deployment.run_checks([passing("first"), failing("second")]))
    assert "[PASS] first" in report
    assert "[FAIL] second" in report


def test_format_report_prints_the_remedy_only_under_a_failure():
    report = deployment.format_report(deployment.run_checks([passing("first"), failing("second")]))
    assert "run the thing" in report
    assert report.index("run the thing") > report.index("[FAIL] second")
    assert report.count("run the thing") == 1


def test_exit_code_is_zero_when_nothing_failed():
    assert deployment.exit_code(deployment.run_checks([passing()])) == 0


def test_exit_code_is_zero_when_a_check_is_only_unknown():
    check = Check(name="offline", probe=lambda: CheckOutcome(Status.UNKNOWN, "no network"), remedy="x")
    assert deployment.exit_code(deployment.run_checks([check])) == 0


def test_exit_code_is_one_when_any_check_failed():
    assert deployment.exit_code(deployment.run_checks([passing(), failing()])) == 1


def test_converge_applies_fix_then_rechecks():
    state = {"broken": True}

    def probe():
        return CheckOutcome(Status.PASS, "fine") if not state["broken"] else CheckOutcome(Status.FAIL, "broken")

    def fix():
        state["broken"] = False
        return "unbroke it"

    check = Check(name="fixable", probe=probe, remedy="x", fix=fix)
    (_, outcome), = deployment.converge([check], announce=lambda _: None)
    assert outcome.status is Status.PASS


def test_converge_announces_before_it_acts():
    announced = []
    state = {"broken": True}

    def fix():
        assert announced, "fix ran before anything was announced"
        state["broken"] = False
        return "unbroke it"

    check = Check(
        name="fixable",
        probe=lambda: CheckOutcome(Status.PASS, "fine") if not state["broken"] else CheckOutcome(Status.FAIL, "broken"),
        remedy="x",
        fix=fix,
    )
    deployment.converge([check], announce=announced.append)
    assert any("fixable" in line for line in announced)


def test_converge_leaves_a_passing_check_alone_and_stays_quiet():
    announced = []
    deployment.converge([passing()], announce=announced.append)
    assert announced == []


def test_converge_does_not_call_fix_for_an_unknown_check():
    called = []
    check = Check(
        name="offline",
        probe=lambda: CheckOutcome(Status.UNKNOWN, "no network"),
        remedy="x",
        fix=lambda: called.append("fixed") or "fixed",
    )
    deployment.converge([check], announce=lambda _: None)
    assert called == []


def test_minimum_python_is_three_ten():
    assert deployment.MINIMUM_PYTHON == (3, 10)
