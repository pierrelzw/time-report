# 会话记录分类：哪些是人工介入，哪些是 AI 自主

`Human Online` 指标的正确性完全取决于"哪些记录算真人动作"。这份文档把 Claude Code
session JSONL 里出现过的**每一种记录**逐条列出并标注归属，作为
`extract_timestamps()` / `_is_human_turn()` / `_human_side_channel()` 的判定依据。

分类基于对 316 个 JSONL 文件（`~/.claude/projects/*wechat*automation*/`，
2026-05 → 2026-07）的实测清点，不是根据文档推测。

## 判定原则

一条记录进入 `humanTimestamps` 的条件：**它的时间戳对应一次真人的键盘/鼠标动作**。

三个推论：

1. **人工动作同时也是活动。** 每个 human 时间戳必须同时进 `timestamps`，否则
   `Human Online` 可能超过 `Active Time`，`compute_metrics` 的 `total - human`
   会被 `max(..., 0)` 错误地夹到 0。
2. **没有 timestamp 的记录不可用。** 无论语义上是不是人工动作，缺时间戳就无法参与
   gap-threshold 计算（`mode` / `permission-mode` 属于这类）。
3. **别把 `away_summary` 当在场/缺席的 ground truth。** 名字像"人离开了"，实测是
   **固定计时器**：521 条全部落在"上一条 assistant 记录之后 3.1–4.1 分钟"
   （p10 3.1 / 中位 3.1 / p90 4.1）。触发门槛比 15min 阈值低一个量级，其中
   **36% 落在 ≤15min 的真人间隔内**（人根本没走）。它只说明"AI 停了、约 3 分钟
   没有人的输入"，既不能当正向信号，也不能用来校验阈值判断。

## A. 计入 human（人工介入行为）

| 记录                                                          | 数量   | 对应你的动作                                                 | 代码位置                         |
| ----------------------------------------------------------- | ---- | ------------------------------------------------------ | ---------------------------- |
| `user` + content 是非空字符串                                     | 2438 | 打字发 prompt、短确认（`好的`/`B`/`continue`）、slash 命令           | `_is_human_turn` 分支 2        |
| `user` + content 是 list 含 `text`                            | 287  | 带附件/图片的 prompt、Esc 打断（`[Request interrupted by user]`） | `_is_human_turn` 分支 3        |
| `user` + `tool_result` 且 `tool_use_id` 命中 `AskUserQuestion` | 398  | **AI 让你选择、你回车确认**                                      | `_answers_ask_user_question` |
| `queue-operation` / `operation=enqueue` 且带 content          | 971  | AI 干活时你排队打的 prompt                                     | `_human_side_channel`        |
| `attachment` / `type=queued_command`                        | 476  | 同上，新版 CLI 的第二种记录形状                                     | `_human_side_channel`        |
| `system` / `subtype=local_command` 且含 `<command-name>`      | 14   | 你运行的 slash 命令                                          | `_human_side_channel`        |
| `attachment` / `type=plan_mode_exit`                        | 3    | 你批准了一个 plan                                            | `_human_side_channel`        |

`queue-operation` 和 `queued_command` 是同一动作的两种记录形状，时间戳常常重合。
不去重是安全的：重复时间戳产生长度 0 的间隔，对总时长贡献 0 分钟。

## B. 不计入（AI 自主行为 / 系统注入）

