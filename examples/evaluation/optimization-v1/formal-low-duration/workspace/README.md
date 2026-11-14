# formal-low-duration

实现 parse_duration(value: str)->float，返回毫秒。仅接受首尾可有空白、内部无空白的十进制非负数（整数或小数，必须有整数位）加 s 或 ms；允许 00.50s，拒绝符号、指数、NaN/Infinity、尾随文本和无单位。非法格式/非有限结果抛 ValueError，非str抛 TypeError。

公开检查：`python -B smoke.py`。公开检查只覆盖基础用例。
只修改：candidate.py。不要修改 smoke/README，不安装依赖或联网。
