# 本地无服务器函数调用平台（Python）

一个在单进程内运行的函数注册 / 调用 / 生命周期网关。它不依赖任何外部
中间件，专注于让函数调用在 **并发、超时、重复投递** 三类扰动下给出
**一致、可区分、可复现** 的结论。

- 版本一经注册不可篡改；重复注册与调用未发布版本都会被拒绝，且拒绝原因可区分。
- 同步调用与可轮询结果的异步调用；同一 `request_id` 重复提交收敛为同一结论。
- 并发超限立即拒绝（`THROTTLED`，不排队）；超时立即定案（`TIMED_OUT`，不拖垮服务）。
- 别名原子切换、函数可启停；在途调用固定在准入时解析到的版本上，不串版、不丢结果。
- **灰度放量**：别名可挂多个版本并按整数百分比分流，在线调比例即时生效；
  非法配置（和不为 100、负权重、未发布版本、空表）分别以不同错误码拒绝。
- 每个版本独立记账：并发上限与超时照旧按版本生效，`inflight` 与四种终态
  累计计数可随时查询，长跑不漂移，权重降到 0 且 `inflight` 归零即可安全下线。
- 执行结束后并发计数即时回收；超时的“迟到返回”无法改写已定案的结论。

## 目录结构

```
app/
  types.py      # 枚举与值对象：InvocationState / Target / VersionConfig / VersionStats ...
  errors.py     # 拒绝原因分类（稳定 error_code）
  registry.py   # 版本注册表、加权别名（按比例分流）、启停
  engine.py     # 并发槽位、超时、request_id 去重收敛、分版本统计
  gateway.py    # 门面：组装 registry + engine
  http_api.py   # FastAPI 传输层
  demo.py       # 内置本地函数绑定表
main.py         # uvicorn 入口
tests/          # 50 个单测（并发 / 超时 / 去重 / 版本冲突 / 生命周期 / 灰度分流）
docs/protocol.md# 协议与语义的详细说明
```

## 快速开始

```bash
uv sync --dev          # 安装依赖（Python >= 3.14）
uv run python main.py  # 启动 HTTP 服务，默认 127.0.0.1:8000，文档 /docs
```

内置演示函数：

| 函数  | 版本 | 超时  | 并发上限 | 行为                     |
|-------|------|-------|----------|--------------------------|
| echo  | v1/v2 | 2s | 8 | 原样回显；别名 `current` |
| slow  | v1   | 1s   | 2        | `payload.seconds` 控制睡眠（必超时） |
| flaky | v1   | 2s   | 4        | 必定抛异常               |

同步调用（别名）：

```bash
curl -s -X POST http://127.0.0.1:8000/functions/echo/aliases/current/invoke \
  -H 'Content-Type: application/json' \
  -d '{"request_id":"demo-1","payload":{"hello":"world"}}'
```

异步调用并轮询：

```bash
curl -s -X POST http://127.0.0.1:8000/functions/echo/versions/v2/invoke-async \
  -H 'Content-Type: application/json' \
  -d '{"request_id":"demo-2","payload":"hi"}'
# -> 202 {"state":"ACCEPTED", ...}
curl -s http://127.0.0.1:8000/requests/demo-2
```

别名切换与启停：

```bash
curl -s -X PUT  http://127.0.0.1:8000/admin/functions/echo/aliases/current \
  -H 'Content-Type: application/json' -d '{"alias":"current","version":"v2"}'
curl -s -X POST http://127.0.0.1:8000/admin/functions/echo/status \
  -H 'Content-Type: application/json' -d '{"enabled":false}'
```

## 灰度放量（加权别名）

同一个别名可以挂多个版本，各配一个整数百分比权重，合计必须恰好 100。
走该别名的调用按比例落到不同版本；改比例**在线即时生效**，无需重启，
在途调用仍跑完在准入时钉住的版本上，结果也只记到实际执行的版本头上。

```bash
# 90% 留在 v1，10% 灰度到 v2
curl -s -X PUT http://127.0.0.1:8000/admin/functions/echo/aliases/current \
  -H 'Content-Type: application/json' \
  -d '{"alias":"current","weights":{"v1":90,"v2":10}}'

# 查看当前路由表与各版本账本（在跑数 / 上限 / 各终态累计）
curl -s http://127.0.0.1:8000/admin/functions/echo/aliases/current
curl -s http://127.0.0.1:8000/admin/functions/echo/stats

# 验证新版本无恙后全量；或把 v1 降到 0 观察 inflight 归零后安全下线
curl -s -X PUT http://127.0.0.1:8000/admin/functions/echo/aliases/current \
  -H 'Content-Type: application/json' \
  -d '{"alias":"current","weights":{"v1":0,"v2":100}}'
```

非法配置被明确拒绝且原因可区分（HTTP 响应体 `error_code`）：
`ALIAS_WEIGHTS_EMPTY`（一个版本都没配）、`INVALID_WEIGHT`（负数或非整数
权重）、`WEIGHT_SUM_INVALID`（合计 ≠ 100）、`VERSION_NOT_FOUND`（指向
未发布版本）。被拒绝的配置不会破坏现有路由。

