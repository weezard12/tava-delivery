"""Konimbo keeps the shipping address as one line; Cargo wants street and city apart."""

import re
from dataclasses import dataclass

_HOUSE_NUMBER = re.compile(r"\d+[א-תA-Za-z]?(?:/\d+)?")
_LABELLED = re.compile(r"(?:קומה|קומת)\s*(\d+)|(?:דירה|דירת)\s*(\d+)")


@dataclass(frozen=True, slots=True)
class Address:
    street: str
    city: str
    floor: str = ""
    apartment: str = ""


def parse_address(text: str | None) -> Address | None:
    """Split "הרצל 3, תל אביב" into street and city. None when the city cannot be told apart.

    The last comma separates the city, as Konimbo writes it. Floor and apartment written as
    "קומה 2" or "דירה 5" are read out of the street part.
    """
    if not text:
        return None
    line = " ".join(text.split())
    if "," in line:
        street, _, city = line.rpartition(",")
    else:
        # "הרצל 3 תל אביב": the city follows the house number.
        words = line.split()
        numbers = [index for index, word in enumerate(words) if _HOUSE_NUMBER.fullmatch(word)]
        if not numbers or numbers[0] in (0, len(words) - 1):
            return None
        split = numbers[0] + 1
        street, city = " ".join(words[:split]), " ".join(words[split:])
    street, city = street.strip(" ,"), city.strip(" ,")
    floor = apartment = ""
    for match in _LABELLED.finditer(street):
        floor = floor or (match.group(1) or "")
        apartment = apartment or (match.group(2) or "")
    street = " ".join(_LABELLED.sub("", street).replace(",", " ").split())
    if not street or not city or not any(ch.isalpha() for ch in city):
        return None
    if not _HOUSE_NUMBER.search(street) and not any(ch.isalpha() for ch in street):
        return None
    return Address(street=street, city=city, floor=floor, apartment=apartment)
