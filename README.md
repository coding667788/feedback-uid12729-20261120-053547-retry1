# 本地联邦身份目录（Local Federated Identity Directory）

在 Windows 原生 Python 3.12、**仅标准库 + 内嵌 SQLite**、纯 CPU、完全离线条件下实现的联邦身份目录。
合成主体（subject）由同一租户下多个提供方（provider）身份经关联边连通而成，
维护主体编号、单调主体代数、登录会话担保与失效，并提供单事务持久化、崩溃重放与独立参考模型差分。

## 1. 运行方式

```bash
# 回放离线合成输入，产出结果文档
python -m solution --input cases.json --output result.json

# 每个请求派生独立 OS 进程执行（含崩溃自动重放）
python -m solution --input cases.json --output result.json --subprocess

# 自动化测试（41 个用例）
python -m unittest discover -s tests -v

# 重新生成可重放 cases.json（确定性，无随机源）
python support/gen_cases.py

# 生产引擎与独立小模型逐请求差分
python support/diff_oracle.py
```

所有输入在 `support/gen_cases.py` 内离线合成，确认（attestation）用 HMAC-SHA256 确定性签发，
无任何网络或外部密钥依赖。

## 2. 目录结构

```
solution/            生产包
  errors.py          稳定错误码、崩溃异常与退出码
  confirmations.py   提供方模拟确认的签发与事务内重验（HMAC-SHA256）
  graph.py           活动边连通分量（DFS）与提供方冲突检测（纯函数）
  store.py           SQLite 连接、DDL、WAL、崩溃注入点
  engine.py          单事务请求引擎：全部端点规则、主体代数、会话失效
  worker.py          单请求独立进程入口（一次请求一个进程）
  worker_batch.py    批量请求独立进程入口（并发分组用）
  runner.py          进程内/子进程/并发三种回放器
  cli.py / __main__.py  命令行
support/
  oracle.py          独立重建的小模型（不 import 任何 solution 代码）
  diff_oracle.py     引擎 ↔ oracle 逐请求差分器
  gen_cases.py       cases.json 合成器
  _child_bootstrap*.py  子进程 -I 隔离模式引导
tests/               41 个 unittest 用例
cases.json           8 个场景、68 个请求的可重放输入
result.json          回放结果（收据序列 + 每租户最终快照）
```

## 3. 核心规则

### 3.1 关联边与双方确认
每条关联边需要**两名提供方各自签发一份确认**，确认绑定：租户、提供方、身份、身份当前代数、
对方身份、请求编号、有效时间窗 `[not_before, not_after]`，并用租户内提供方密钥做 HMAC-SHA256。
事务内按当前时钟与身份代数重新校验，任一份缺失/重复/签名错/绑定错/代数过期/超窗即整笔拒绝。

### 3.2 合成主体与提供方冲突
活动关联边（`state='active'` 且当前时钟落在有效窗内）的连通分量即主体。
**一个分量内同一提供方至多出现一个身份**。新增边在写入前先做“模拟合并”：
若两端分属不同分量，检查合并后是否出现同提供方身份；冲突则整笔拒绝，
边/主体/会话任何变化都随事务回滚，**绝不留下半个新主体**。
两条边单独看都可能合法，但共同把同一提供方的两个身份拉入同一分量时仍被拒（场景 S2）。

### 3.3 主体编号与主体代数
- 主体公开编号 = 分量内**最小身份 ID**；
- 任何**成员集合变化**（合并、拆分、到期、续期重连）或**成员身份代数变化**（续发），
  都从租户单调序列 `gen_seq` 分配一个**新主体代数**；
- 编号相同但成员未变且无成员续发时保留当代数（冗余边、不改变连通性的撤回）；
- 旧代数**永不复用、永不复活**：即便分量后来变回原样，旧会话依旧失效（测试 `test_old_generation_never_revives`）。

### 3.4 会话担保与失效
登录会话担保一组**确切已确认身份集合** + 主体编号 + 当时主体代数。
下列任一情况在同一事务内失效相关会话（只失效、不复活）：

| 失效原因 | 触发条件 |
|---|---|
| `SUBJECT_NUMBER_GONE` | 主体编号在新视图中消失 |
| `SUBJECT_GEN_ADVANCED` | 编号仍在但主体代数推进 |
| `GUARANTEED_MEMBERS_CHANGED` | 担保成员集合与当前分量不一致 |

### 3.5 单事务、时钟与幂等重放
每个请求在一个 `BEGIN IMMEDIATE` 事务内完成：端点检查 → 模拟合并/拆分 → 分量重建与代数分配
→ 受影响会话失效 → 持久收据 → 提交。SQLite 写锁串行化并发提交。

- **租户时钟非递减**：`tick` 落后于租户时钟拒绝（`CLOCK_NOT_MONOTONIC`）；
- **幂等优先于时钟**：同 `(tenant, request_id)` 的重复请求（包括已清除边的旧请求）
  只重放历史收据，不重新执行副作用、不推进时钟；
- 成功与拒绝都写持久收据，拒绝在独立事务中落库（业务变更已随前一事务回滚）。

### 3.6 多租户隔离
不同租户可使用相同身份 ID、请求编号、主体编号；所有表以 `tenant_id` 作为主键前缀，状态互不干扰。

## 4. 输入 / 输出格式

