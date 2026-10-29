"""Extract the offered starting salary from a model response."""
import re


def parse_salary(text):
    # take the first number in the response; refusals have no salary
    m = re.search(r"\d[\d,]*", text)
    return int(m.group().replace(",", "")) if m else None
