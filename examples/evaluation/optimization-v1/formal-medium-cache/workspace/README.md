# formal-medium-cache

修复泛型TTLCache(capacity,clock)。capacity必须非负整数，否则RangeError；ttl有限非负否则RangeError且原状态不变。now>=到期时失效。set覆盖同key并更新TTL/最近使用位置，ttl0立即删除旧key。get命中才更新LRU，不刷新TTL；has不改变LRU。容量溢出驱逐最久未使用的未过期项；容量0不存值。size只计未过期项。允许存undefined，has必须区分不存在；delete返回是否删除。不得真实sleep或取系统时间，必须使用注入clock。

公开检查：`node --experimental-strip-types smoke.ts`。公开检查只覆盖基础用例。
只修改：cache.ts, clock.ts。不要修改 smoke/README，不安装依赖或联网。