| 记录                                                                                                                                          | 数量    | 归属         | 为什么不算                                                       |
| ------------------------------------------------------------------------------------------------------------------------------------------- | ----- | ---------- | ----------------------------------------------------------- |
| `assistant`                                                                                                                                 | 42165 | AI         | 模型输出与 tool_use。**进 Active Time，不进 human**                  |
| `user` + content 只含 `tool_result`（非 AskUserQuestion）                                                                                        | 18016 | AI         | harness 把工具输出回灌给模型                                          |
| `user` + `isMeta=True`                                                                                                                      | 555   | 系统         | skill 注入、Stop hook feedback、`<local-command-caveat>`、图片尺寸说明 |
| `attachment` / `task_reminder`                                                                                                              | 2489  | 系统         | 待办提醒注入                                                      |
| `attachment` / `hook_system_message` `hook_success` `hook_cancelled` `hook_additional_context`                                              | 1357  | 系统         | hook 产生                                                     |
| `attachment` / `nested_memory`                                                                                                              | 1054  | 系统         | CLAUDE.md / 记忆注入                                            |
| `attachment` / `edited_text_file`                                                                                                           | 676   | AI         | AI 编辑文件后的内容快照                                               |
| `attachment` / `deferred_tools_delta` `mcp_instructions_delta` `agent_listing_delta` `skill_listing` `command_permissions` `invoked_skills` | 1899  | 系统         | 能力清单注入                                                      |
| `attachment` / `goal_status` `date_change` `diagnostics` `compact_file_reference`                                                           | 301   | 系统         | 状态注入                                                        |
| `attachment` / `file`                                                                                                                       | 24    | 人工但**无增量** | @ 引用的文件；实测 24 条全部紧贴一条打字 prompt，新增锚点 0 个                     |
| `last-prompt`                                                                                                                               | 6036  | —          | **无 timestamp**，无法用于计时                                      |
| `mode`                                                                                                                                      | 5874  | 人工但不可用     | 切 model；**无 timestamp**，纯状态快照                               |
| `permission-mode`                                                                                                                           | 5764  | 人工但不可用     | 切权限模式；**无 timestamp**，纯状态快照                                 |
| `ai-title`                                                                                                                                  | 4547  | AI         | 自动生成的会话标题（用于 Title 列，不计时）                                   |
| `file-history-snapshot`                                                                                                                     | 3285  | AI         | 编辑前快照，无 timestamp                                           |
| `file-history-delta`                                                                                                                        | 114   | AI         | 编辑差异                                                        |
| `system` / `stop_hook_summary`                                                                                                              | 1608  | 系统         | Stop hook 执行汇总                                              |
| `system` / `turn_duration`                                                                                                                  | 1552  | AI + 等人的时间 | 一轮的墙钟耗时，但**混入了等你回应的时间**，不能用来反推人的时间（见 E 节）                   |
| `system` / `away_summary`                                                                                                                   | 521   | 计时器，非人工    | recap 功能。**不是**"人离开"的证据，是 AI 轮次结束后约 3min 无人输入的固定触发（见判定原则 3） |
| `system` / `api_error`                                                                                                                      | 130   | AI         | API 报错                                                      |
| `system` / `local_command` 的 stdout 那条                                                                                                      | \~49  | 系统         | 与 `<command-name>` 同一时刻的命令输出，去掉避免重复                         |
| `system` / `scheduled_task_fire`                                                                                                            | 11    | AI         | 定时任务自动触发，不是人点的                                              |
| `system` / `compact_boundary`                                                                                                               | 3     | 混合         | 无法区分 `/compact` 手动与自动压缩，量太小，忽略                              |
| `pr-link`                                                                                                                                   | 1026  | AI         | AI 创建 PR 的链接                                                |
| `relocated` `worktree-state` `agent-name` `custom-title` `agent-setting` `frame-link`                                                       | 435   | 其他         | 元数据，多数无 timestamp                                           |

## C. 记录了但无法计入的人工动作（已知盲区）

这三类是真人动作，但日志形状让它们无法参与计时。想覆盖需要改 Claude Code 的
写日志格式，不是这个脚本能解决的：

1. **点权限弹窗批准工具** — 没有逐次授权记录。`permission-mode` 只在你**切换模式**时
   写一条快照。后果：`default` 模式下连续点批准、中间不打字的时段在 Human Online 里
   是空白。`bypassPermissions` / `acceptEdits` 模式下不存在这个问题（你本来也不点）。
2. **切 model** (`mode`, 5874 条) — 有记录，无 timestamp。
3. **切 permission mode** (`permission-mode`, 5764 条) — 有记录，无 timestamp。

## D. 算法本身的两个偏差（与分类无关）

1. **每段工作丢尾巴（低估）。** `compute_active_time` 只累加相邻两个时间戳之差，
   最后一个时间戳之后不计时。你敲完最后一个 prompt、读完输出再关终端的那几分钟，
   从来不计。
2. **阈值内的间隔全额计入（可能高估）。** ≤15min 的间隔一律算全额，但那段时间你
   可能在别的项目或在开会。日志里没有信号能区分"在读输出"和"走开了 12 分钟"。

