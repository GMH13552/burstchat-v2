# burstchat v2 — 分层人格 AI 伴侣

基于 ex-skill 六层人格架构 + 调度器行为控制的重构版本。

## v2 核心改进

### 分层人格（参考 ex-skill）
```
Layer 0: 核心规则  ← 调度器强制，最高优先级
Layer 1: 身份      ← 基础设定
Layer 2: 表达风格  ← 口癖、emoji、句长
Layer 3: 情感模式  ← 怎么表达在乎/不满/道歉
Layer 4: 冲突边界  ← 冷战模式、和解信号
Layer 5: 雷区      ← 触发点、消失模式
Timing: 时间维度  ← 调度器管理
Correction: 用户纠正 ← 运行时覆盖
```

### 调度器行为控制
- **口癖抑制**：检测过去 10 条消息中重复 ≥3 次的词汇，自动注入禁止指令
- **时间节奏**：根据情绪状态动态调整碎片间隔、第一条回复延迟
- **情绪检测**：分析用户消息，调整 energy → temperature 联动
- **纠正系统**：用户说"不对"→ 写入 corrections.json，跨会话生效

### 时间维度（调度器管理）
| 参数 | 说明 |
|------|------|
| first_reply_gap | 收到消息后第一条回复的延迟 |
| fragment_delay | 同思绪碎片之间的间隔 |
| thought_delay | 切换话题碎片之间的间隔 |
| active_interval | 对方沉默时主动发起消息的间隔 |

## 运行
```bash
pip install -r requirements.txt
python main.py
```

## 与原版对比
| | v1 | v2 |
|------|----|----|
| Persona | 平铺 JSON | 六层 + 时间 |
| 行为决策 | LLM 自己判断 | 调度器 + LLM |
| 口癖控制 | Prompt 建议 | 代码检测 + 注入禁止 |
| 时间节奏 | LLM 自行决定 t | 调度器指定延迟范围 |
| 用户纠正 | 无 | corrections.json 持久化 |
