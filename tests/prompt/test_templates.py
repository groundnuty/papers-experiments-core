"""Prompt templates with named slots: strict rendering, study-run refusals and the two recorded hashes."""

import hashlib
import json

import pytest

from expcore.prompt.templates import PromptError, PromptTemplate
from expcore.records.build import PromptText

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


def test_b15_a_json_example_in_a_template_reaches_the_model_with_its_braces():
    """B15: adds that the text sent is the template with its slots filled and nothing else changed: braces that do not
    enclose a slot name, a JSON example's included, are kept as they are, with no escape needed; a capital-letter name
    in braces is always a slot, so it is the one form a template cannot show literally (a value can)."""
    text = 'Answer about {TASK} as JSON: {"answer": "<fact>", "sources": [{"id": 1}]}, or {} if none; {lower} stays.'
    rendered = PromptTemplate(text).render({"TASK": "lake"})
    assert rendered.text == text.replace("{TASK}", "lake")
    assert PromptTemplate(text).slot_names == ("TASK",)
    shown = PromptTemplate("Fill in {FORM}.").render({"FORM": "{NAME}"})  # a literal slot form, given as a value
    assert shown.text == "Fill in {NAME}."


def test_b15_a_nested_template_is_recorded_as_the_outer_slots_text_without_the_inner_hash():
    """B15: adds the known limit of nesting: an inner template rendered first and passed as an outer slot's value is
    sent as its text, and the record keeps the outer template's hash and that text as the slot's value; the inner
    template's hash and slot values are not in the record, so a study that needs them keeps the inner template's text
    stable or records them itself (an instrument snapshot, for one)."""
    inner = PromptTemplate("Use {TOOL} only.").render({"TOOL": "lookup"}, study_run=True)
    outer = PromptTemplate("Task: {TASK}. {RULES}").render({"TASK": "lake", "RULES": inner.text}, study_run=True)
    assert outer.text == "Task: lake. Use lookup only."
    entry = PromptText.of(outer).entry()
    assert entry["slots"] == {"TASK": "lake", "RULES": "Use lookup only."}
    assert entry["template_sha256"] == outer.template_sha256 != inner.template_sha256
    assert inner.template_sha256 not in json.dumps(entry)  # nothing of the inner template but its text
    assert "TOOL" not in entry["slots"]


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
