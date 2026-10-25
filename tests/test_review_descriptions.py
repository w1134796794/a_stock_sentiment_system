import pytest

from web.app import templates


@pytest.mark.parametrize(
    ("name", "visible"),
    [("涨停梯队", False), ("概念连板梯队", False), ("其他数据", True)],
)
def test_only_echelon_descriptions_are_hidden(name, visible):
    description = "DESCRIPTION_SENTINEL"
    html = templates.get_template("partials/section.html").render(
        section={"name": name, "kind": "table", "rows": [], "columns": [], "summary": description},
        view="echelon",
    )
    assert (description in html) is visible
    assert name in html
