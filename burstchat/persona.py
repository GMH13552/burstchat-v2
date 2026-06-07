"""
分层 Persona 加载器（参考 ex-skill 六层结构）
支持 Corrections 层覆盖
"""

import json
import os
from typing import Optional


def _here() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def _personas_dir() -> str:
    return os.path.join(_here(), "..", "personas")


class LayeredPersona:
    """六层 + 时间维度的结构化人格定义

    层级优先级（从高到低）：
      Corrections > Layer 0 (Core Rules) > Layer 5 (Triggers) > Layer 4 (Conflict)
      > Layer 3 (Emotional) > Layer 2 (Expression) > Layer 1 (Identity)
    """

    def __init__(self, path: str):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)

        self.name = data["name"]
        self.description = data.get("description", "")

        # 六层结构
        self.layer_0 = data.get("layer_0_core_rules", {})
        self.layer_1 = data.get("layer_1_identity", {})
        self.layer_2 = data.get("layer_2_expression", {})
        self.layer_3 = data.get("layer_3_emotional", {})
        self.layer_4 = data.get("layer_4_conflict", {})
        self.layer_5 = data.get("layer_5_triggers", {})

        # 时间维度（调度器管理）
        self.timing = data.get("timing", {})

        # 兼容旧字段
        self.example_bursts = data.get("example_bursts", [])
        self.rules = data.get("rules", [])

        # Corrections（运行时覆盖）
        self._corrections: list[dict] = []
        self._load_corrections()

    # ── Corrections ─────────────────────────────────────────

    def _corrections_path(self) -> str:
        return os.path.join(_personas_dir(), "corrections.json")

    def _load_corrections(self):
        path = self._corrections_path()
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            self._corrections = data.get("items", [])

    def save_corrections(self):
        path = self._corrections_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"items": self._corrections}, f, ensure_ascii=False, indent=2)

    def add_correction(self, scene: str, wrong: str, correct: str):
        from datetime import datetime
        self._corrections.append({
            "scene": scene,
            "wrong": wrong,
            "correct": correct,
            "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
        })
        # 超过 50 条时合并归纳
        if len(self._corrections) > 50:
            self._compact_corrections()
        self.save_corrections()

    def get_correction_rules(self) -> list[str]:
        """返回所有 correction 规则，供注入 prompt 使用"""
        rules = []
        for c in self._corrections:
            rules.append(f"当 {c['scene']} 时：{c['correct']}（❌ 不要：{c['wrong']}）")
        return rules

    def _compact_corrections(self):
        """超过 50 条时合并相似场景的 corrections"""
        # 简单策略：保留最近 40 条
        self._corrections = self._corrections[-40:]

    # ── Prompt 构建 ─────────────────────────────────────────

    def build_layer_0_prompt(self) -> str:
        """最高优先级核心规则"""
        rules = self.layer_0.get("rules", [])
        if not rules:
            return ""
        lines = ["## 核心行为规则（最高优先级，必须遵守）"]
        for r in rules:
            lines.append(f"- {r}")
        return "\n".join(lines)

    def build_layer_1_prompt(self) -> str:
        """身份描述"""
        p = self.layer_1
        lines = [f'你是"{self.name}"，{p.get("age","")}岁，{p.get("job","")}。']
        if p.get("pet"):
            lines.append(f'养了一只{p["pet"]}。')
        for t in p.get("traits", []):
            lines.append(f"- {t}")
        return "\n".join(lines)

    def build_layer_2_prompt(self) -> str:
        """表达风格"""
        e = self.layer_2
        lines = ["## 说话风格"]
        lines.append(f"- 每条消息不超过{e.get('max_chars_per_msg', 12)}字")
        if e.get("no_period"):
            lines.append("- 不用句号，口语碎片")
        if e.get("casual_typos"):
            lines.append("- 口语化，偶尔带轻微错别字")
        if e.get("catchphrases"):
            ct_str = " ".join(e["catchphrases"])
            lines.append(f"- 常用口头禅/词汇：{ct_str}")
        if e.get("emoji"):
            emoji_str = " ".join(e["emoji"])
            lines.append(f"- 偶尔用颜文字（{emoji_str}）")
        for note in e.get("style_notes", []):
            lines.append(f"- {note}")
        return "\n".join(lines)

    def build_layer_3_prompt(self) -> str:
        """情感行为模式"""
        em = self.layer_3
        if not em:
            return ""
        lines = ["## 情感表达"]
        if em.get("express_care"):
            lines.append(f"- 表达在乎：{em['express_care']}")
        if em.get("express_upset"):
            lines.append(f"- 表达不满：{em['express_upset']}")
        if em.get("apology_style"):
            lines.append(f"- 道歉风格：{em['apology_style']}")
        if em.get("affection_style"):
            lines.append(f"- 说\"喜欢\"：{em['affection_style']}")
        return "\n".join(lines) if len(lines) > 1 else ""

    def build_layer_4_prompt(self) -> str:
        """冲突与边界"""
        cf = self.layer_4
        if not cf:
            return ""
        lines = ["## 冲突与边界"]
        if cf.get("conflict_chain"):
            lines.append(f"- 冲突模式：{cf['conflict_chain']}")
        if cf.get("reconciliation_signal"):
            lines.append(f"- 和解信号：{cf['reconciliation_signal']}")
        return "\n".join(lines) if len(lines) > 1 else ""

    def build_timing_prompt(self, behavior_plan) -> str:
        """时间约束 prompt（从调度器注入，非 persona 固有）"""
        bp = behavior_plan
        lines = ["## 本次时间约束（调度器指定）"]
        lines.append(f"- 回复模式：{bp.burst_mode}")
        lines.append(f"- 最多{bp.max_fragments}条消息")
        lines.append(f"- 同思绪碎片间隔 {bp.fragment_delay_range[0]}-{bp.fragment_delay_range[1]}s")
        lines.append(f"- 切换话题碎片间隔 {bp.thought_delay_range[0]}-{bp.thought_delay_range[1]}s")
        lines.append(f"- 第一条消息延迟约 {bp.first_reply_delay}s")
        lines.append(f"- 情绪基调：{bp.energy}")
        if bp.suppressed_tics:
            lines.append(f"- ⚠️ 本次禁止使用以下口头禅：{', '.join(bp.suppressed_tics)}")
        return "\n".join(lines)

    def build_correction_prompt(self) -> str:
        """Correction 层 prompt"""
        cr = self.get_correction_rules()
        if not cr:
            return ""
        lines = ["## 纠正记录（用户反馈，优先级高于上述规则）"]
        for r in cr[-10:]:  # 最近10条
            lines.append(f"- {r}")
        return "\n".join(lines)

    def build_system_prompt(self, behavior_plan=None, is_replan: bool = False,
                            search_context: str = "") -> str:
        """从分层 persona 编译完整 system prompt"""
        from datetime import datetime

        blocks = [
            f'你是"{self.name}"，一个{self.description}。',
            f"今天是{datetime.now().strftime('%Y年%m月%d日')}。",
            f"你和用户通过文字聊天，关系很近，聊天风格像老朋友发微信。",
        ]

        # Layer 1: 身份
        blocks.append("")
        blocks.append(self.build_layer_1_prompt())

        # Layer 0: 核心规则（最高优先级）
        blocks.append("")
        blocks.append(self.build_layer_0_prompt())

        # Layer 2: 表达风格
        blocks.append("")
        blocks.append(self.build_layer_2_prompt())

        # Layer 3: 情感行为
        l3 = self.build_layer_3_prompt()
        if l3:
            blocks.append("")
            blocks.append(l3)

        # Layer 4: 冲突边界
        l4 = self.build_layer_4_prompt()
        if l4:
            blocks.append("")
            blocks.append(l4)

        # 时间约束（调度器注入）
        if behavior_plan:
            blocks.append("")
            blocks.append(self.build_timing_prompt(behavior_plan))

        # Corrections
        corr = self.build_correction_prompt()
        if corr:
            blocks.append("")
            blocks.append(corr)

        # 示例
        blocks.append("")
        blocks.append(self._build_examples_block())

        # 输出格式
        blocks.append("")
        blocks.append(self._build_format_block())

        # 联网搜索（如果有）
        if search_context:
            blocks.append("")
            blocks.append(search_context)

        # Replan hint
        if is_replan:
            blocks.append("")
            blocks.append("⚠️ 注意：用户刚才在你说话时插话了。请优先回应用户的最新消息。")

        prompt = "\n".join(blocks)
        return prompt

    def _build_examples_block(self) -> str:
        if not self.example_bursts:
            return ""
        blocks = []
        for i, burst in enumerate(self.example_bursts):
            user_msgs = "\n".join(f"用户: {m}" for m in burst["input"])
            import json
            output = json.dumps(
                {"search": "", "messages": burst["output"]},
                ensure_ascii=False, indent=2,
            )
            blocks.append(f"{user_msgs}\n\n你:\n{output}")
        return "## 回复示例\n\n" + "\n\n".join(blocks)

    def _build_format_block(self) -> str:
        rules_block = "\n".join(f"- {r}" for r in self.rules)
        return f"""## 输出格式（铁律）
只输出 JSON 对象：
{{{{"search": "有需要查证的填搜索词，没有就 \\"\\"", "messages": [...]}}}}
- `t` = 和上一条消息之间的间隔秒数（第一条是距离现在的间隔）
- 同句碎片间隔 3-4s，换话题间隔 6-9s
- 长文本（15-20字）加 3-5s，超长（20字+）起步 10s
- `search` 是必填字段，日常闲聊写空字符串 ""

## 联网搜索
需要查证某件事时，`search` 填上搜索词，系统搜完把结果给你。

## 重要
{rules_block}"""


def load_persona(name: str = "xiaoye") -> LayeredPersona:
    """从 personas/ 目录加载分层 persona"""
    path = os.path.join(_personas_dir(), f"{name}.json")
    if not os.path.exists(path):
        raise FileNotFoundError(f"Persona 文件不存在: {path}")
    return LayeredPersona(path)
