# 调用协议、版本与别名语义、并发与超时规则

本文档描述网关对外行为的精确语义。所有结论都以稳定的 **状态名** 或
**error_code** 表达，便于程序化区分，也便于复现。

## 1. 对象模型

- **函数（function）**：按名称注册的本地可调用单元，如 `echo`。
- **版本（version）**：函数下不可变的发布单元 `(function, version)`，携带
  一份固定配置 `VersionConfig{ timeout_seconds, max_concurrency }`。
- **别名（alias）**：函数内 `alias -> {version: weight}` 的路由表，可原子
  改指。权重为整数百分比，合计必须恰好 100；只有一个版本时退化为
  `{version: 100}`，即经典的单版本别名。
- **调用目标（target）**：两类，`version:<v>` 与 `alias:<a>`，二者永不混淆。
- **调用记录（InvocationRecord）**：`request_id / function / resolved_version /
  state / result / error / error_code / started_at / finished_at`。

## 2. 注册与版本语义

1. 首次注册成功后，该版本的处理器与配置**永久冻结**。
2. 再次注册同一 `(function, version)`：拒绝，`error_code=VERSION_ALREADY_EXISTS`
   （HTTP 409）。不会覆盖处理器、不会重置配置。
3. 调用未发布的版本：拒绝，`VERSION_NOT_FOUND`（404）；
   函数名不存在：`FUNCTION_NOT_FOUND`（404）。
4. 非法名称、不可调用处理器、`timeout<=0`、`max_concurrency<1`：
   `INVALID_REQUEST`（400）。
5. 版本只能新增、不能删除；下线通过“函数级启停”完成（见第 5 节）。

HTTP 注册端点：`POST /admin/functions/{name}/versions/{version}`，
本地函数在进程内绑定（见 `app/demo.py` 的 `HANDLER_TABLE`），平台不从
网络上传任意代码。

## 3. 调用协议

### 3.1 同步调用

```
POST /functions/{name}/versions/{version}/invoke
POST /functions/{name}/aliases/{alias}/invoke
{ "request_id": "字符串，必填", "payload": 任意 JSON，可空 }
```

- 阻塞到终态后返回；HTTP 状态码映射：200 成功、502 处理器失败、
  504 超时、429 并发限流。
- 准入类拒绝（未发布、已下线、冲突等）在执行前返回，见第 6 节。

### 3.2 异步调用

```
POST .../invoke-async          -> 202, state=ACCEPTED
GET  /requests/{request_id}    -> 当前记录快照
```

- 立即返回 `ACCEPTED`，随后后台迁移到 `RUNNING` 与终态。
- 轮询端返回与同步调用**同一份**调用记录；未知 request id 返回
  404 `REQUEST_NOT_FOUND`。

### 3.3 请求标识与幂等收敛

“同一请求”的判定（指纹）= `function + target 类型 + target 名称 + payload`。

- 同一 `request_id` 且指纹相同：只允许存在一个执行者（owner）。
  - owner 尚在执行：后来的提交**附着**到该次执行并等待其终态；
  - owner 已结束：后来的提交直接返回**同一个终态记录**。
  - 因此无论重复投递多少次、并发还是串行，结论恒等（收敛）。
- 同一 `request_id` 但指纹不同：拒绝，`REQUEST_CONFLICT`（409），
  已存在的结论不受影响。
- 准入前被拒绝的请求不占用 request id（例如函数下线期间投递被拒，
  函数恢复后可用同一 id 重新提交）。

> 注意：按别名调用时，指纹包含别名本身而非解析后的版本。切换别名（含调整
> 权重）后用同一 request id 重试属于不同请求，这保证“切换后重试”不会被旧
> 结论吞掉。权重分流不改变收敛语义：同一 request id 的重复提交无论被随机
> 分到哪个版本，都附着到首次准入时钉住的版本，收敛为同一份记录。

## 3.4 权重分流（灰度放量）

