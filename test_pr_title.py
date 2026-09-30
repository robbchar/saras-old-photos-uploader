import pytest

import pr_title


@pytest.mark.parametrize("change_type", pr_title.ALLOWED_TYPES)
def test_each_allowed_type_passes(change_type):
    assert pr_title.title_problems(f"{change_type}: do the thing") == []


@pytest.mark.parametrize(
    "title",
    [
        "feat(upload-page): add a theme picker",
        "fix(e2e)!: drop the old lock tab",
        "feat!: change the identifier scheme",
        "chore(deps-dev): bump vitest from 5.0.0 to 5.1.0",
        "chore(main): release 1.2.0",
        "  fix: surrounding whitespace is ignored  ",
    ],
)
def test_conventional_titles_pass(title):
    assert pr_title.title_problems(title) == []


@pytest.mark.parametrize(
    "title",
    [
        "Add a theme picker",
        "feat add a theme picker",
        "feat:add a theme picker",
        "feat: ",
        "Feat: add a theme picker",
        "feat(): add a theme picker",
        "feat(upload page): add a theme picker",
        "",
    ],
)
def test_malformed_titles_fail(title):
    assert pr_title.title_problems(title) != []


def test_an_unknown_type_is_named_with_the_allowed_list():
    problems = pr_title.title_problems("feature: add a theme picker")
    assert len(problems) == 1
    assert '"feature"' in problems[0]
    assert "feat" in problems[0] and "fix" in problems[0]


def test_main_exits_zero_and_prints_nothing_for_a_valid_title(capsys):
    assert pr_title.main(["fix: handle a 503"]) == 0
    assert capsys.readouterr().err == ""


def test_main_exits_one_and_prints_the_problem_for_an_invalid_title(capsys):
    assert pr_title.main(["handle a 503"]) == 1
    assert "type(scope): subject" in capsys.readouterr().err


def test_main_exits_two_without_exactly_one_argument(capsys):
    assert pr_title.main([]) == 2
    assert "usage" in capsys.readouterr().err
