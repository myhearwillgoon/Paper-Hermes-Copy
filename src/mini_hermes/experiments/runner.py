"""对照实验 runner(PLAN §10)。

配对被试内设计:同一任务在 treatment(skill 库)/ control(--no-skills)
两臂各跑一次。每 run 全新 session DB + 全新 workdir;指标落 experiment_runs
(§4 全列)。臂顺序随机化(带 seed,日志记录)。

隔离(§5 硬要求):
- treatment:skill_lib_dir=<lib>/treatment,review_enabled 照开(学习即变量)
- control  :no_skills=true + review_enabled=false + control-seed/ 独立库

LLM-judge(§10.3):固定 rubric、盲评(输入不含臂标识)、存 llm_judge_json。

确定性说明:review fork 在 runner 里**不挂 daemon**,engine 提交进队列,
run 结束后 runner 统一 drain —— 异步复盘变同步,假端点场景队列顺序可预测。
"""

from __future__ import annotations

import json
import logging
import random
import subprocess
import uuid
from pathlib import Path
from typing import Callable, Optional

import yaml

from ..logging import log_event
from ..metrics import RunMetrics
from ..review_fork import ReviewFork
from ..runtime import AgentRuntime
from ..skills_lib import SEED_DIR, SkillLibrary, build_system_prompt
from ..state_db import SessionDB
from ..tools.registry import Registry, discover_builtin_tools
from ..workflow.definition import load_builtin_workflow
from ..workflow.engine import WorkflowEngine

_logger = logging.getLogger("mini_hermes")

JUDGE_PROMPT = """你是论文评审。按以下 rubric 给章节打分,只输出 JSON。

{rubric}

## 待评章节
{final_text}
"""


def load_tasks(path: str | Path, only_ids: Optional[list[str]] = None,
               pilot: bool = False) -> list[dict]:
    with Path(path).open("r", encoding="utf-8") as f:
        tasks = yaml.safe_load(f) or []
    if pilot:
        tasks = [t for t in tasks if t.get("pilot")]
    else:
        tasks = [t for t in tasks if not t.get("pilot")]
    if only_ids:
        wanted = set(only_ids)
        tasks = [t for t in tasks if t["id"] in wanted]
    return tasks


def _git_head(lib_dir: Path) -> Optional[str]:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=lib_dir,
            capture_output=True, text=True, check=True,
        )
        return out.stdout.strip()
    except Exception:
        return None


