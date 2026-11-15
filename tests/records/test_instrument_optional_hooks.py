"""C31: the optional hooks the core looks for on an instrument are one exported list, the core's discovery uses exactly
that list, and `missing_hooks` tells a study's own test what a wrapper around an instrument fails to pass on."""

import ast
import inspect
from collections.abc import Mapping
from typing import Any

import pytest
from record_support import hand_made

from expcore.errors import InfraError
from expcore.records import instruments as core_instruments
from expcore.records.instruments import OPTIONAL_HOOKS, finish_instruments, missing_hooks

REFUSED = "tool_plane_integrity"  # a core kind no condition may declare as its treatment
REQUIRED = {"namespace", "start", "finish"}  # what every instrument has: not optional, so not in the list
LOOKUPS = {"_method", "getattr", "hasattr"}


def discovered_names(source: str) -> set[str]:
    """The names a module's source looks an optional member up by: the literal second argument of every call of
    `_method`, `getattr` and `hasattr`, less the members every instrument has."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in LOOKUPS):
            continue
        if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str):
            names.add(node.args[1].value)
    return names - REQUIRED


class Bare:
    """An instrument with neither hook."""

    namespace = "bare"

    def start(self, identity: Mapping[str, Any]) -> None:
        pass

    def finish(self) -> Mapping[str, Any]:
        return {}


class Hooked(Bare):
    """An instrument with both hooks, the second of which refuses."""

    namespace = "hooked"

    def at_first_turn(self, moment: Mapping[str, Any]) -> None:
        pass

    def check(self) -> None:
        raise InfraError(REFUSED, "the manipulation was not realised")


class Wrapper:
    """A study's wrapper that passes `start` and `finish` on and, as written, nothing else: it drops both hooks."""

    def __init__(self, wrapped):
        self.wrapped = wrapped
        self.namespace = wrapped.namespace

    def start(self, identity):
        self.wrapped.start(identity)

    def finish(self):
        return self.wrapped.finish()


class Forwarding(Wrapper):
    """A wrapper that passes the hooks on by name, whatever the wrapped instrument has."""

    def __getattr__(self, name):
        return getattr(self.wrapped, name)


def test_c31_the_exported_list_names_each_optional_hook_once_and_none_of_the_required_members():
    """C31: the list is a tuple of distinct names, and a hook the core looks for is never one every instrument has."""
    assert isinstance(OPTIONAL_HOOKS, tuple)
    assert all(isinstance(name, str) for name in OPTIONAL_HOOKS)
    assert len(set(OPTIONAL_HOOKS)) == len(OPTIONAL_HOOKS)
    assert not REQUIRED & set(OPTIONAL_HOOKS)
    assert {"at_first_turn", "check"} <= set(OPTIONAL_HOOKS)


def test_c31_the_list_is_every_optional_name_the_discovery_code_looks_an_instrument_up_by():
    """C31: a hook the discovery code calls but the list lacks (or one the list names that nothing looks up) fails
    here, so a hook added to the core without the list, and so without the wrapper rule, is caught."""
    found = discovered_names(inspect.getsource(core_instruments))
    assert found == set(OPTIONAL_HOOKS)


def test_c31_the_scan_finds_a_hook_looked_up_by_any_of_the_three_ways_and_leaves_the_required_members():
    """C31: the scan is not blind to a hook looked up through the helper, `getattr` or `hasattr`, and a lookup of a
    member every instrument has is no hook."""
    source = (
        "def f(instrument):\n"
        "    _method(instrument, 'a_hook')\n"
        "    getattr(instrument, 'b_hook', None)\n"
        "    hasattr(instrument, 'c_hook')\n"
        "    getattr(instrument, 'namespace', None)\n"
        "    getattr(instrument, name, None)\n"
    )
    assert discovered_names(source) == {"a_hook", "b_hook", "c_hook"}


def test_c31_the_discovery_helper_refuses_a_name_that_is_not_in_the_list():
    """C31: a hook the core calls by a name the list lacks is a defect of the core, refused where it is looked up and
    not found only by a test."""
    with pytest.raises(ValueError, match="OPTIONAL_HOOKS"):
        core_instruments._method(Hooked(), "at_last_turn")


def test_c31_a_wrapper_that_passes_both_hooks_on_misses_nothing():
    """C31: the helper is empty for a wrapper that forwards every hook the wrapped instrument has."""
    assert missing_hooks(Forwarding(Hooked()), Hooked()) == ()


def test_c31_a_wrapper_that_drops_the_hooks_is_told_which_ones_in_the_lists_order():
    """C31: the names the wrapped instrument has and the wrapper does not, in the order of `OPTIONAL_HOOKS`."""
    wrapped = Hooked()
    assert missing_hooks(Wrapper(wrapped), wrapped) == ("at_first_turn", "check") == OPTIONAL_HOOKS


def test_c31_only_a_hook_the_wrapped_instrument_has_can_be_missing():
    """C31: a wrapper around an instrument with one hook misses that one, and around one with none misses nothing."""

    class OnlyCheck(Bare):
        def check(self) -> None:
            pass

    assert missing_hooks(Wrapper(OnlyCheck()), OnlyCheck()) == ("check",)
    assert missing_hooks(Wrapper(Bare()), Bare()) == ()


def test_c31_a_hook_the_wrapper_adds_is_not_missing():
    """C31: the wrapper may have a hook the wrapped instrument lacks; the helper names only what is dropped."""

    class Adding(Wrapper):
        def check(self) -> None:
            pass

    assert missing_hooks(Adding(Bare()), Bare()) == ()


def test_c31_a_hook_that_is_not_callable_is_no_hook_on_either_side_as_the_core_sees_it():
    """C31: the core calls a hook only when the attribute is callable, so the helper judges the same way: an attribute
    that is not callable on the wrapped instrument is nothing to pass on, and one on the wrapper that is not callable
    passes nothing on."""

    class NoCheck(Bare):
        check = None

    class DeadCheck(Wrapper):
        check = None

    assert missing_hooks(Wrapper(NoCheck()), NoCheck()) == ()
    assert missing_hooks(DeadCheck(Hooked()), Hooked()) == ("at_first_turn", "check")


def test_c31_a_hook_a_property_withholds_is_not_one_the_wrapped_instrument_has():
    """C31: an instrument whose hook is a property that answers None in one mode has no hook in it (the progressive
    ingest's first-turn hook is such a property), so a wrapper that lacks it misses nothing then, and misses it when
    the property answers a method."""

    class Sometimes(Bare):
        def __init__(self, on):
            self.on = on

        @property
        def at_first_turn(self):
            return (lambda moment: None) if self.on else None

    assert missing_hooks(Wrapper(Sometimes(False)), Sometimes(False)) == ()
    assert missing_hooks(Wrapper(Sometimes(True)), Sometimes(True)) == ("at_first_turn",)


def test_c31_the_helper_names_what_the_core_would_silently_not_call():
    """C31: the hook the wrapper dropped is the one the core never calls: the wrapped instrument's check refuses, the
    wrapper's run records no refusal, and the helper names the check."""
    wrapped = Hooked()
    assert finish_instruments(hand_made(), [wrapped]).instrument_refusal is not None
    assert finish_instruments(hand_made(), [Wrapper(wrapped)]).instrument_refusal is None
    assert "check" in missing_hooks(Wrapper(wrapped), wrapped)
    assert finish_instruments(hand_made(), [Forwarding(wrapped)]).instrument_refusal is not None