一个别名可以同时指向多个版本，每个版本一个整数百分比权重：

```
PUT /admin/functions/{name}/aliases/{alias}
{ "alias": "current", "weights": { "v1": 90, "v2": 10 } }
```

- **按比例分发**：每次准入时在注册表锁内做一次加权随机选择，请求按
  权重比例落到不同版本；被选中的版本即钉在本次调用的
  `resolved_version` 上（见 6.1）。
- **在线生效**：权重调整是原子换表，无需重启；调整之后的新调用立即
  按新比例走，在途调用继续跑准入时的版本，结果只计入该版本的计数。
- **平滑下线**：把某版本权重调为 0，它立即不再获得新流量，在途调用
  照常跑完；观察总览接口确认 `inflight == 0` 后即可安全停用。
- **非法配置全部拒绝且原因可区分**（HTTP 状态 + error_code）：

  | 配置 | error_code | HTTP |
  |---|---|---|
  | 一个版本都没配（空表） | `ROUTING_EMPTY` | 400 |
  | 权重合计 ≠ 100 | `WEIGHT_SUM_INVALID` | 400 |
  | 权重为负数 | `WEIGHT_NEGATIVE` | 400 |
  | 指向未发布版本 | `VERSION_NOT_FOUND` | 404 |
  | 权重非整数 / 超出 [0,100] / 名称非法 | `INVALID_REQUEST` | 400 |

  被拒绝的配置不会落库：别名保持调整前的路由表不变。
- **兼容关系**：`set_alias` / `{ "version": "v1" }` 的单版本写法等价于
  `{ "v1": 100 }`，直接按版本调用完全不经过权重逻辑；两者行为与引入
  分流前一致。

### 3.5 运行总览（判断版本是否已排空）

```
GET /functions/{name}/overview
```

返回每个别名的当前权重表，以及每个版本的实时账目：

```json
{
  "function": "echo",
  "enabled": true,
  "aliases": { "current": { "v1": 90, "v2": 10 } },
  "versions": {
    "v1": { "inflight": 2, "max_concurrency": 8, "timeout_seconds": 2.0,
            "terminal": { "SUCCEEDED": 41, "TIMED_OUT": 1 } },
    "v2": { "inflight": 0, "max_concurrency": 8, "timeout_seconds": 2.0,
            "terminal": { "SUCCEEDED": 5 } }
  }
}
```

`inflight` 是该版本**此刻真正在跑**的调用数（与槽位计数同源，正常
返回、报错、超时都会即时回收，不漂移）；`terminal` 是各终态的累计
结论数。某版本权重为 0 且 `inflight` 为 0，即表示它已彻底没人走。

## 4. 并发规则

1. 每个**具体版本**有独立的并发计数器与容量为 `max_concurrency` 的槽位；
   不同版本（即使属于同一函数、同一别名分流到的流量）互不影响。
2. 准入时槽位获取为**非阻塞**：拿不到立即终态 `THROTTLED`
   （`error_code=THROTTLED`，HTTP 429）。**不做静默排队**。
3. 槽位在执行作用域结束时（成功 / 失败 / 超时 / 提交通道失败）于
   `finally` 中释放；计数只增只减在锁保护下进行。
4. 超时的处理器线程可能稍后才真正结束，但槽位在**超时判定的那一刻**
   即回收，不会被迟到返回继续占用（日志：`invoke.reclaim ...
   reason=execution_scope_closed`）。
5. 每次定案（含 `THROTTLED`）在对应版本的终态直方图上累计一次；
   一个被准入的 request id 恰好产生一条终态计数，因此
   `sum(terminal)` 恒等于该版本定案过的请求数，`inflight` 恒等于
   真实在跑数——压测再久也不漂移（见 3.5 总览接口）。
6. 线程池是全局天花板（默认 256 个工作线程），正常路径下由各版本
   `max_concurrency` 先行限流，避免无限创建线程。

## 5. 超时规则

