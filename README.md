# 本地无服务器函数调用平台（Serverless Function Gateway）

一个可在本地运行的函数调用网关：把本地函数按 **名称 + 版本** 注册为不可变的可调用单元，
提供同步 / 异步调用、请求标识幂等去重、按版本的并发上限与执行超时、函数启停与别名切换。
所有拒绝与结束都有**可区分、可复现的状态与原因码**，并在日志中打印请求标识与状态迁移依据。

## 1. 快速开始

```bash
uv sync
uv run uvicorn main:app --reload        # 启动 HTTP 服务
uv run pytest                            # 运行全部测试（日志含请求标识与状态迁移）
```

也可以直接使用 Python API（可注册任意可调用对象）：

```python
from gateway.invocation import Gateway

g = Gateway()
g.registry.register("add", "1.0.0", lambda p: p["a"] + p["b"],
                    timeout_seconds=2.0, concurrency_limit=4)

r = g.invoke_sync("add", "1.0.0", {"a": 1, "b": 2}, request_id="req-001")
print(r.state.value, r.payload)          # succeeded 3
print(r.transitions)                     # 状态迁移依据（含相对耗时）

# 异步
h = g.invoke_async("add", "1.0.0", {"a": 1, "b": 2}, request_id="req-002")
g.query("req-002")                        # running -> succeeded
```

## 2. 调用协议（HTTP）

| 方法 | 路径 | 说明 |
| ---- | ---- | ---- |
| POST | `/functions/{name}/versions/{version}` | 注册不可变版本，body：`builtin`、`timeout_seconds`、`concurrency_limit` |
| PUT  | `/functions/{name}/aliases/{alias}` | 别名指向已发布版本，body：`{"version": "v1"}` |
| POST | `/functions/{name}/disable` `/enable` | 停用 / 启用函数 |
| POST | `/invoke/{name}/{version_or_alias}` | 同步调用，body：`{"payload": ..., "request_id": "x"}` |
| POST | `/invoke-async/{name}/{version_or_alias}` | 异步调用，立即返回 `running` |
| GET  | `/requests/{request_id}` | 查询请求结论 |
| GET  | `/builtins` `/health` | 内置函数清单 / 健康检查 |

HTTP 注册的函数从内置类型选择：`echo`、`add`、`sleep_echo`、`error`；
任意自定义 Python 函数请使用 Python API `registry.register(...)` 注册。

调用与查询响应统一为：

```json
{
  "request_id": "req-001",
  "name": "add",
  "target": "1.0.0",
  "via_alias": null,
  "state": "succeeded",
  "payload": 3,
  "started_at": 0.0,
  "finished_at": 0.0,
  "transitions": ["0.0000s state=RUNNING 依据：...", "0.0002s state=SUCCEEDED 依据：..."]
}
```

注册 / 解析类错误：HTTP `409`（冲突）或 `404`（不存在），`detail.code` 为稳定原因码，
例如 `version_already_exists`。

## 3. 版本与别名语义

- **版本不可变**：`(name, version)` 首次注册后，函数体、超时、并发上限均不可修改；
  再次注册同一版本直接拒绝，原因码 `version_already_exists`，旧对象绝不被覆盖。
- **未发布不可调用**：调用一个从未注册的版本 → 原因码 `version_not_found`；
  函数名不存在 → `function_not_found`；别名不存在 → `alias_not_found`。三者可区分。
- **别名可切换，版本不可改**：别名（如 `prod`）只是“名字 → 已发布版本”的指针，可反复切换；
  切换只对**之后的新请求**生效。
- **在途调用固定版本**：请求在开始执行前解析一次别名并拿到不可变版本对象；执行期间
  发生别名切换、下线、新版本发布，都不会影响该调用——结果不会丢失，也不会串入其它版本。
  响应中的 `target` 是实际执行的版本，`via_alias` 记录是否经由别名。

## 4. 并发与超时规则

并发计数与超时判定以 **(函数名, 版本)** 为粒度：

- **并发上限不排队**：在途数达到 `concurrency_limit` 时，新请求**立即**结束为
  `concurrency_rejected`（不会静默排队、不占用槽位）。测试中该路径在毫秒级返回。
