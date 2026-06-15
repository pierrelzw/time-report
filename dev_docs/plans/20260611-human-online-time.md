# 开发计划:人在线时间统计 + Python/JS 双向校验 + 互动间隔直方图

> 日期:2026-06-11
> 作者:pierrelzw + Claude
> 涉及版本:3.4.0 → 3.5.0(本计划完成后 bump)
> 源 repo:`/Users/lizhiwei/codes/time-report`(master 分支)

---

## 0. 背景与动机

当前报告只有一个时间指标:**总 active time**(所有消息时间戳跑 gap-threshold)。
它衡量的是"项目被推进了多久",但**不区分是人在操作还是 AI 在自主跑**。

对于"基本让 AI 开发"的项目,用户想知道:
- **人在线时长** —— 我本人实际参与互动了多久
- **AI 自主时长** —— AI 在我没干预的情况下自己跑了多久(= 总 active − 人在线)

同时在调研中发现了一个**已存在的真 bug**(详见 §1),以及用户对**数字准确性**的强需求
(终端值和网页值"偶尔对不上"),因此本计划把"双向校验"作为一等公民。

---

## 1. 【先修】已发现的 bug:跨午夜会话分天不一致

### 现象
终端(Python)算出的总 active time 与网页(JS)算出的**系统性对不上**。

### 根因
两边把"一个会话的时间戳"归到"哪一天"的逻辑不同:

| | Python(`time-report.py` `print_summary`) | JS(`template.html` `preprocessDays`) |
|---|---|---|
| 分天方式 | 每条时间戳**各自**按其本地日期分桶(`fromtimestamp(t).date()`) | 整个会话按**第一条消息**的日期归入**单独一天**(`getLocalDateStr(sorted[0])`) |

后果:一个 23:30 → 次日 01:00 的跨午夜会话,在 JS 端只挂在前一天;
渲染时 `dayTs = s.timestamps.filter(t in dayBounds)` 把次日 01:00 段过滤掉,
而次日那天的 `day.sessions` 里**根本没有这个会话** → 次日段**彻底丢失**。

Python 代码注释(`time-report.py:614`)已坦白这个 bug 并**只修了 Python 自己**,JS 一直没同步。

### 量化(2026-06 真实数据)
```
A (Python 逐戳分天): 64.82 h
B (JS 按首戳分天):   56.19 h
偏差:               8.63 h   ← 网页系统性少算约 13%
跨午夜会话: 40 个,被 JS 丢弃的时间戳: 4819 条
```

### 修复
把 JS `preprocessDays` 的分天改成与 Python 一致:**按每条时间戳的本地日期分桶**,
同一会话可出现在它覆盖的多天里,每天只携带当天的时间戳。修完两边基线才一致,
后续 §4 的校验才有意义。

---

## 2. 数据层:收集"真人互动"时间戳

### 定义("真人互动" = 发消息 + 文字确认)
在 `extract_timestamps`(Claude Code)新增 `humanTimestamps`,判定规则:
- `type == "user"`
- **且** `message.content` 是字符串(纯文本输入 / 简短确认「好的」「continue」/ slash 命令)
- **且** `not record.get("isMeta")`(排除系统注入的 reminder)

排除项(实测确认):
- `tool_result`(content 是 list 且含 `tool_result` block)—— AI 调工具的回包,**不是人**
- `isMeta` 记录 —— 系统注入

### Codex(`extract_codex_timestamps`)
- `payload.role == "user"` 的消息时间戳(复用现有 summary 提取处的判定)
- 注意排除环境注入类 user 消息(如 `<environment_context>`),实现时抽样核对

### "纯点击批准"无法纳入(数据限制,需在 UI/文档注明)
实测:`permission-mode` / `mode` 记录**没有时间戳**,且多为 `bypassPermissions` 模式
(跳过逐次确认)。因此"没打字、纯点批准按钮"的确认在 transcript 里**无记录**,
无法统计。带文字的确认(占绝大多数)已被上面的规则涵盖。

### 数据流
`humanTimestamps` 与现有 `timestamps` 一并:
- 过滤到日期范围(同 `ts_in_range` 逻辑)
- 存入每个 session dict
- 经 `build_report_data` 注入网页 JSON

---

## 3. 四档统一切换 + 三指标 + 终端对比表

### 网页
- 把**连续滑块**改成 **4 个按钮:10 / 15 / 20 / 30 分钟**(默认 15)
- 顶部统计卡片显示三个指标,**随选中档位同步切换**:
  - 总 active time(所有时间戳,跨 session 按天 union)
  - **人在线**(humanTimestamps,跨 session 按天 union)
  - **AI 自主** = 总 active − 人在线
- 三者用**同一档位**,保证 `AI 自主 = 总 − 人在线` 恒成立

### 终端
新增「4 档 × 3 指标」对比表,例如:
```
Threshold   Total active   Human online   AI autonomous
   10 min        ...            ...            ...
   15 min        ...            ...            ...   (default)
   20 min        ...            ...            ...
   30 min        ...            ...            ...
```
现有 per-session 长表保留。