class ExperimentRunner:
    def __init__(
        self,
        config,
        out_dir: str | Path,
        *,
        seed: int = 0,
        judge_call: Optional[Callable[[str], str]] = None,
        rubric_path: Optional[str | Path] = None,
        on_progress: Optional[Callable[[str], None]] = None,
    ):
        self.config = config
        self.out_dir = Path(out_dir)
        self.seed = seed
        self.rubric_path = Path(rubric_path) if rubric_path else None
        self.on_progress = on_progress or (lambda msg: None)
        self._judge_call = judge_call or self._default_judge_call
        # 中央实验库:experiment_runs 全落这里(各 run 的 session DB 独立)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.exp_db = SessionDB(self.out_dir / "experiment.db")

    # -- 装配 --------------------------------------------------------------

    def _arm_config(self, arm: str):
        lib_root = Path(self.config.paths.data_dir) / "skill-lib"
        if arm == "treatment":
            return self.config.model_copy(deep=True, update={
                "paths": self.config.paths.model_copy(
                    update={"skill_lib_dir": lib_root / "treatment"}),
            })
        return self.config.model_copy(deep=True, update={
            "no_skills": True,
            "paths": self.config.paths.model_copy(
                update={"skill_lib_dir": lib_root / "control-seed"}),
            "learning": self.config.learning.model_copy(
                update={"review_enabled": False}),
        })

    def _default_judge_call(self, prompt: str) -> str:
        rt = self._judge_runtime
        return rt._aux_summarize(prompt)

    def _make_aux_caller(self, db: SessionDB, arm_config):
        aux = arm_config.effective_aux_model
        rt = AgentRuntime(
            db, "judge-aux",
            base_url=arm_config.model.base_url, api_key=arm_config.model.api_key,
            model=arm_config.model.name,
            aux_base_url=aux.base_url, aux_api_key=aux.api_key, aux_model=aux.name,
        )
        return rt

    # -- 主流程 -------------------------------------------------------------

    def run(
        self,
        tasks: list[dict],
        arm: str,
        *,
        reps: int = 1,
    ) -> list[dict]:
        """跑一臂。返回写入的 experiment_runs 记录列表。"""
        rng = random.Random(f"{self.seed}:{arm}")
        order = list(tasks)
        rng.shuffle(order)
        log_event(_logger, logging.INFO, "experiment_start",
                  arm=arm, seed=self.seed, n_tasks=len(order), reps=reps)
        self.on_progress(f"arm={arm} seed={self.seed} 顺序={[t['id'] for t in order]}")

        records = []
        for task in order:
            for rep in range(reps):
                self.on_progress(f"[{arm}] {task['id']} rep{rep + 1}/{reps}")
                records.append(self._run_one(task, arm, rep))
        return records

    def _run_one(self, task: dict, arm: str, rep: int) -> dict:
        arm_config = self._arm_config(arm)
        run_dir = self.out_dir / arm / task["id"] / f"rep{rep}"
        workdir = run_dir / "work"
        session_id = f"exp-{arm}-{task['id']}-r{rep}"
        started_at = _utcnow()

        # treatment:空库则播种(种子库是学习的起点);commit 入库
        lib = SkillLibrary(arm_config.paths.skill_lib_dir,
                           remote=arm_config.skill_lib_remote)
        if arm == "treatment" and not lib.list_skills():
            lib.seed_from_directory(SEED_DIR)
        skill_lib_commit = _git_head(lib.root)

        db = SessionDB(run_dir / "state.db")
        self._judge_runtime = self._make_aux_caller(db, arm_config)
        try:
            db.create_session(session_id, source="experiment",
                              model=arm_config.model.name)
            registry = discover_builtin_tools(
                Registry(), include_learning=(arm == "treatment"))
            metrics = RunMetrics(session_id=session_id, arm=arm)
            self._judge_runtime.metrics = metrics  # 辅模型 token 计入本 run
            runtime = AgentRuntime(
                db, session_id,
                base_url=arm_config.model.base_url,
                api_key=arm_config.model.api_key,
                model=arm_config.model.name,
                aux_base_url=arm_config.effective_aux_model.base_url,
                aux_api_key=arm_config.effective_aux_model.api_key,
                aux_model=arm_config.effective_aux_model.name,
                compression=arm_config.compression,
                system_prompt=build_system_prompt(arm_config),
                tools=registry,
                metrics=metrics,
                config=arm_config,
                cwd=workdir,
            )

            # review fork:不入 daemon,run 结束后统一 drain(确定性)
            fork = None
            if arm == "treatment" and arm_config.learning.review_enabled:
                fork = ReviewFork(db, lib, self._judge_runtime._aux_summarize,
                                  data_dir=arm_config.paths.data_dir,
                                  workdir=workdir)

            definition, def_dir = load_builtin_workflow("academic_paper")
            engine = WorkflowEngine.start(
                db, definition, def_dir, runtime, registry, workdir, task,
                run_id=session_id,
                gate_args_extra={
                    "external_mode": arm_config.workflows.gate_external_mode,
                    **({"cassette_dir": task["cassette_dir"]}
                       if task.get("cassette_dir") else {}),
                },
                review_fork=fork,
                skill_lib=lib if arm == "treatment" else None,
                learning={
                    "promotion_uses": arm_config.learning.promotion_uses,
                    "promotion_ratio": arm_config.learning.promotion_ratio,
                },
            )
            status = engine.run()
            if fork is not None:
                fork.drain()

            record = self._collect(
                db, task, arm, session_id, status, skill_lib_commit,
                metrics, workdir, started_at,
            )
            self.exp_db.add_experiment_run(record)
            log_event(_logger, logging.INFO, "experiment_run_recorded",
                      arm=arm, task_id=task["id"], rep=rep, status=status)
            return record
        finally:
            db.close()

    # -- 指标收集 -------------------------------------------------------------

    def _collect(self, db, task, arm, session_id, status,
                 skill_lib_commit, metrics, workdir, started_at) -> dict:
        row = db.get_workflow_run(session_id)
        states = row["phase_states"] if row else {}
        gate_retries = {k.split(":", 1)[1]: v for k, v in states.items()
                        if k.startswith("gate_attempts:")}
        waivers = states.get("waivers", {})
        gates_passed = self._count_gates_passed(db, session_id)

        judge = self._judge(workdir)
        return {
            "id": f"{session_id}-{uuid.uuid4().hex[:6]}",
            "task_id": task["id"],
            "arm": arm,
            "session_id": session_id,
            "skill_lib_commit": skill_lib_commit,
            "no_skills_flag": 1 if arm == "control" else 0,
            "gates_passed": gates_passed,
            "gate_retries_json": gate_retries,
            "waiver_count": len(waivers),
            "input_tokens": metrics.main_tokens.input_tokens,
            "output_tokens": metrics.main_tokens.output_tokens,
            "aux_tokens": metrics.aux_tokens.total,
            "cost_usd": None,  # 定价表未接入;token 已分列,成本可事后折算
            "llm_judge_json": judge,
            "started_at": started_at,
            "ended_at": _utcnow(),
        }

    @staticmethod
    def _count_gates_passed(db, session_id) -> int:
        passed: set[str] = set()
        for cp in db.get_workflow_checkpoints(session_id):
            for gate_id, result in (cp["gate_result"] or {}).items():
                if result.get("status") in ("PASSED", "WAIVED"):
                    passed.add(gate_id)
        return len(passed)

    def _judge(self, workdir: Path) -> dict:
        """盲评:rubric + FINAL.md;输入不含臂标识(§10.3)。"""
        final = Path(workdir) / "FINAL.md"
        if not final.is_file() or self.rubric_path is None:
            return {}
        rubric = self.rubric_path.read_text(encoding="utf-8")
        prompt = JUDGE_PROMPT.format(
            rubric=rubric, final_text=final.read_text(encoding="utf-8")[:12000],
        )
        assert "treatment" not in prompt and "control" not in prompt  # 盲评自检
        try:
            raw = self._judge_call(prompt)
            text = raw.strip().strip("`").removeprefix("json").strip()
            data = json.loads(text)
            return {"scores": data.get("scores", {}),
                    "overall": data.get("overall")}
        except Exception as e:
            log_event(_logger, logging.WARNING, "llm_judge_failed", error=str(e))
            return {"error": str(e)}


def _utcnow() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
