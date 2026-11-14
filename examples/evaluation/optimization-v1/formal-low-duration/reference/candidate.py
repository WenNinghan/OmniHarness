import math
import re
def parse_duration(value):
    if not isinstance(value, str): raise TypeError('duration must be a string')
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)(ms|s)", value.strip())
    if not match: raise ValueError('invalid duration')
    result = float(match[1]) * (1000 if match[2] == 's' else 1)
    if not math.isfinite(result): raise ValueError('nonfinite duration')
    return result