### 口径一致性(关键)
人在线**必须跨 session 按天合并**计算,不能按单 session:
- 单 session 真人互动间隔 p90 = 123 min(用户切到别的 session 干活了)
- 跨 session 合并后 p90 = 14.8 min(真实互动节奏)
按单 session 会把"切到别处工作"误判成"离线",严重失真。

---

## 4. Python ↔ JS 双向校验(准确性保障)

动机:用户已遇到"终端和网页对不上",且数字准确性至关重要。

机制:
1. Python 对 **4 档**各算出三个指标(总/人/AI),作为 `expected` 嵌入网页 JSON
2. JS 用自己的算法(同一份 `computeActiveSpans`,分别喂全部戳 / 真人戳)重算
3. JS 加载后**逐项比对** expected,容差取 ±1 分钟(整分钟显示)以内
4. **任何一项不符** → 页面顶部显示**醒目红色警告条**:
   `⚠ 校验失败:[指标] @ [档位]min — Python=X, JS=Y`

效果:把"悄悄漂移"变成"当场报错"。§1 那类 9 小时偏差以后第一时间暴露。

注意:Python 与 JS 是**两份独立实现**(语言不同,本就如此),校验是为了让两份
互相兜底,而非合并。两边的分天 / 阈值边界(`gap <= threshold`)/ 单戳会话
(len<2 → 0)/ 去重(已在 DB 层按 external_id 去重,两边共用去重后数据)必须对齐。

---

## 5. 真人互动间隔直方图(论证默认阈值 15min 合理)

### 目的
在网页中用证据说明"为什么默认 gap threshold = 15min 合理"。

### 数据
跨 session 按天合并的**真人互动间隔**(humanTimestamps 相邻 gap)。
**必须跨 session**(理由同 §3 口径)。

### X 轴分桶(6 桶,用户指定)
```
0-2 / 2-5 / 5-10 / 10-15 / 15-30 / >30  (分钟)
```

### 预期形态(基于 2026-06 数据,跨 session)
| 桶 | 占比 |
|---|---|
| 0-2 min | ~57% |
| 2-5 min | ~18% |
| 5-10 min | ~10% |
| 10-15 min | ~5% |
| 15-30 min | ~5% |
| >30 min | ~5% |
| **累计 ≤15min** | **~90%** |

### 可视化
- Y 轴:次数(柱状)
- **竖线 = 当前选中档位**(10/15/20/30),随档位左右移动
- 标注:"线左侧覆盖 X% 的真人互动" —— 默认 15min 时约 90%
- 结论文案:90% 的真人互动间隔 ≤ 15min,说明 15min 足以抓住"连续互动",
  只把真正的长时间离开判为离线 → 默认值合理

### 数据来源
Python 算好各桶计数 + 各档位累计覆盖率,嵌入 JSON;JS 负责绘制 + 阈值线随档位移动。
(直方图为分布展示,不参与 §4 的三指标校验。)

---

## 6. 验收标准

- [ ] §1 修复后,同一份数据终端总 active 与网页总 active **完全一致**(±1min)
- [ ] 三指标满足 `AI 自主 = 总 active − 人在线`(每档)
- [ ] `humanTimestamps` 正确排除 tool_result / isMeta(抽样核对 ≥1 个会话)
- [ ] Codex 会话的真人时间戳正确提取
- [ ] 网页四档按钮切换,三指标 + 直方图阈值线同步更新
- [ ] 故意制造 Python/JS 不一致(临时改一边)→ 红色警告条出现
- [ ] 直方图 6 桶分布与 Python 终端统计一致
- [ ] 终端 4 档对比表数值与网页对应档位一致

## 7. 测试方法

1. 用 `wechat_automation 2026-06` 真实数据跑,人工核对三指标与直方图
2. 构造一个跨午夜的小型 fixture,验证 §1 修复(两边一致)
3. 临时篡改 JS 算法引入偏差,确认校验红条触发(然后还原)

## 8. 发布

1. 在源 repo `/Users/lizhiwei/codes/time-report` 实现并自测
2. 同步改动到 plugin 缓存目录
   `/Users/lizhiwei/.claude/plugins/cache/pierrelzw/time-report/3.4.0/...`(用于本机即时生效)
3. bump 版本 3.4.0 → 3.5.0(`.claude-plugin/*.json`)
4. 同步更新 marketplace 仓库 `zhiwei_skills` 的 `marketplace.json` 版本号
5. commit + push(commit 说明"为什么"——双向校验源于实际遇到的 9h 漂移 bug)

---

## 附:关键文件与函数

| 文件 | 关键位置 |
|---|---|
| `skills/time-report/scripts/time-report.py` | `extract_timestamps`(232)、`extract_codex_timestamps`(374)、`compute_active_time`(586)、`print_summary`(598)、`build_report_data`(550)、dedup(847) |
| `skills/time-report/references/template.html` | `computeActiveSpans`(273)、`preprocessDays`(470,**§1 bug 点**)、`render`(516)、stats 注入(585)、阈值滑块/presets CSS(23-29) |
| `skills/time-report/SKILL.md` | 用法文档,需补充新指标说明 |
