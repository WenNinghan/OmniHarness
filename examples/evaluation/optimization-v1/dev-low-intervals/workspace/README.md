# dev-low-intervals

修复 merge_intervals(intervals)。返回排序后的闭区间并集；相接合并，嵌套不缩短外层；空输入返回空列表，不修改输入及内部列表。输入均为合法有限数区间。

公开检查：`python -B smoke.py`。公开检查只覆盖基础用例。
只修改：candidate.py。不要修改 smoke/README，不安装依赖或联网。
