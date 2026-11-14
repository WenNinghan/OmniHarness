# dev-medium-config

修复 merge_config(layers)，按从左到右优先级合并到默认 enabled=true,retries=3,label=local；显式false/0/空字符串保留。只准这3字段；enabled严格bool，retries严格int且0..9（bool不算int），label为str且长度≤30；未知键或非法值ValueError。不得修改defaults或输入。CLI接收若干JSON对象参数，成功输出sort_keys JSON和exit0，非法输入stderr INVALID_CONFIG和exit2且stdout空。

公开检查：`python -B smoke.py`。公开检查只覆盖基础用例。
只修改：cli.py, config.py, values.py。不要修改 smoke/README，不安装依赖或联网。
