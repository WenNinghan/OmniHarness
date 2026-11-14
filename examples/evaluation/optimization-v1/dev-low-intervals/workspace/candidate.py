def merge_intervals(intervals):
    result = [intervals[0]]
    for item in sorted(intervals)[1:]:
        if item[0] <= result[-1][1]: result[-1][1] = item[1]
        else: result.append(item)
    return result