兼容关系：经典 `{"alias":..., "version":"v2"}` 单版本写法等价于
`{"v2": 100}`，行为不变；直接按版本调用完全绕开别名权重，也不变。
分流只决定落点版本，落点之后各版本自己的并发上限与超时照旧生效；
同一 `request_id` 无论走别名还是直接按版本提交，仍收敛为同一结论。

## 在代码中直接使用

```python
from app import Gateway, Target, InvocationState

g = Gateway()
g.register_function("echo", "v1", lambda p: p,
                    timeout_seconds=2.0, max_concurrency=4)

record = g.invoke("echo", Target.version("v1"), {"x": 1},
                  request_id="req-001")
assert record.state is InvocationState.SUCCEEDED and record.result == {"x": 1}

# 同一 request_id + 同一参数 => 同一个结果对象（收敛）
again = g.invoke("echo", Target.version("v1"), {"x": 1}, request_id="req-001")
assert again is record
```

## 执行状态与拒绝码

执行终态（出现在调用记录的 `state` 字段）：

| state | 含义 | HTTP |
|---|---|---|
| `SUCCEEDED` | 处理器正常返回 | 200 |
| `FAILED` | 处理器抛异常（`error_code=HANDLER_ERROR`） | 502 |
| `TIMED_OUT` | 超出该版本的超时预算 | 504 |
| `THROTTLED` | 并发已满，立即拒绝而非排队 | 429 |

非终态：`ACCEPTED`（异步已受理）、`RUNNING`。

准入阶段拒绝（HTTP 响应体 `error_code`）：
`VERSION_ALREADY_EXISTS`(409)、`REQUEST_CONFLICT`(409)、
`FUNCTION_DISABLED`(409)、`FUNCTION_NOT_FOUND`/`VERSION_NOT_FOUND`/
`ALIAS_NOT_FOUND`(404)、`INVALID_REQUEST`(400)。

完整语义见 [`docs/protocol.md`](docs/protocol.md)。

## 验证方法

```bash
uv run pytest                      # 50 个单测
uv run pytest --log-cli-level=INFO # 实时打印 request_id 与状态迁移依据
```

关键场景与对应用例：

| 场景 | 用例 |
|---|---|
| 重复注册同一版本被拒绝、原版本不被篡改 | `tests/test_registry.py::test_reregister_same_version_rejected_distinctly` |
| 同一 request_id 并发重复提交只执行一次、结论收敛 | `tests/test_engine.py::test_concurrent_duplicate_ids_run_handler_once` |
| request_id 相同但参数不同 => `REQUEST_CONFLICT` | `test_same_id_different_params_is_conflict` |
| 并发上限：超额请求 `THROTTLED` 且不排队 | `test_concurrency_cap_throttles_without_queueing` |
| 超时：`TIMED_OUT` 且槽位立刻回收、迟到返回不改写 | `test_timeout_is_distinct_and_slot_reclaimed` |
| 别名切换期间在途调用固定旧版本，新调用走新版本 | `test_inflight_call_pinned_across_alias_switch` |
| 下线不影响在途结果，恢复后可继续调用 | `test_disable_does_not_lose_inflight_and_blocks_new` |
| 混合压力后并发计数零漂移 | `test_concurrency_counters_have_no_drift_after_mixed_load` |
| 加权别名按比例分发、在线改比例即时生效 | `tests/test_weighted_alias.py::test_weighted_split_matches_configured_proportions` |
| 非法权重配置以可区分错误码拒绝、不破坏现有路由 | `test_invalid_weight_configs_have_distinct_codes` / `test_rejected_config_leaves_previous_routing_untouched` |
| 改比例期间在途调用钉住旧版本、结果记账不串版本 | `test_inflight_call_pinned_and_attributed_across_reweight` |
| 权重降到 0 后该版本不再有新流量 | `test_zero_weight_version_receives_no_new_traffic` |
| 超时等异常结束下并发上限仍是真上限、计数零漂移 | `test_concurrency_ceiling_holds_through_timeouts` / `test_no_counter_drift_after_mixed_load` |
| 同一 request_id 走别名 / 直接按版本仍收敛、不可分叉 | `test_duplicate_id_converges_through_weighted_alias` / `test_same_id_via_alias_and_direct_version_cannot_fork` |
| HTTP 层：权重设置/查询、非法配置 400、分版本账本 | `tests/test_http_api.py::test_weighted_alias_split_over_http` 等 |

单测日志样例（每次迁移都带 request id 与 reason）：

```
invoke.admit request_id=req-race-1 ... resolved_version=v1 reason=first_submission
invoke.start request_id=req-race-1 inflight=1/8 reason=slot_acquired
invoke.dedupe request_id=req-race-1 reason=request_id_resubmitted
invoke.succeeded request_id=req-race-1 reason=handler_returned
invoke.duplicate request_id=req-race-1 state=SUCCEEDED reason=shared_request_id_converged
invoke.reclaim request_id=req-race-1 inflight=0 reason=execution_scope_closed
```
