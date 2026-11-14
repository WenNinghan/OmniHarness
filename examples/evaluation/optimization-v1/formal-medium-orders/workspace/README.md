# formal-medium-orders

修复CSV订单库和CLI。CSV严格header id,customer,amount，每行三列，id/customer非空；id重复拒绝整批。标准CSV引号、逗号、换行及UTF-8必须可用。amount语法同非负整数及可选1/2小数位，除0无前导0；金额用整数分。summarize(text)返回按customer Unicode排序的[{customer,cents}]合计。非法输入ValueError，不部分返回。python cli.py input.csv output.json 成功exit0写UTF-8 JSON数组；非法输入exit2且stderr INVALID_ORDERS，目标原内容保持，目标不存在时不能创建。

公开检查：`python -B smoke.py`。公开检查只覆盖基础用例。
只修改：aggregate.py, cli.py, orders.py。不要修改 smoke/README，不安装依赖或联网。
