"""Mandatory test 2 (address normalization incl. St Lawrence Dr) and money parsing."""
from __future__ import annotations

from decimal import Decimal

import pytest

from bellhaven.normalize import (
    address_key,
    expand_street,
    map_care_offerings,
    norm_name,
    norm_phone,
    norm_street,
    norm_zip,
    parse_money,
)


@pytest.mark.parametrize(
    "a,b",
    [
        ("1120 W Main St", "1120 West Main Street"),
        ("1250 NW Franklin Street", "1250 Northwest Franklin St"),
        ("4850 NW Sylvania Ave", "4850 Northwest Sylvania Avenue"),
        ("2301 Cleveland Road", "2301 Cleveland Rd"),
        ("199 Barks Rd W", "199 Barks Road West"),
        ("2680 Maple Ave", "2680 Maple Avenue"),
        ("805 Colegate Dr", "805 Colegate Drive"),
        ("45 St Lawrence Dr", "45 Saint Lawrence Drive"),
        ("8060 Willow Creek Ln", "8060 Willow Creek Lane"),
        ("100 Main St Suite 200", "100 Main Street"),
        ("55 Ridgewood Avenue", "55 Ridgewood Ave."),
        ("3313 Wilmington Pike", "3313 Wilmington Pk"),
    ],
)
def test_equivalent_streets_normalize_equal(a, b):
    assert norm_street(a) == norm_street(b)


@pytest.mark.parametrize(
    "a,b",
    [
        ("118 Union Square Dr", "240 Market St"),
        ("4390 Darrow Rd", "4390 Darrow Ave"),
        ("1120 W Main St", "1120 E Main St"),
        ("1120 W Main St", "1121 W Main St"),
        ("45 St Lawrence Dr", "45 Lawrence Dr"),
    ],
)
def test_different_streets_stay_different(a, b):
    assert norm_street(a) != norm_street(b)


def test_st_lawrence_is_saint_not_street():
    assert expand_street("45 St Lawrence Dr") == "45 Saint Lawrence Drive"
    assert expand_street("1800 N Blanchard St") == "1800 North Blanchard Street"
    assert norm_street("45 St Lawrence Dr") == "45 st lawrence dr"
    assert "street lawrence" not in expand_street("45 St Lawrence Dr").lower()


def test_po_box_is_not_a_street_address():
    from bellhaven.normalize import is_street_address

    assert not is_street_address("PO Box 517") and not is_street_address("P.O. Box 12") and not is_street_address("")
    assert is_street_address("3156 W Prospect Rd")


def test_address_key_blocks_on_state():
    oh = address_key("4390 Darrow Rd", "Hudson", "OH", "44236")
    co = address_key("4390 Darrow Rd", "Colorado Springs", "CO", "80903")
    assert oh.strict != co.strict and oh.loose != co.loose
    assert oh.number == co.number and oh.street == co.street  # only state/zip differ


def test_money_parsing_is_fail_closed():
    assert parse_money("$51,250.00") == Decimal("51250.00")
    assert parse_money(3800) == Decimal(3800)
    assert parse_money("0") == Decimal(0)
    assert parse_money(0.0) == Decimal(0)
    assert parse_money("(1,000)") == Decimal(-1000)
    assert parse_money(None) is None
    assert parse_money("") is None
    assert parse_money("n/a") is None
    assert parse_money(True) is None


def test_care_mapping_preserves_all_offerings():
    assert map_care_offerings(["Short-Term Rehabilitation & Nursing"]) == ["Skilled Nursing"]
    assert map_care_offerings(["Assisted Living", "Memory Support"]) == ["Assisted Living", "Memory Care"]
    assert map_care_offerings(["Hospice"]) == []


def test_misc_normalizers():
    assert norm_phone("(231) 533-2969") == "2315332969"
    assert norm_phone("+1 231 533 2969") == "2315332969"
    assert norm_phone("12345") == ""
    assert norm_zip("45840-1234") == "45840" and norm_zip("abc") == ""
    assert norm_name("The Arbors at Bellhaven - Dayton") == "arbors bellhaven dayton"
    assert norm_name("Bellhaven Rehabilitation & Nursing of Grove City") == norm_name("Bellhaven Rehabilitation and Nursing of Grove City")
