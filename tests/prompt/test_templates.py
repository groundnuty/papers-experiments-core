"""Prompt templates with named slots: strict rendering, study-run refusals and the two recorded hashes."""

import hashlib

import pytest

from expcore.prompt.templates import PromptError, PromptTemplate

TEXT = "Study {AREA} between {START} and {END}.\nYou may use {TOOLS}. Never show {AREA} twice in a row."


def values(**overrides):
    return {"AREA": "the north", "START": "1 May", "END": "2 May", "TOOLS": "grep"} | overrides


def test_a_template_names_its_slots_once_each_in_sorted_order():
    assert PromptTemplate(TEXT).slot_names == ("AREA", "END", "START", "TOOLS")
    assert PromptTemplate("no slots here, and {lower} or {not a slot} stay text").slot_names == ()


def test_rendering_fills_every_occurrence_and_reports_what_it_inserted():
    rendered = PromptTemplate(TEXT).render(values(START=3))
    assert (
        rendered.text == "Study the north between 3 and 2 May.\nYou may use grep. Never show the north twice in a row."
    )
    assert rendered.slots["START"] == "3"


def test_any_number_of_slots_works_including_none():
    assert PromptTemplate("plain").render({}).text == "plain"
    many = " ".join("{S" + str(i) + "}" for i in range(30))
    assert PromptTemplate(many).render({f"S{i}": i for i in range(30)}).text == " ".join(map(str, range(30)))


def test_a_missing_slot_raises_and_names_it():
    with pytest.raises(PromptError, match=r"no value for prompt slot \{END\}"):
        PromptTemplate(TEXT).render({k: v for k, v in values().items() if k != "END"})


@pytest.mark.parametrize("blank", [None, "", "   ", "\n"])
def test_an_unfilled_slot_raises(blank):
    with pytest.raises(PromptError, match=r"prompt slot \{TOOLS\} is unfilled"):
        PromptTemplate(TEXT).render(values(TOOLS=blank))


def test_a_value_for_a_slot_the_template_lacks_raises_instead_of_being_ignored():
    with pytest.raises(PromptError, match="slots the template does not have: ACCES"):
        PromptTemplate(TEXT).render(values(ACCES="typo"))


def test_a_value_name_that_cannot_be_a_slot_raises():
    with pytest.raises(PromptError, match="not a slot name"):
        PromptTemplate("{A}").render({"a": "x"})


def test_a_value_is_inserted_as_it_is_and_never_expanded_again():
    rendered = PromptTemplate("{FIRST} then {SECOND}").render({"FIRST": "{SECOND}", "SECOND": "two"})
    assert rendered.text == "{SECOND} then two"


def test_a_study_run_refuses_a_placeholder_in_a_value_in_any_case():
    template = PromptTemplate(TEXT)
    assert template.render(values(AREA="PLACEHOLDER area")).text.startswith("Study PLACEHOLDER area")  # a pilot may
    for draft in ("PLACEHOLDER", "see PlaceHolder", "xPLACEHOLDERx"):
        with pytest.raises(PromptError, match="refuses PLACEHOLDER in the values of: AREA"):
            template.render(values(AREA=draft), study_run=True)


def test_a_study_run_refuses_a_template_that_still_has_a_placeholder():
    with pytest.raises(PromptError, match="template that contains PLACEHOLDER"):
        PromptTemplate("{A} PLACEHOLDER").render({"A": "x"}, study_run=True)


def test_the_template_and_rendered_hashes_are_recorded():
    template = PromptTemplate(TEXT)
    one, two = template.render(values()), template.render(values(AREA="the south"))
    assert one.template_sha256 == two.template_sha256 == hashlib.sha256(TEXT.encode()).hexdigest()
    assert one.rendered_sha256 == hashlib.sha256(one.text.encode()).hexdigest()
    assert one.rendered_sha256 != two.rendered_sha256
    assert template.render(values()).rendered_sha256 == one.rendered_sha256


def test_a_template_file_is_read_and_can_be_pinned_to_a_hash(tmp_path):
    path = tmp_path / "prompt.txt"
    path.write_text(TEXT, encoding="utf-8")
    pinned = hashlib.sha256(TEXT.encode()).hexdigest()
    assert PromptTemplate.from_file(path, expected_sha256=pinned).text == TEXT
    with pytest.raises(PromptError, match="not the pinned"):
        PromptTemplate.from_file(path, expected_sha256="0" * 64)


def test_a_template_file_that_is_missing_is_an_error_that_names_it(tmp_path):
    with pytest.raises(PromptError, match=r"absent\.txt"):
        PromptTemplate.from_file(tmp_path / "absent.txt")
