# 开发计划:Session 活动摘要（在 HTML 里直接看出"这个会话做了什么"）

> 日期:2026-06-11
> 作者:pierrelzw + Claude
> 涉及版本:3.5.0 → 3.6.0(假设 `20260611-human-online-time.md` 先落地;若本计划先做则 3.5.0)
> 源 repo:`/Users/lizhiwei/codes/time-report`(master 分支)

---

## 0. 背景与动机

当前报告里,一个 session 能看到的"做了什么"只有三样,且都不够用:

| 现有列 | 来源 | 问题 |
| --- | --- | --- |
| **Title** | `ai-title` 记录(Claude Code 用模型起的短标题),`time-report.py:264-267` | 真·LLM 概括,但很短;Codex 会话没有,会回退到 Summary |
| **Summary** | 第一条非 meta 用户消息原文截断 80 字,`time-report.py:285-291` | **不是概括**,是原话开头;且经常取到噪声(见 §1) |
| **Transcript 链接** | `--transcripts` 渲染的完整对话 | 信息全,但要逐个点进去翻,无法"扫一眼就知道" |

用户诉求:**在 HTML 报告里直接看出每个 session 干成了什么事**,不必点进 transcript。

核心思路:jsonl 里已经埋了大量"做了什么"的**硬信号**(改的文件、跑的命令、git commit、
TodoWrite 清单),现在脚本一个都没用。先**零成本、确定性**地把这些抽出来做成可展开面板;
真·一句话人话总结作为 opt-in 的 LLM 选项叠加在后面。

---

## 1. 【先修】Summary 取到噪声,不是用户真正干的事

### 现象

成本表里大量 Summary 显示成 `You are a code reviewer. DO NOT modify...`、`You are a plan
reviewer...`、`/clear clear`、`/exit exit`、`/goal goal ...`——这些不是"用户这次要做什么",
而是**子 agent 注入的 system prompt** 或 **slash 命令外壳**。

### 根因

`time-report.py:285` 只判断"第一条非 meta user 消息",没有跳过:

- 子 agent / reviewer 注入型 prompt(以 `You are a ...`、`DO NOT modify` 等开头的长指令)
- 纯 slash 命令外壳(`/clear`、`/exit`、`/goal goal`、`/grill:roast` 等——`/cmd cmd` 重复词模式)

### 修复

在选 summary 时增加跳过规则,取**第一条"像人话"的用户消息**:

- 跳过纯 slash 命令行(正则 `^/\S+`,尤其 `^/(\S+)\s+\1\b` 这种命令名重复的外壳)
- 跳过 reviewer/agent 注入(以 `You are a`、`You're reviewing`、`DO NOT modify` 开头)
- 都跳完仍没有 → 回退到现状(第一条 user 消息),保证不退化为空

注:这是**启发式**(用规则猜,不是 100% 准),目的是把信噪比拉高;实现时对
`wechat_automation 2026-05` 抽样核对至少 5 个会话确认有改善。

---

## 2. 数据层:从 jsonl 抽"活动信号"(无 LLM,确定性)

在 `extract_timestamps`(`time-report.py:232`)那个**已经逐行读 jsonl** 的循环里顺手收集。
所有信号都来自 assistant 消息里的 `tool_use` block 和 user 消息,无额外 IO。

### 2.1 要收集的信号(按价值排序)

| 信号 | 提取自 | 字段(注入 session dict) |
| --- | --- | --- |
| **TodoWrite 最终清单** | 最后一次 `TodoWrite` 工具调用的 `todos`,取 content 列表 | `todos: [{content, status}]` |
| **git commit 信息** | `Bash` 工具调用 `command` 里匹配 `git commit -m "..."` / `-m '...'` | `commits: [str]` |
| **改过的文件** | `Edit`/`Write`/`NotebookEdit` 工具调用的 `file_path`,去重 | `filesTouched: [str]` |
| **跑过的 slash 命令** | user 消息里 `^/\S+` 行 | `slashCommands: [str]` |
| **工具使用直方图** | 统计各 `tool_use.name` 次数 | `toolHistogram: {name: count}` |