这两项都**无法用现有日志校验**。两个候选信号都已实测否决：

- `system/away_summary` — 不是缺席检测，是 AI 轮次结束后约 3 分钟的固定计时器
  （见判定原则 3）
- `system/turn_duration` — 混入了等你回应的时间，反推只会把误差挪个位置（见 E 节）

**日志内的候选到此穷尽。** 要真正测量在场时间，只能引入 session 之外的信号，例如
终端/编辑器的应用焦点时长——那是直接测量，而不是从键盘事件反推。

因此 Human Online 应当**始终按估计值引用**：报数字时带上阈值和误差带，不要精确到
分钟。Active Time 不同，它有 E 节的独立验证支撑。

**给后来者（也包括未来的我）**：`away_summary` 和 `turn_duration` 都因为名字听起来
正好能解决问题而被提出过，两次都是先给了乐观判断、后被实测推翻。再提任何新信号之前，
先按 E 节的方式验证语义，别信字段名。

## E. 否决记录：`system/turn_duration` 不能用来反推人的时间

**设想过的思路**：`turn_duration` 记录每轮的 `durationMs`，看起来能独立算出"AI 干活
的时间"，再用 `Active − AI自主` 反推人的时间——一条与 gap-threshold 完全不同的推导
路径，两者可以互相对照。

**实测三项，第二项否决了它**（1552 条记录，316 个 JSONL）：

### Q1 是墙钟时间，不是 CPU 时间 — 通过

`durationMs` 对比"该轮真人动作 → `turn_duration` 时刻"的墙钟跨度：
比值中位 **0.98**，差值中位 **0.03 min**（p10 0.77 / p90 4.80）。

p90 比值 4.80 是代理指标失效，不是记录有问题：那些样本里你中途插了排队 prompt，
真人动作落在轮次内部而不是起点，所以"上一次真人动作"不是真正的轮次起点。

### Q2 包含等你回应的时间 — **否决**

判定方法：检查 AskUserQuestion 的「提问→回答」等待区间是否落在 `durationMs`
覆盖的窗口 `[end − durationMs, end]` 内。

| 关系                         | 数量     |
| -------------------------- | ------ |
| 完全落在某个 `turn_duration` 窗口内 | **69** |
| 部分重叠                       | 123    |
| 完全在窗口外                     | 117    |

那 69 次被完整包住的等待合计 **22.8 小时**（中位 2.4min，p90 17.2min，最大 489min）。
**你在思考、AI 在空等的时间被计入了 `durationMs`。**

后果：`durationMs` 会高估"AI 自主"，用 `Active − AI自主` 反推出的人的时间就被低估，
而低估的量恰好是最难独立估计的那部分。用它反推等于把误差换个地方藏起来，不是消除。

### Q3 并行 session 会重复计 — 可修，但不重要了

各 session `durationMs` 直接相加 **194.2h**，合并重叠区间后 **166.9h**，
重复计 **1.16x**（248 个 session）。按时间区间合并即可修正，思路与
`active_minutes_by_day` 的按日 union 相同。因 Q2 已否决整条路线，未实现。

### 副产品：Active Time 得到独立验证

合并重叠后的 `turn_duration` 墙钟 **166.9h**，与 gap-threshold 算出的
Active Time **167.1h** 相差 **0.1%**。两者路径完全不同（一个来自 Claude Code
自记的每轮耗时，一个来自消息时间戳 + 15min 阈值），如此接近说明 **Active Time
可靠**。

但这个验证**只对 Active Time 有效，对 Human Online 无效**——`turn_duration` 不区分
"AI 在干活"和"AI 在等你"，它验的正是二者之和。

## 校准过的其他事实

- **AskUserQuestion 提问 → 回答间隔**（48 次配对）：中位 3.0min，p90 82min，
  最大 1272min。补丁只补锚点，不把超阈值的等待算成在场——间隔 >15min 时
  `gap <= thr` 判否，那段照样不计。
- **排队 prompt 的打字时刻 → 实际发送时刻延迟**（74 条配对）：中位 0.0min，
  p90 0.4min，最大 6.7min，无一超过 15min。所以用哪个时刻当锚点都不影响结果。

