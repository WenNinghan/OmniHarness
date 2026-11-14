import re
def parse_duration(value):
    match = re.match(r"([0-9.]+)(ms|s)", value.strip())
    if not match: raise ValueError('duration')
    return float(match[1]) * 1000