1. 每个版本配置独立的 `timeout_seconds` 硬期限，从拿到槽位、开始执行算起。
2. 处理器在工作线程中执行，主线程用 `Future` + 超时等待：
   - 期限内返回 → `SUCCEEDED`；
   - 期限内抛异常 → `FAILED`（`HANDLER_ERROR`），异常信息原样记录；
   - 到期未返回 → 立即定案 `TIMED_OUT`（HTTP 504），尽力 cancel，
     记录不再变更，迟到返回被丢弃。
3. 超时是终态且与 `FAILED`/`THROTTLED` 三者互斥可区分。

## 6. 启停与别名切换（生命周期）

### 6.1 版本钉住（version pinning）

调用在**准入瞬间**把目标解析成具体版本对象并保存在调用记录上
（`resolved_version`），之后整个执行过程只引用该对象。因此：

- 别名在执行期间被切换（含权重调整、份额降为 0）：在途调用继续跑旧版本，
  结果写回旧版本的执行与计数，不会串入新版本；切换之后的新调用按新
  路由表解析。
- 函数被下线：在途调用不受影响、结果不丢失；下线只拦截**新准入**，
  新调用得到 `FUNCTION_DISABLED`（409）。重新启用后恢复正常。

### 6.2 原子性

- 别名指针的读写都在注册表锁内完成，读者只会看到旧版或新版，不存在中间态。
- request id 的“建条目 / 指纹比对 / 成为 owner”在同一套锁协议下完成，
  并发重复提交恰好选出一个 owner。

### 6.3 管理端点

| 端点 | 作用 |
|---|---|
| `POST /admin/functions/{name}/versions/{version}` | 注册不可变版本（处理器需已在本地绑定） |
| `PUT  /admin/functions/{name}/aliases/{alias}` | 原子设置/移动别名：body `{alias, version}` 为单版本， `{alias, weights:{v:w}}` 为权重分流；目标版本必须已发布 |
| `POST /admin/functions/{name}/status` | 启停，body: `{"enabled": bool}` |
| `GET  /functions/{name}/overview` | 各别名权重表 + 各版本 inflight/上限/终态累计（见 3.5） |

## 7. 状态机

```
首次准入 ──同步──> RUNNING ──┬─> SUCCEEDED
        └─异步─> ACCEPTED ──> RUNNING ├─> FAILED (HANDLER_ERROR)
                                     ├─> TIMED_OUT
并发已满（准入后、取槽失败）──────────> THROTTLED
```

所有带箭头的迁移都在 `gateway.engine` 日志中输出 `request_id`、版本、
计数与 `reason=...`，例如：

```
invoke.admit      ... reason=first_submission
invoke.dedupe     ... reason=request_id_resubmitted
invoke.start      ... reason=slot_acquired
invoke.throttled  ... reason=concurrency_capacity_exceeded
invoke.timed_out  ... reason=timeout_budget_exhausted
invoke.failed     ... reason=handler_exception
invoke.succeeded  ... reason=handler_returned
invoke.duplicate  ... reason=shared_request_id_converged
invoke.reclaim    ... reason=execution_scope_closed
```

注册表侧：`registry.register ... reason=first_registration_immutable`、
`registry.alias_switch ... atomic=true`、
`registry.alias_weights ... scope=new_invocations_only`（权重换表）、
`registry.weighted_pick ... weights={...} chosen=<v>`（每次分流选择）、
`registry.reject ... reason=function_disabled`。

## 8. 可复现性说明

- 所有拒绝原因均为枚举化字符串，测试对状态与 error_code 做精确断言。
- 并发用例通过 `Barrier` 对齐线程起跑，制造真实竞态；结论只依赖计数器
  与状态机，不依赖时序运气（例如“恰有 N 个 THROTTLED”）。
- 时间用例使用单调时钟（`time.monotonic`），记录中同时给出起止戳与
  `elapsed_seconds`。
