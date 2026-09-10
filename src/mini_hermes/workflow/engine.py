"""Workflow 引擎(E10):YAML 定义 phase 序列,编排状态全走落盘链。

状态机(每 phase):running → contracted(产出契约已验) → passed
run 状态:IN_PROGRESS / PAUSED_GATE_BLOCK / PAUSED_USER_DECISION /
         PAUSED_ERROR / COMPLETED / ABORTED

写序(混沌安全):
1. phase 开始前:update run(current_phase, states[phase]=running)
2. 契约验证后:states[phase]=contracted(先于 gate)
3. gate 通过/豁免后:insert checkpoint + states[phase]=passed + current_phase=next
kill -9 后 resume:passed 跳过;contracted 只重跑 gate;running 重跑该 phase。
"""

from __future__ import annotations

import json
import logging
import string
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

from ..barrier import TestBarrier
from ..logging import log_event
from ..runtime import AgentRuntime
from ..state_db import SessionDB
from ..tools.registry import Registry, ToolContext
from .definition import GateDef, PhaseDef, WorkflowDef

_logger = logging.getLogger("mini_hermes")

MAX_PHASE_RETRIES = 2

# run.status 取值
IN_PROGRESS = "IN_PROGRESS"
PAUSED_GATE_BLOCK = "PAUSED_GATE_BLOCK"
PAUSED_USER_DECISION = "PAUSED_USER_DECISION"
PAUSED_ERROR = "PAUSED_ERROR"
COMPLETED = "COMPLETED"
ABORTED = "ABORTED"

# phase state 取值
S_RUNNING = "running"
S_CONTRACTED = "contracted"
S_PASSED = "passed"


class WorkflowError(RuntimeError):
    pass


