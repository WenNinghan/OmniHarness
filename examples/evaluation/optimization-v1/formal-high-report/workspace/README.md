# formal-high-report

修复离线报告流水线。JSON为[{category:非空字符串,cents:非负安全整数}]；CSV严格header category,cents，标准引号/双引号转义/逗号/换行及CRLF，金额为无前导0的非负安全整数字符串。非法输入TypeError。summarize(rows,minimum=0)按cents>=minimum过滤、按category精确分组、统计count/totalCents，按JS字符串比较升序；minimum非法或累计溢出RangeError，输入不改动，原型属性名是普通键。toCSV固定header category,count,totalCents，必要转义，末尾恰好换行；空结果仅header。report(text,format,minimum)统一导出。node --experimental-strip-types cli.ts INPUT csv|json OUTPUT [MINIMUM] 成功exit0；失败exit2 stderr INVALID_REPORT且旧OUTPUT不变、不产生半份文件。

公开检查：`node --experimental-strip-types smoke.ts`。公开检查只覆盖基础用例。
只修改：cli.ts, format.ts, parse.ts, pipeline.ts, stats.ts, types.ts。不要修改 smoke/README，不安装依赖或联网。
