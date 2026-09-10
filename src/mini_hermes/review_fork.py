"""Review fork(M5 学习闭环的心脏,Hermes background_review 精神)。

触发(E9):workflow phase 结束(passed);gate 失败/豁免立即触发。
执行:DaemonThreads 串行队列(每会话同刻至多一个 review),辅模型非流式,
fire-and-forget —— 永不阻塞 workflow,失败只记日志。

白名单(Q7):review 的产出只能经 apply_decision 落地 ——
create_skill(→probation/learned)/ update_skill / add_memory / none。
deprecation、任意文件写等在 review 路径上不可达。

证据流(probation 生命周期):gate 结算时按 watermark 增量关联
skill_usage ↔ gate 结果,累计 evidence;达阈值自动转正(promotion),
负面占优自动写 PENDING_DEPRECATION(demotion,留人审 —— 不变量 7)。
"""

from __future__ import annotations

import json
import logging
import queue
from pathlib import Path
from typing import Any, Callable, Optional

from .logging import log_event
from .skills_lib import SkillLibrary, read_memory_entries
from .state_db import SessionDB

_logger = logging.getLogger("mini_hermes")

REVIEW_PROMPT = """你是一个 agent 运行复盘员。刚发生的事件:{trigger}(会话 {session_id},phase {phase})。

## 最近对话(至多 {n_messages} 条,截断显示)
{messages}

## Gate 结果 JSON
{gate_results}

## REVIEW_REPORT.md(若存在)
{review_report}

## 当前 skill 索引
{skill_index}

## 你的任务
判断这次事件是否产出了值得沉淀的经验。只允许以下动作类型:
- create_skill:新建可复用技能(name 用 slug,description ≤60 字符,content 为完整正文)
- update_skill:更新已有 skill 的 body 或 evidence
- add_memory:向长期记忆追加一条 § 条目
- none:没有值得沉淀的

只输出 JSON(不要任何其他文字):
{{"actions": [{{"type": "...", "name": "...", "content": "...", "rationale": "...", "evidence_refs": ["..."]}}]}}
没有动作就输出 {{"actions": [{{"type": "none"}}]}}。
"""

_ALLOWED_ACTIONS = {"create_skill", "update_skill", "add_memory", "none"}


class DecisionParseError(ValueError):
    pass


def parse_decision(raw: str) -> list[dict]:
    """严格解析 LLM 决策 JSON;畸形 → DecisionParseError(调用方丢弃)。"""
    text = raw.strip()
    # 容忍 ```json 围栏
    if text.startswith("```"):
        text = text.strip("`").removeprefix("json").strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise DecisionParseError(f"决策不是 JSON:{e}") from e
    if not isinstance(data, dict) or not isinstance(data.get("actions"), list):
        raise DecisionParseError("决策缺 actions 列表")
    actions = []
    for i, action in enumerate(data["actions"]):
        if not isinstance(action, dict) or action.get("type") not in _ALLOWED_ACTIONS:
            raise DecisionParseError(f"actions[{i}] 类型非法:{action!r}")
        actions.append(action)
    return actions


def apply_decision(
    actions: list[dict],
    lib: SkillLibrary,
    data_dir: Path,
    logger: Optional[logging.Logger] = None,
) -> list[str]:
    """白名单 applier:只落地 create_skill/update_skill/add_memory/none。

    返回落地结果描述列表;越权动作(deprecate/写文件等)在类型层就不可达
    (parse_decision 已拒绝),这里再防御一层。
    """
    logger = logger or _logger
    applied: list[str] = []
    for action in actions:
        atype = action["type"]
        if atype == "none":
            continue
        rationale = str(action.get("rationale", ""))
        try:
            if atype == "create_skill":
                skill = lib.create(
                    str(action["name"]),
                    str(action.get("description") or str(action["name"]))[:60],
                    str(action.get("content") or ""),
                    status="probation",   # Q7:新创建进试用期
                    origin="learned",
                    commit_body=f"review rationale: {rationale}",
                )
                applied.append(f"create_skill:{skill.name}")
            elif atype == "update_skill":
                changes = {}
                if action.get("content"):
                    changes["body"] = str(action["content"])
                if isinstance(action.get("evidence_delta"), dict):
                    changes["evidence_delta"] = action["evidence_delta"]
                skill = lib.update(str(action["name"]),
                                   commit_body=f"review rationale: {rationale}",
                                   **changes)
                applied.append(f"update_skill:{skill.name}")
            elif atype == "add_memory":
                mem_dir = Path(data_dir) / "memories"
                mem_dir.mkdir(parents=True, exist_ok=True)
                with (mem_dir / "MEMORY.md").open("a", encoding="utf-8") as f:
                    f.write(f"§ {action.get('content', '')}\n")
                applied.append("add_memory")
            else:
                # 理论不可达(parse 已拦),防御
                log_event(logger, logging.WARNING, "review_action_rejected",
                          action_type=atype)
        except Exception as e:
            log_event(logger, logging.WARNING, "review_action_failed",
                      action_type=atype, error=str(e))
    return applied