- **超时不拖垮服务**：用户函数在独立守护线程运行，主线程等待至多 `timeout_seconds`；
  超时后调用立即结束为 `timed_out` 并把结论交还给调用方（同步不无限阻塞、异步可查询）。
- **槽位精确回收、计数不漂移**：正常结束时槽位释放一次；超时情况下，槽位要等那个
  “已超时但仍在运行”的函数**真正返回后**才由回收线程释放——避免把槽位提前放出导致超卖，
  也保证计数永不为负。
- **版本之间隔离**：v1 占满不影响 v2，各自独立计数。

### 执行状态（`state`）

| 状态 | 含义 |
| ---- | ---- |
| `running` | 已受理 / 执行中（中间态，异步可查询） |
| `succeeded` | 用户函数正常返回，结果在 `payload` |
| `failed` | 用户函数自身抛出异常，`payload` 为异常类型与信息 |
| `timed_out` | 超过 `timeout_seconds` |
| `concurrency_rejected` | 并发已满，立即拒绝 |
| `rejected_disabled` | 函数处于停用状态 |
| `rejected_no_version` | 函数 / 版本 / 别名不存在（`payload` 内含细分原因码） |

## 5. 请求标识幂等（结论收敛）

- 同一 `request_id` 在进程内**只执行一次**；并发重复提交时只有一个提交者真正执行，
  其余等待或复用同一个结果对象。
- 无论同步还是异步、无论重复多少次、即使重复时携带的 `payload` 不同，
  最终结论（状态、`target`、`payload`）**完全一致**。
- 同步重复提交会阻塞到首个执行终态后返回同一结论；异步重复提交直接复用同一执行句柄。
- 查询一个从未提交的 `request_id` → `unknown_request`。

## 6. 生命周期（启停 / 切换 / 下线）

- 停用函数后，**新请求**在解析阶段即被拒绝为 `rejected_disabled`；重新启用后恢复。
- 停用与别名切换都不会触碰已经在途的调用；在途调用继续执行其绑定版本并正常产出结果。
- 执行结束（成功 / 失败 / 超时 / 被拒）后并发计数一律回收，运行时状态可查询，不出现漂移。

## 7. 验证方法

```bash
uv run pytest          # 26 个用例，覆盖以下场景，日志打印 [req=...] 与状态迁移依据
```

| 测试文件 | 覆盖场景 |
| -------- | -------- |
| `tests/test_registry.py` | 重复注册冲突、未发布版本、未知别名、别名切换、启停 |
| `tests/test_executor.py` | 成功/失败、并发上限立即拒绝、超时与延迟槽位回收、版本隔离 |
| `tests/test_gateway.py` | 请求标识幂等收敛（8 并发重复）、异步查询、下线/别名切换期间在途不串版本 |
| `tests/test_api.py` | HTTP 协议端到端：冲突码、同步/异步、去重、超时、别名生命周期 |

手工验证（启动服务后）：

```bash
curl -s -X POST localhost:8000/functions/add/versions/v1 \
  -H 'content-type: application/json' \
  -d '{"builtin":"add","concurrency_limit":1,"timeout_seconds":1}'
curl -s -X POST localhost:8000/invoke/add/v1 \
  -H 'content-type: application/json' -d '{"request_id":"r1","payload":{"a":1,"b":2}}'
# 再次用 r1 提交（即使载荷不同）会得到完全相同的结论
```

## 8. 代码结构

```
gateway/
  errors.py      # 可区分原因码（异常类型 <-> code）
  models.py      # 状态枚举、不可变版本、函数记录、调用结果
  registry.py    # 版本注册（不可变）、别名、启停、解析
  executor.py    # 并发槽位、超时执行、槽位精确回收
  invocation.py  # 请求标识去重、同步/异步门面、结果查询、拒绝映射
  builtins.py    # HTTP 可注册的内置函数
  api.py         # FastAPI 路由与 HTTP 错误映射
main.py          # 应用入口
tests/           # 并发 / 超时 / 去重 / 版本冲突测试
```
