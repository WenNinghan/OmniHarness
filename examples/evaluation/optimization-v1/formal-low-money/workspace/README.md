# formal-low-money

实现 sumCents(values: readonly string[]): number。金额语法为非负十进制整数（除0外无前导0）及可选1或2位小数，不允许空白/符号/指数；以整数分精确求和。非法字符串或元素类型抛 TypeError；单项或累计超过 Number.MAX_SAFE_INTEGER 分抛 RangeError；空数组返回0，不修改输入。

公开检查：`node --experimental-strip-types smoke.ts`。公开检查只覆盖基础用例。
只修改：candidate.ts。不要修改 smoke/README，不安装依赖或联网。