# ---------------------------------------------------------------------------
# 证据流(gate 结算 → evidence → promotion/demotion)
# ---------------------------------------------------------------------------


def apply_gate_evidence(
    db: SessionDB,
    lib: SkillLibrary,
    session_id: str,
    phase_id: str,
    outcome: str,  # positive | negative
    watermark: int,
) -> tuple[int, list[str]]:
    """按 watermark 增量处理该 phase 的 skill_usage 行。

    返回 (新 watermark, 本次涉及的 skill 名列表)。同一 attempt 的 usage 行
    只计一次(幂等靠 watermark,不怕 resume 重放)。
    """
    rows = [
        r for r in db.get_skill_usage(session_id)
        if r["phase"] == phase_id and r["id"] > watermark
    ]
    if not rows:
        return watermark, []
    by_skill: dict[str, int] = {}
    for r in rows:
        by_skill[r["skill_name"]] = by_skill.get(r["skill_name"], 0) + 1
    for skill_name, n in by_skill.items():
        try:
            lib.update(skill_name,
                       evidence_delta={"uses": n, outcome: n},
                       commit_body=f"evidence from {session_id}/{phase_id} "
                                   f"gate outcome={outcome}")
        except KeyError:
            continue  # 库中已不存在的 skill,跳过
    return max(r["id"] for r in rows), list(by_skill)


def check_transitions(
    db: SessionDB,
    lib: SkillLibrary,
    skill_name: str,
    *,
    promotion_uses: int = 3,
    promotion_ratio: float = 0.6,
    logger: Optional[logging.Logger] = None,
) -> Optional[str]:
    """probation 状态机:达阈值自动转正;负面占优自动写 PENDING_DEPRECATION。

    返回发生的迁移("promoted"/"demotion_pending"/None)。
    """
    logger = logger or _logger
    try:
        skill = lib.load(skill_name)
    except KeyError:
        return None
    if skill.status != "probation":
        return None
    ev = skill.evidence
    pos, neg, uses = ev.get("positive", 0), ev.get("negative", 0), ev.get("uses", 0)
    if uses < promotion_uses:
        return None
    if neg > pos:
        db.meta_set(
            f"pending_deprecation:{skill_name}",
            json.dumps({"name": skill_name,
                        "reason": f"证据恶化:positive={pos} negative={neg} uses={uses}"},
                       ensure_ascii=False),
        )
        log_event(logger, logging.WARNING, "skill_event",
                  skill_name=skill_name, action="demotion_pending",
                  origin=skill.origin)
        return "demotion_pending"
    if pos + neg > 0 and pos / (pos + neg) >= promotion_ratio:
        lib.update(skill_name, status="active",
                   commit_body=f"auto-promote: positive={pos} negative={neg} "
                               f"uses={uses} ratio={pos / (pos + neg):.2f}")
        log_event(logger, logging.INFO, "skill_event",
                  skill_name=skill_name, action="promoted", origin=skill.origin)
        return "promoted"
    return None


# ---------------------------------------------------------------------------
# ReviewFork
# ---------------------------------------------------------------------------