class WorkflowEngine:
    def __init__(
        self,
        db: SessionDB,
        definition: WorkflowDef,
        definition_dir: Path,
        runtime: AgentRuntime,
        base_registry: Registry,
        run_id: str,
        workdir: Path,
        task: dict,
        *,
        waiver_decider: Optional[Callable[[str, dict], Optional[str]]] = None,
        gate_args_extra: Optional[dict] = None,
        barrier: Optional[TestBarrier] = None,
        review_fork: Any = None,          # M5b:ReviewFork,可选
        skill_lib: Any = None,            # M5b:SkillLibrary,证据流用,可选
        learning: Optional[dict] = None,  # {promotion_uses, promotion_ratio}
    ):
        self.db = db
        self.definition = definition
        self.definition_dir = Path(definition_dir)
        self.runtime = runtime
        self.base_registry = base_registry
        self.run_id = run_id
        self.workdir = Path(workdir)
        self.task = task
        self.waiver_decider = waiver_decider
        self.gate_args_extra = gate_args_extra or {}
        self.barrier = barrier or TestBarrier.from_env()
        self.review_fork = review_fork
        self.skill_lib = skill_lib
        self.learning = learning or {}
        self.logger = _logger

    # -- 构造 / 恢复 -----------------------------------------------------------

    @classmethod
    def start(
        cls,
        db: SessionDB,
        definition: WorkflowDef,
        definition_dir: Path,
        runtime: AgentRuntime,
        base_registry: Registry,
        workdir: Path,
        task: dict,
        run_id: Optional[str] = None,
        **kw,
    ) -> "WorkflowEngine":
        run_id = run_id or uuid.uuid4().hex[:12]
        workdir = Path(workdir)
        workdir.mkdir(parents=True, exist_ok=True)
        db.create_workflow_run(
            run_id,
            session_id=runtime.session_id,
            workflow_name=definition.name,
            task=task,
            workdir=str(workdir),
        )
        log_event(_logger, logging.INFO, "workflow_start",
                  run_id=run_id, workflow=definition.name)
        return cls(db, definition, definition_dir, runtime, base_registry,
                   run_id, workdir, task, **kw)

    @classmethod
    def resume(
        cls,
        db: SessionDB,
        run_id: str,
        definition: WorkflowDef,
        definition_dir: Path,
        runtime: AgentRuntime,
        base_registry: Registry,
        **kw,
    ) -> "WorkflowEngine":
        row = db.get_workflow_run(run_id)
        if row is None:
            raise WorkflowError(f"run 不存在:{run_id}")
        if row["status"] in (COMPLETED, ABORTED):
            raise WorkflowError(f"run {run_id} 已终态({row['status']}),不可 resume")
        runtime.resume()  # 水合 transcript(同一 session)
        if runtime.needs_continuation:
            runtime.continue_run()  # 先收尾被杀死的半个 turn
        log_event(_logger, logging.INFO, "workflow_resume",
                  run_id=run_id, current_phase=row["current_phase"],
                  status=row["status"])
        return cls(db, definition, definition_dir, runtime, base_registry,
                   run_id, Path(row["workdir"]), row["task"], **kw)

    # -- 主循环 -----------------------------------------------------------------

    def run(self) -> str:
        """推进到终态,返回最终 run.status。"""
        row = self.db.get_workflow_run(self.run_id)
        states: dict = row["phase_states"]
        phases = self.definition.phases
        targets = self._merged_targets()

        for index, phase in enumerate(phases):
            if states.get(phase.id) == S_PASSED:
                continue
            next_phase = phases[index + 1].id if index + 1 < len(phases) else None

            # -- 阶段执行(contracted 跳过,只补 gate)-------------------------
            if states.get(phase.id) != S_CONTRACTED:
                status = self._execute_phase(phase, states, targets)
                if status is not None:
                    return status  # PAUSED_ERROR

            # -- gate(按序执行该 phase 的全部 gate)--------------------------
            if phase.gate:
                gate_results = {}
                for gate_id in phase.gate:
                    outcome, result = self._run_gate_with_policy(
                        phase, gate_id, states, targets
                    )
                    if outcome != "advanced":
                        return outcome  # 某种 PAUSED
                    gate_results[gate_id] = result
                self._checkpoint(phase, states, gate_results, next_phase)
            else:
                self._checkpoint(phase, states, None, next_phase)

        self.db.update_workflow_run(self.run_id, status=COMPLETED,
                                    ended_at=_utcnow(), phase_states=states)
        log_event(self.logger, logging.INFO, "workflow_completed", run_id=self.run_id)
        return COMPLETED

    def _merged_targets(self) -> dict:
        """workflow targets ← task 顶层 year_threshold/venues ← task.targets(后者优先)。"""
        merged = dict(self.definition.targets)
        for key in ("year_threshold", "venues"):
            if key in self.task:
                merged[key] = self.task[key]
        merged.update(self.task.get("targets", {}))
        return merged

    def _execute_phase(self, phase: PhaseDef, states: dict, targets: dict) -> Optional[str]:
        """跑 phase 的 LLM turn + 契约验证。成功返回 None,失败返回 PAUSED_ERROR。"""
        self.db.update_workflow_run(
            self.run_id, current_phase=phase.id, status=IN_PROGRESS,
            phase_states={**states, phase.id: S_RUNNING},
        )
        states[phase.id] = S_RUNNING
        self.runtime.tools = self.base_registry.subset(phase.toolset)
        self.runtime.current_phase = phase.id  # skill_usage 归因(M5b,不变量 3)

        prompt = self._render_prompt(phase, targets)
        last_error: Optional[str] = None
        for attempt in range(1, MAX_PHASE_RETRIES + 2):
            self.barrier.hit("before_phase_turn")
            self.barrier.hit(f"before_phase_turn:{phase.id}")
            try:
                self.runtime.run_turn(prompt)
            except Exception as e:
                last_error = f"turn 异常:{e}"
                log_event(self.logger, logging.ERROR, "workflow_phase_error",
                          run_id=self.run_id, phase=phase.id, attempt=attempt,
                          error=repr(e))
                continue
            missing = self._check_contract(phase)
            if missing is None:
                states[phase.id] = S_CONTRACTED
                self.db.update_workflow_run(self.run_id, phase_states=states)
                log_event(self.logger, logging.INFO, "workflow_phase",
                          run_id=self.run_id, phase=phase.id, state=S_CONTRACTED)
                return None
            last_error = missing
            prompt = self._repair_prompt(phase, missing)
            log_event(self.logger, logging.WARNING, "workflow_phase_error",
                      run_id=self.run_id, phase=phase.id, attempt=attempt,
                      error=missing)

        states[f"error:{phase.id}"] = last_error
        self.db.update_workflow_run(self.run_id, status=PAUSED_ERROR,
                                    phase_states=states)
        return PAUSED_ERROR

    def _run_gate_with_policy(
        self, phase: PhaseDef, gate_id: str, states: dict, targets: dict
    ) -> tuple[str, Optional[dict]]:
        """跑单个 gate + escalation 策略。

        返回 ("advanced", result) 或 (PAUSED_*, None)。
        BLOCK gate 失败:重跑 phase(phase 是重试粒度)后重试,至多 max_retries 次。
        USER_DECISION gate 失败:有豁免(既往或现场决策)→ WAIVED;否则暂停等人。
        """
        gate_def = self.definition.gates[gate_id]
        waivers = states.setdefault("waivers", {})
        attempts_key = f"gate_attempts:{phase.id}:{gate_id}"

        while True:
            attempts = states.get(attempts_key, 0)
            self.barrier.hit("before_gate_execution")
            self.barrier.hit(f"before_gate_execution:{gate_id}")
            result = self._execute_gate(gate_id, gate_def, targets)
            result["status"] = self._apply_existing_waiver(gate_id, result, waivers)
            log_event(self.logger, logging.INFO, "gate_result",
                      run_id=self.run_id, gate=gate_id, phase=phase.id,
                      passed=result["status"] in ("PASSED", "WAIVED"),
                      retries=attempts, waived=result["status"] == "WAIVED")

            if result["status"] == "FAILED" and gate_def.escalation == "USER_DECISION":
                reason = self.waiver_decider(gate_id, result) if self.waiver_decider else None
                if reason:
                    waivers[gate_id] = {"reason": reason, "decided_by": "user"}
                    result["status"] = "WAIVED"
                    result["waiver"] = waivers[gate_id]

            if result["status"] in ("PASSED", "WAIVED"):
                self._update_evidence(phase, states, "positive")
                if result["status"] == "WAIVED":
                    self._submit_review("gate_waiver", phase, gate_id, result)
                return "advanced", result

            # FAILED:立即复盘(失败即学习机会,E9)+ 负面证据
            self._update_evidence(phase, states, "negative")
            self._submit_review("gate_failure", phase, gate_id, result)

            if gate_def.escalation == "BLOCK":
                attempts += 1
                states[attempts_key] = attempts
                if attempts <= gate_def.max_retries:
                    # 重跑该 phase 后再试(落盘:回到 running)
                    states[phase.id] = S_RUNNING
                    self.db.update_workflow_run(self.run_id, phase_states=states,
                                                status=IN_PROGRESS)
                    status = self._execute_phase(phase, states, targets)
                    if status is not None:
                        return status, None
                    continue
                states[f"gate_failure:{gate_id}"] = result
                self.db.update_workflow_run(self.run_id, status=PAUSED_GATE_BLOCK,
                                            phase_states=states)
                return PAUSED_GATE_BLOCK, None

            # USER_DECISION 且无人豁免 → 暂停等人
            states[f"gate_failure:{gate_id}"] = result
            self.db.update_workflow_run(self.run_id, status=PAUSED_USER_DECISION,
                                        phase_states=states)
            return PAUSED_USER_DECISION, None

    # -- 组件 -------------------------------------------------------------------

    def _submit_review(self, trigger: str, phase: PhaseDef,
                       gate_id: str, result: dict) -> None:
        """E9 触发点:phase 结束 / gate 失败 / 豁免 → fire-and-forget 复盘。"""
        if self.review_fork is not None:
            self.review_fork.submit(
                trigger, self.runtime.session_id,
                phase_id=phase.id,
                gate_results=[{**result, "gate_id": gate_id}],
            )

    def _update_evidence(self, phase: PhaseDef, states: dict, outcome: str) -> None:
        """证据流:gate 结算 → skill evidence → promotion/demotion(可选装配)。"""
        if self.skill_lib is None:
            return
        from ..review_fork import apply_gate_evidence, check_transitions

        wm_key = f"evidence_watermark:{phase.id}"
        new_wm, skills = apply_gate_evidence(
            self.db, self.skill_lib, self.runtime.session_id,
            phase.id, outcome, states.get(wm_key, 0),
        )
        states[wm_key] = new_wm
        for skill_name in skills:
            check_transitions(
                self.db, self.skill_lib, skill_name,
                promotion_uses=int(self.learning.get("promotion_uses", 3)),
                promotion_ratio=float(self.learning.get("promotion_ratio", 0.6)),
            )
        if skills:
            self.db.update_workflow_run(self.run_id, phase_states=states)

    def _render_prompt(self, phase: PhaseDef, targets: dict) -> str:
        template_path = self.definition_dir / phase.prompt_template
        template = string.Template(template_path.read_text(encoding="utf-8"))
        artifacts = [
            p.output_contract.path for p in self.definition.phases
            if (self.workdir / p.output_contract.path).exists()
        ]
        mapping = {
            "section": self.task.get("section", ""),
            "focus_areas": ", ".join(self.task.get("focus_areas", [])),
            "seed_papers": json.dumps(self.task.get("seed_papers", []),
                                      ensure_ascii=False, indent=2),
            "year_threshold": str(self.task.get("year_threshold", 2024)),
            "workdir": str(self.workdir),
            "prior_artifacts": ", ".join(artifacts) or "(none)",
            "targets": json.dumps(targets, ensure_ascii=False),
        }
        return template.safe_substitute(mapping)

    def _repair_prompt(self, phase: PhaseDef, missing: str) -> str:
        return (
            f"上一个任务未完成产出契约:{missing}。"
            f"请立即用 write_file 把要求的文件写到 {self.workdir}/"
            f"{phase.output_contract.path}。"
        )

    def _check_contract(self, phase: PhaseDef) -> Optional[str]:
        path = self.workdir / phase.output_contract.path
        if not path.is_file():
            return f"产出缺失:{phase.output_contract.path}"
        if path.stat().st_size < phase.output_contract.min_bytes:
            return f"产出过小({path.stat().st_size}B):{phase.output_contract.path}"
        return None

    def _execute_gate(self, gate_id: str, gate_def: GateDef, targets: dict) -> dict:
        args = {
            **gate_def.args,
            "workdir": str(self.workdir),
            "targets": targets,
            **self.gate_args_extra,
        }
        ctx = ToolContext(cwd=self.workdir, session_id=self.runtime.session_id,
                          db=self.db)
        raw = self.base_registry.execute(gate_def.tool, args, ctx)
        try:
            result = json.loads(raw)
        except json.JSONDecodeError:
            result = {"gate_id": gate_id, "status": "FAILED",
                      "criteria": [], "violations": [f"gate 工具输出非 JSON:{raw[:200]}"]}
        result.setdefault("gate_id", gate_id)
        result.setdefault("status", "FAILED")
        result.setdefault("criteria", [])
        result.setdefault("violations", [])
        return result

    @staticmethod
    def _apply_existing_waiver(gate_id: str, result: dict, waivers: dict) -> str:
        """已豁免的 gate 不再拦(resume 后自动跳过,对齐 LOOP_PLAN §2.4)。"""
        if result["status"] == "FAILED" and gate_id in waivers:
            result["waiver"] = waivers[gate_id]
            return "WAIVED"
        return result["status"]

    def _checkpoint(self, phase: PhaseDef, states: dict,
                    gate_result: Optional[dict], next_phase: Optional[str]) -> None:
        artifact = str(self.workdir / phase.output_contract.path)
        self.db.add_workflow_checkpoint(
            self.run_id, phase.id, [artifact], gate_result
        )
        states[phase.id] = S_PASSED
        self.db.update_workflow_run(
            self.run_id,
            current_phase=next_phase or phase.id,
            phase_states=states,
            status=IN_PROGRESS if next_phase else COMPLETED,
        )
        log_event(self.logger, logging.INFO, "workflow_checkpoint",
                  run_id=self.run_id, phase=phase.id, next_phase=next_phase)
        # E9:phase 结束(passed)触发复盘
        if self.review_fork is not None:
            self.review_fork.submit(
                "phase_end", self.runtime.session_id,
                phase_id=phase.id,
                gate_results=list((gate_result or {}).values()),
            )


def _utcnow() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