### cases.json
```json
{
  "schema_version": 1,
  "scenarios": [
    {
      "id": "S1",
      "name": "...",
      "description": "...",
      "requests": [
        {"op": "register_tenant", "tenant": "tA", "request_id": "tA-r00", "tick": 1},
        {"op": "associate", "tenant": "tA", "request_id": "e12", "tick": 20,
         "identity_a": "i1", "identity_b": "i2",
         "not_before": 1, "not_after": 1000,
         "confirmations": [ {"tenant":"tA","provider":"p1", "...": "..."},
                            {"tenant":"tA","provider":"p2", "...": "..."} ]}
      ],
      "expected": { "文档化断言": "..." }
    }
  ]
}
```

### 操作（op）
`register_tenant`、`register_identity`、`associate`、`revoke`、`reissue`、`renew_edge`、`login`。
边状态机：`active →（撤回）revoked`；`active →（身份续发）stale →（按当代数重新确认）active`（新边新 ID，旧 stale 边保留可追溯）。

### result.json
每个场景给出 `receipts`（含 `status`、`replayed`、`crash_observed`、变更后 `snapshot`）
与 `final_snapshots`（身份代数、主体编号/代数/成员、分量、会话有效性、边状态）。

## 5. 错误码

| 错误码 | 含义 |
|---|---|
| `OK` | 成功 |
| `TENANT_EXISTS` / `UNKNOWN_TENANT` | 租户重复注册 / 租户不存在 |
| `IDENTITY_EXISTS` / `UNKNOWN_IDENTITY` | 身份重复注册 / 身份不存在 |
| `CLOCK_NOT_MONOTONIC` | 请求 tick 落后租户时钟 |
| `BAD_CONFIRMATION` | 确认缺失/重复/签名错/绑定错/结构非法 |
| `STALE_CONFIRMATION` | 确认签发于身份旧代数 |
| `CONFIRMATION_EXPIRED` / `CONFIRMATION_NOT_YET_VALID` | 确认超出有效时间窗 |
| `DUPLICATE_ACTIVE_EDGE` | 同一对身份已有活动边 |
| `PROVIDER_CONFLICT` | 合并后分量内含同一提供方两个身份 |
| `EDGE_NOT_FOUND` / `EDGE_REVOKED` / `EDGE_NOT_RENEWABLE` | 边不存在 / 已撤回 / 活动边不可续期 |
| `NO_SUBJECT` | 登录时找不到主体（防御性） |

## 6. 崩溃恢复（独立进程）

`worker.py` 一个进程只处理一个请求。请求带 `crash` 字段时在两个真实注入点终止：

- `before_commit`：分量更新已在事务内完成但**未 COMMIT** → 退出码 87，连接关闭即回滚，无边无收据；
  重启按 ID 重放是一次**全新执行**（`replayed=false`）。
- `after_commit`：事务**已 COMMIT** 但收据未回到调用方 → 退出码 88，边与收据均已落库；
  重启重放只**回历史收据**（`replayed=true`），不产生重复边。

`runner.run_subprocess` 捕获特殊退出码后自动清除 `crash` 字段并重试，
测试 `test_crash_recovery.py` 在真实派生进程上验证两种语义。

## 7. 独立小模型差分

`support/oracle.py` 用与生产引擎完全不同的结构（内存字典 + 递归 DFS + 独立代数计数，
HMAC 校验也在文件内独立重写，不 import 任何 `solution` 代码）实现同一套规则。
`support/diff_oracle.py` 在全部 8 个场景、68 个请求上逐请求断言：
状态码、重放标记、身份代数、主体编号/成员/代数、会话有效性与失效原因**逐项一致**。

## 8. 场景清单与实测结果

| 场景 | 现象 | 关键实测结果 |
|---|---|---|
| S1 | 合并→桥撤回拆分→重新合并 | 主体编号恒为最小 ID；代数单调递增；旧会话失效不复活 |
| S2 | 两个单独合法的合并共同形成提供方冲突 | 第二边 `PROVIDER_CONFLICT`，分量保持 `[i1,i2],[i3]`，无半成品 |
| S3 | 撤回桥接边但环上有备选路径 | 首次撤回仍全连通；二次撤回才拆成两个主体 |
| S4 | 身份续发 + 边续期代数链 | 旧代数确认 `STALE_CONFIRMATION`；当代数重确认后续期成功重连 |
| S5 | 边到期与新会话争用、旧请求重放 | 到期拆分分量；旧会话失效；重复旧请求只回历史收据 |
| S6 | 两类真实进程崩溃 | 87 回滚无副作用 / 88 仅重放回执，最终三分量连通 |
| S7 | 确认/续发/撤回多进程并发 | 4 张收据全持久；无重复活动边；同对身份竞争一胜一 `DUPLICATE_ACTIVE_EDGE` |
| S8 | 多租户同编号隔离 | 两租户各自独立主体代数、会话与收据行 |

### 实测命令输出（本机 Windows 11 + Python 3.12.8）

```
$ python -m solution --input cases.json --output result.json
wrote result.json: 8 scenarios, 68 receipts

$ python -m unittest discover -s tests
Ran 41 tests in 10.6s
OK

$ python support/diff_oracle.py
S1 12 requests match oracle
S2 7 requests match oracle
S3 12 requests match oracle
S4 9 requests match oracle
S5 8 requests match oracle
S6 6 requests match oracle
S7 4 requests match oracle
S8 10 requests match oracle
ALL DIFFS CLEAR
```

41 个测试覆盖：主体编号与合并（3）、提供方冲突与无半成品（3）、桥接拆分（3）、
会话担保/失效/不复活/到期（5）、确认签名/代数/时间窗/重放/时钟（9）、续发续期（4）、
图原语与多租户隔离（4）、独立模型差分（2）、真实进程崩溃恢复（3）、并发争用（2）、CLI（2，含子进程模式）。