class ReviewFork:
    """串行复盘队列。call_aux(prompt) -> str 由装配层提供(辅模型,走重试包装)。"""

    def __init__(
        self,
        db: SessionDB,
        lib: SkillLibrary,
        call_aux: Callable[[str], str],
        *,
        data_dir: Optional[Path] = None,   # 配置 data_dir(add_memory 落点)
        workdir: Optional[Path] = None,    # run 工作目录(REVIEW_REPORT 来源)
        max_messages: int = 30,
        logger: Optional[logging.Logger] = None,
    ):
        self.db = db
        self.lib = lib
        self.call_aux = call_aux
        self.data_dir = Path(data_dir) if data_dir else None
        self.workdir = Path(workdir) if workdir else None
        self.max_messages = max_messages
        self.logger = logger or _logger
        self._queue: queue.Queue = queue.Queue()

    def submit(
        self,
        trigger: str,
        session_id: str,
        *,
        phase_id: Optional[str] = None,
        gate_results: Optional[list[dict]] = None,
    ) -> None:
        """fire-and-forget 入队。trigger: phase_end | gate_failure | gate_waiver | manual。"""
        self._queue.put({
            "trigger": trigger, "session_id": session_id,
            "phase_id": phase_id, "gate_results": gate_results or [],
        })

    def register_daemon(self, daemons, interval_s: float = 0.5) -> None:
        daemons.add_loop("review_fork", self.tick, interval_s)

    def tick(self) -> None:
        """DaemonThreads 每拍调用:至多处理一件,异常绝不外抛(daemon 永不死)。"""
        try:
            item = self._queue.get_nowait()
        except queue.Empty:
            return
        try:
            self._process(item)
        except Exception as e:
            log_event(self.logger, logging.ERROR, "daemon_thread_error",
                      thread="review_fork", error=repr(e))

    def drain(self) -> None:
        """同步排空(CLI review 用)。"""
        while not self._queue.empty():
            self.tick()

    # -- 内部 -----------------------------------------------------------------

    def build_packet(self, item: dict) -> str:
        messages = self.db.get_messages(item["session_id"])[-self.max_messages:]
        msg_text = "\n".join(
            f"<{m['role']}> {(m.get('content') or '')[:300]}" for m in messages
        ) or "(空)"
        review_report = "(无)"
        if self.workdir:
            report = Path(self.workdir) / "REVIEW_REPORT.md"
            if report.is_file():
                review_report = report.read_text(encoding="utf-8")[:3000]
        return REVIEW_PROMPT.format(
            trigger=item["trigger"],
            session_id=item["session_id"],
            phase=item["phase_id"] or "-",
            n_messages=self.max_messages,
            messages=msg_text,
            gate_results=json.dumps(item["gate_results"], ensure_ascii=False, indent=2),
            review_report=review_report,
            skill_index=self.lib.build_index() or "(空库)",
        )

    def _process(self, item: dict) -> None:
        packet = self.build_packet(item)
        raw = self.call_aux(packet)
        try:
            actions = parse_decision(raw)
        except DecisionParseError as e:
            log_event(self.logger, logging.WARNING, "review_decision_dropped",
                      session_id=item["session_id"], error=str(e))
            return
        applied = apply_decision(actions, self.lib, self.data_dir or Path("."),
                                 logger=self.logger)
        log_event(self.logger, logging.INFO, "review_completed",
                  session_id=item["session_id"], trigger=item["trigger"],
                  phase=item["phase_id"], actions_applied=applied)


# ---------------------------------------------------------------------------
# Backfill(Q6 part 2):历史运行 → 低置信度 skill
# ---------------------------------------------------------------------------

DEFAULT_BACKFILL_SOURCES = [
    "/home/lenovo/.agents/skills/academic-paper-loop/test_discover_20260701_145142",
    "/home/lenovo/.agents/skills/loop-engineering",
]

_BACKFILL_PACKET = """以 下是一次历史论文 loop 运行的资料。用同一套复盘决策 JSON 格式
(create_skill/update_skill/add_memory/none)提炼可复用经验。

{materials}
"""


def run_backfill(
    lib: SkillLibrary,
    call_aux: Callable[[str], str],
    source_dirs: list[str],
    logger: Optional[logging.Logger] = None,
) -> list[str]:
    """对每个历史运行目录跑同一套复盘决策,skill 落 status/origin=backfilled。"""
    logger = logger or _logger
    created: list[str] = []
    for source in source_dirs:
        src = Path(source)
        if not src.is_dir():
            log_event(logger, logging.WARNING, "backfill_source_missing", source=source)
            continue
        materials = []
        for name in ("gate_1_report.json", "EXTENDED_REFERENCES.md",
                     "REVIEW_REPORT.md"):
            path = src / name
            if path.is_file():
                materials.append(f"### {name}\n{path.read_text(encoding='utf-8')[:4000]}")
        finals = sorted(src.glob("*FINAL*.md"))
        for path in finals[:1]:
            materials.append(f"### {path.name}\n{path.read_text(encoding='utf-8')[:4000]}")
        if not materials:
            continue
        raw = call_aux(_BACKFILL_PACKET.format(materials="\n\n".join(materials)))
        try:
            actions = parse_decision(raw)
        except DecisionParseError as e:
            log_event(logger, logging.WARNING, "review_decision_dropped",
                      error=str(e), source=source)
            continue
        for action in actions:
            if action["type"] != "create_skill":
                continue
            try:
                skill = lib.create(
                    str(action["name"]),
                    str(action.get("description") or str(action["name"]))[:60],
                    str(action.get("content") or ""),
                    status="backfilled",       # 低置信度起步(Q6)
                    origin="backfilled",
                    commit_body=f"backfill from {source}\n"
                                f"rationale: {action.get('rationale', '')}",
                )
                created.append(skill.name)
            except Exception as e:
                log_event(logger, logging.WARNING, "review_action_failed",
                          action_type="create_skill", error=str(e))
    return created