实现要点:

- assistant 消息的 `message.content` 是 list,逐个 block 判 `block.get("type") == "tool_use"`,
  读 `block["name"]` 与 `block["input"]`。
- **TodoWrite 取最后一次**(任务推进中会写多次,最后一次最接近"最终做完了什么")。
- 所有列表设**上限**(如 files 最多 20、commits 最多 10),避免巨型 session 把 JSON 撑爆;
  截断时记录真实总数(如 `filesTouchedTotal`)以便 UI 显示 "+N more"。
- 全部信号**过滤到日期范围**口径与现有 `ts_in_range` 一致(用 block 所在消息的时间戳)。

### 2.2 Codex(`extract_codex_timestamps`,`time-report.py:374`)

Codex 的工具调用结构与 Claude Code 不同(`function_call` / `shell` 等),且字段不稳定:

- **第一版只做能稳妥拿到的**:从 `shell` / 命令类调用里正则抓 `git commit -m`(同 2.1 规则)。
- TodoWrite / Edit-Write 这类 Claude 专有结构,Codex 无对应 → 该会话相应字段留空。
- 实现时抽样核对 1~2 个真实 Codex 会话,确认不报错、不误抓;拿不准的信号**宁可不抽**。

### 2.3 注入网页

经 `build_report_data`(`time-report.py:550`)与 `sessions.append`(`time-report.py:904`)把上述
字段一并放进每个 session dict,随现有 JSON 注入模板。

---

## 3. 渲染:成本表每行可展开的"活动详情"面板

### 现状

- 成本表 JS:`costRows` 构建(`template.html:350-364`),行渲染(`template.html:397-403`),
  目前每行是 Title(可链到 transcript)+ Summary,**不可展开**。
- 甘特图那边已有"点击展开"模式(`.day-detail` / `.detail-table`,`template.html:757-788`),
  可复用这套交互与样式风格。

### 改动

1. **Title 列加展开触发**:每行 Title 前加一个小三角 ▸,点击展开/收起该行下方的详情面板
   (不影响 Title 仍可点链接到 transcript——三角与链接分开点)。
2. **详情面板内容**(有就显示,无则省略对应块):
   - **✅ 任务清单**:TodoWrite 最终 todos,按 status 标记(done / in-progress / pending)
   - **📦 提交**:commits 列表(commit message)
   - **✏️ 改动文件**:filesTouched(超上限显示 "+N more")
   - **🔧 工具**:toolHistogram 前几名(如 `Edit×12 Bash×8 Read×20`)
   - **⌨️ 命令**:slashCommands
3. 纯前端逐行展开(类似甘特图 `day-row.open`),无需翻页/请求。

### 取舍

- 详情面板是**事实罗列**(硬信号),不做二次概括——概括交给 §4 的 opt-in LLM。
- TodoWrite 清单本身就是 Claude 写的任务自述,通常已经是最好的"做了什么"speed-read。

---

## 4. 【可选,opt-in】`--ai-summary`:Haiku 生成一句话人话总结

当用户想要的是"用人话讲这个会话在干嘛"的 1~2 句话,只能上模型。作为**默认关闭**的增量功能:

- 输入:每个 session 的 transcript(`--transcripts` 已生成的 index.html 文本,或直接喂 jsonl
  的 user/assistant 文本)。
- 模型:**Haiku**(`claude-haiku-4-5`,便宜);prompt 要求输出 ≤30 字中文"本次成果"。
- **缓存到磁盘,幂等**(对齐 transcript 设计):`time-report-summaries/<session-id>.txt`,
  存在即复用、不重跑;可加 `--summaries-dir` 覆盖。
