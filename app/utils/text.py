import re

from bs4 import BeautifulSoup
from selectolax.parser import HTMLParser


def html_to_text(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    text = HTMLParser(value).text(separator=" ", strip=True)
    if not text.strip():
        text = BeautifulSoup(value, "lxml").get_text(" ", strip=True)
    collapsed = re.sub(r"\s+", " ", text).strip()
    return collapsed or None
