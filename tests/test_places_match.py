"""A place-photo match must agree on the name's own words, not just a brand."""
import asyncio

from app.services.places_service import _is_relevant_match


def match(q, found):
    return asyncio.run(_is_relevant_match(q, found))


def test_another_taj_is_not_this_taj():
    assert not match("Taj Hari Mahal", "Taj Mahal Tower, Mumbai")
    assert not match("The Oberoi Udaivilas", "The Oberoi Amarvilas, Agra")


def test_the_same_place_still_matches():
    assert match("Taj Lake Palace", "Taj Lake Palace, Udaipur")
    assert match("Umaid Bhawan Palace", "Taj Umaid Bhawan Palace")
    assert match("The Oberoi", "The Oberoi, New Delhi")