- 成本量级:129 个会话 × Haiku ≈ 几毛到一两元,可接受;**仅 `--ai-summary` 时才触发**。
- 渲染:把生成的一句话显示在 Title 下方或详情面板顶部,标注"AI 生成"。
- 依赖:需要可用的 Anthropic API key;无 key 时 `--ai-summary` 报清晰错误并跳过(不阻断主报告)。

本节与 §2/§3 **正交**:§2/§3 给硬事实(免费),本节给人话概括(opt-in 付费)。先做前者。

---

## 5. 验收标准

- [ ] §1:`wechat_automation 2026-05` 抽样 ≥5 个会话,Summary 不再取到 `You are a ...` /
      `/clear` / `/exit` 这类噪声(取到真正的用户首句)
- [ ] §2:assistant 消息里的 TodoWrite / Edit-Write / git commit 正确抽取(抽样 ≥1 会话逐项核对)
- [ ] §2:列表上限生效,超限显示真实总数(filesTouchedTotal 等)
- [ ] §2:Codex 会话只抓到稳妥的 commit,不报错、不误抓
- [ ] §3:成本表每行可点击展开详情面板;无信号的块自动省略;Title 链接仍可独立点击
- [ ] §3:展开/收起纯前端,不翻页、不丢分页/排序状态
- [ ] §4(若实现):`--ai-summary` 生成并缓存;二次运行复用缓存不重跑;无 API key 时优雅跳过
- [ ] 主报告在**不带** `--ai-summary` 时行为不变(零成本路径默认开启)

## 6. 测试方法

1. 用 `wechat_automation 2026-05` 真实数据跑 `--transcripts`,人工展开几行核对 todo/commit/文件
2. 挑一个已知"改了很多文件 + 有 commit"的会话,确认面板与 git 历史 / 实际改动吻合
3. 挑一个"纯讨论无改动"的会话,确认面板优雅显示(只剩工具直方图或全空)
4. 抽 1~2 个 Codex 会话,确认 §2.2 不报错
5. (若做 §4)对同一项目跑两次 `--ai-summary`,确认第二次命中缓存、无新增 API 调用

## 7. 发布

1. 在源 repo `/Users/lizhiwei/codes/time-report` 实现并自测
2. 同步改动到 plugin 缓存目录
   `/Users/lizhiwei/.claude/plugins/cache/pierrelzw/time-report/<ver>/...`(本机即时生效)
3. bump 版本(`.claude-plugin/*.json`)
4. 同步更新 marketplace 仓库 `zhiwei_skills` 的 `marketplace.json` 版本号
5. 更新 `SKILL.md`:说明成本表新增可展开活动详情,以及 `--ai-summary` 用法/成本提示
6. commit + push(commit 说明"为什么"——Summary 原本只取首句原话且常是注入噪声,
   无法表达会话成果;改用 jsonl 里已有的硬信号做活动摘要)

---

## 附:关键文件与函数

| 文件 | 关键位置 |
| --- | --- |
| `skills/time-report/scripts/time-report.py` | `extract_timestamps`(232,§1 修复 + §2 收集信号)、`extract_codex_timestamps`(374,§2.2)、session 选 summary(285-291)、`sessions.append`(904)、字段 title/summary(909-910)、`build_report_data`(550)、(§4 新增)transcript 生成参考 `generate_transcripts`(495) |
| `skills/time-report/references/template.html` | `costRows` 构建(350-364)、成本表行渲染(397-403)、可复用的展开样式 `.day-detail`/`.detail-table`(64-82, 757-788) |
| `skills/time-report/SKILL.md` | 用法文档,补充活动详情面板与 `--ai-summary` |

---

## 实施顺序建议

1. **§1 + §2 + §3 一起做**(零成本核心):修 Summary 噪声 + 抽硬信号 + 成本表可展开面板。
   这一步即可达成"在 HTML 里看出每个 session 做了什么"的主目标。
2. **§4 单独作为后续增量**:等核心稳定后,再加 `--ai-summary` 的 Haiku 一句话总结。
