"""CLI:mini-hermes chat --session <id> / --resume <id>。

REPL:流式输出 assistant 文本;工具调用显示一行摘要(▸ name: args);
斜杠命令 /resume <id> /exit。Ctrl-C / SIGTERM 优雅退出(exit 0)。
测试钩子(均通过环境变量):
- MINI_HERMES_TEST_BARRIER_DIR  混沌测试冻结点
- MINI_HERMES_TEST_MARKER_FILE  注册 write_marker_file 副作用工具
- MINI_HERMES_WATCHDOG_DEADLINE_S  看门狗 deadline(默认 300s)
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import uuid
from pathlib import Path

import yaml

from . import supervisor
from .barrier import TestBarrier
from .config import load_config
from .logging import configure_from_config, log_event
from .metrics import MetricsWriter, RunMetrics, utcnow
from .runtime import (
    AgentRuntime,
    DaemonThreads,
    make_echo_tool,
    make_write_marker_file_tool,
)
from .skills_lib import SkillLibrary, build_system_prompt
from .state_db import SessionDB
from .tools.registry import Registry, discover_builtin_tools
from .watchdog import Heartbeat, Watchdog
from .workflow.engine import COMPLETED, WorkflowEngine
from .workflow.definition import load_builtin_workflow


def _build_registry(config=None) -> Registry:
    no_skills = bool(getattr(config, "no_skills", False))
    registry = discover_builtin_tools(Registry(), include_learning=not no_skills)
    registry.register(*make_echo_tool())
    marker = os.environ.get("MINI_HERMES_TEST_MARKER_FILE")
    if marker:  # test-only
        registry.register(*make_write_marker_file_tool(marker))
    return registry


def _make_stream_callbacks():
    def on_text_delta(text: str) -> None:
        print(text, end="", flush=True)

    def on_tool_call(name: str, arguments: str) -> None:
        summary = arguments.replace("\n", " ")[:80]
        print(f"\n▸ {name}: {summary}", flush=True)

    return on_text_delta, on_tool_call


def cmd_chat(args: argparse.Namespace) -> int:
    try:
        config = load_config(args.config)
    except Exception as e:
        print(f"配置错误:{e}", file=sys.stderr)
        return supervisor.EXIT_FATAL_CONFIG

    logger = configure_from_config(config)
    config.paths.data_dir.mkdir(parents=True, exist_ok=True)

    db = SessionDB(config.paths.data_dir / "state.db")
    heartbeat = Heartbeat(config.paths.data_dir / "heartbeat")
    heartbeat.start()

    deadline = float(os.environ.get("MINI_HERMES_WATCHDOG_DEADLINE_S", "300"))
    watchdog = Watchdog(
        deadline_s=deadline,
        dump_path=config.paths.data_dir / "watchdog_dump.log",
    )
    watchdog.start()

    session_id = args.session or args.resume
    on_text_delta, on_tool_call = _make_stream_callbacks()
    metrics = RunMetrics(session_id=session_id)
    aux = config.effective_aux_model
    runtime = AgentRuntime(
        db,
        session_id,
        base_url=config.model.base_url,
        api_key=config.model.api_key,
        model=config.model.name,
        aux_base_url=aux.base_url,
        aux_api_key=aux.api_key,
        aux_model=aux.name,
        compression=config.compression,
        system_prompt=build_system_prompt(config),
        tools=_build_registry(config),
        barrier=TestBarrier.from_env(),
        watchdog=watchdog,
        metrics=metrics,
        config=config,
        on_text_delta=on_text_delta,
        on_tool_call=on_tool_call,
    )

    # 优雅中断:置标志,当前落盘完成后退出(不变量:中断只做标志)
    def _on_signal(signum, frame):
        runtime.request_interrupt()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    log_event(logger, logging.INFO, "session_start",
              session_id=session_id, model=config.model.name, source="cli")
    db.create_session(session_id, source="cli", model=config.model.name,
                      cwd=os.getcwd())
    exit_code = supervisor.EXIT_CLEAN
    try:
        if args.resume:
            if runtime.resume() and not runtime.interrupt_requested:
                runtime.continue_run()
                print()
        if args.message:
            for text in args.message:
                if runtime.interrupt_requested:
                    break
                runtime.run_turn(text)
                print()  # 流式输出后的收尾换行
        elif not args.resume:
            _repl(runtime, db)
    except KeyboardInterrupt:
        runtime.request_interrupt()
    except Exception as e:
        print(f"\nturn 失败:{e}", file=sys.stderr)
        exit_code = 1
    finally:
        end_reason = "interrupted" if runtime.interrupt_requested else "completed"
        db.end_session(session_id, end_reason)
        metrics.ended_at = metrics.ended_at or utcnow()
        MetricsWriter(config.paths.data_dir / "metrics.jsonl").write(metrics)
        log_event(logger, logging.INFO, "session_end",
                  session_id=session_id, end_reason=end_reason,
                  message_count=len(runtime.messages))
        heartbeat.stop()
        db.close()
    return exit_code


def _repl(runtime: AgentRuntime, db: SessionDB) -> None:
    print("mini-hermes (M2)。输入消息回车发送;/resume <id> 恢复会话;/exit 退出。")
    while not runtime.interrupt_requested:
        try:
            line = input("> ")
        except (EOFError, KeyboardInterrupt):
            print()
            break
        line = line.strip()
        if not line:
            continue
        if line == "/exit":
            break
        if line.startswith("/resume "):
            new_id = line.split(None, 1)[1].strip()
            runtime.session_id = new_id
            db.create_session(new_id, source="cli")
            if runtime.resume():
                runtime.continue_run()
                print()
            continue
        try:
            runtime.run_turn(line)
            print()
        except Exception as e:
            print(f"[turn 失败: {e}]", file=sys.stderr)


def _wf_bootstrap(args):
    """workflow 子命令公共装配:config / db / registry / workflow 定义。"""
    try:
        config = load_config(args.config)
    except Exception as e:
        print(f"配置错误:{e}", file=sys.stderr)
        sys.exit(supervisor.EXIT_FATAL_CONFIG)
    configure_from_config(config)
    config.paths.data_dir.mkdir(parents=True, exist_ok=True)
    db = SessionDB(config.paths.data_dir / "state.db")
    registry = _build_registry()
    definition, def_dir = load_builtin_workflow(args.workflow)
    return config, db, registry, definition, def_dir


def _wf_runtime(config, db, registry, session_id: str, workdir: Path) -> AgentRuntime:
    aux = config.effective_aux_model
    return AgentRuntime(
        db, session_id,
        base_url=config.model.base_url,
        api_key=config.model.api_key,
        model=config.model.name,
        aux_base_url=aux.base_url,
        aux_api_key=aux.api_key,
        aux_model=aux.name,
        compression=config.compression,
        tools=registry,
        barrier=TestBarrier.from_env(),
        config=config,
        cwd=workdir,
    )


def _wf_gate_extra(config, task: dict) -> dict:
    extra = {
        "external_mode": task.get("gate_external_mode",
                                  config.workflows.gate_external_mode),
    }
    if task.get("cassette_dir"):
        extra["cassette_dir"] = task["cassette_dir"]
    return extra


def _interactive_waiver_decider(gate_id: str, result: dict):
    """USER_DECISION gate 失败:交互询问;非交互环境返回 None(暂停)。"""
    if not sys.stdin.isatty():
        return None
    violations = result.get("violations", [])
    print(f"\nGate {gate_id} FAILED(USER_DECISION):")
    for v in violations[:10]:
        print(f"  - {v}")
    answer = input("豁免并继续?原因留空=否 / 输入原因=豁免: ").strip()
    return answer or None


def _make_aux_caller(config, db: SessionDB):
    """辅模型调用器(review/backfill 用,走 runtime 的重试包装)。"""
    aux = config.effective_aux_model
    rt = AgentRuntime(
        db, "review-aux",
        base_url=config.model.base_url, api_key=config.model.api_key,
        model=config.model.name,
        aux_base_url=aux.base_url, aux_api_key=aux.api_key, aux_model=aux.name,
    )
    return rt._aux_summarize


def _make_review_fork(config, db: SessionDB, workdir: Path):
    from .review_fork import ReviewFork

    lib = SkillLibrary(config.paths.skill_lib_dir,
                       remote=config.skill_lib_remote)
    return ReviewFork(
        db, lib, _make_aux_caller(config, db),
        data_dir=config.paths.data_dir, workdir=workdir,
    )


def cmd_workflow_run(args: argparse.Namespace) -> int:
    config, db, registry, definition, def_dir = _wf_bootstrap(args)
    try:
        task = yaml.safe_load(Path(args.task).read_text(encoding="utf-8")) or {}
        run_id = args.run_id or uuid.uuid4().hex[:12]
        session_id = args.session or f"wf-{run_id}"
        workdir = Path(task.get("workdir")
                       or config.paths.data_dir / "workflows" / run_id)
        db.create_session(session_id, source="workflow", model=config.model.name)
        runtime = _wf_runtime(config, db, registry, session_id, workdir)

        daemons = DaemonThreads()
        # §5 隔离:no_skills 时 review fork 不触发、证据流关闭;
        # review_enabled=false 时(确定性测试)同样全关
        fork = None
        if config.learning.review_enabled and not config.no_skills:
            fork = _make_review_fork(config, db, workdir)
            fork.register_daemon(daemons)
        daemons.start()

        engine = WorkflowEngine.start(
            db, definition, def_dir, runtime, registry, workdir, task,
            run_id=run_id,
            waiver_decider=_interactive_waiver_decider,
            gate_args_extra=_wf_gate_extra(config, task),
            review_fork=fork,
            skill_lib=fork.lib if fork else None,
            learning={
                "promotion_uses": config.learning.promotion_uses,
                "promotion_ratio": config.learning.promotion_ratio,
            },
        )
        try:
            status = engine.run()
        finally:
            daemons.stop()
        print(f"run {run_id}: {status}(workdir={workdir})")
        return supervisor.EXIT_CLEAN if status == COMPLETED else 1
    finally:
        db.close()


def cmd_workflow_resume(args: argparse.Namespace) -> int:
    config, db, registry, definition, def_dir = _wf_bootstrap(args)
    try:
        row = db.get_workflow_run(args.run)
        if row is None:
            print(f"run 不存在:{args.run}", file=sys.stderr)
            return supervisor.EXIT_FATAL_CONFIG
        runtime = _wf_runtime(config, db, registry, row["session_id"],
                              Path(row["workdir"]))
        task = row["task"]
        fork = None
        if config.learning.review_enabled and not config.no_skills:
            fork = _make_review_fork(config, db, Path(row["workdir"]))
        daemons = DaemonThreads()
        if fork:
            fork.register_daemon(daemons)
        daemons.start()
        engine = WorkflowEngine.resume(
            db, args.run, definition, def_dir, runtime, registry,
            waiver_decider=_interactive_waiver_decider,
            gate_args_extra=_wf_gate_extra(config, task),
            review_fork=fork,
            skill_lib=fork.lib if fork else None,
            learning={
                "promotion_uses": config.learning.promotion_uses,
                "promotion_ratio": config.learning.promotion_ratio,
            },
        )
        try:
            status = engine.run()
        finally:
            daemons.stop()
        print(f"run {args.run}: {status}")
        return supervisor.EXIT_CLEAN if status == COMPLETED else 1
    finally:
        db.close()


def cmd_workflow_waive(args: argparse.Namespace) -> int:
    config, db, registry, definition, def_dir = _wf_bootstrap(args)
    try:
        row = db.get_workflow_run(args.run)
        if row is None:
            print(f"run 不存在:{args.run}", file=sys.stderr)
            return supervisor.EXIT_FATAL_CONFIG
        states = row["phase_states"]
        states.setdefault("waivers", {})[args.gate] = {
            "reason": args.reason, "decided_by": "user",
        }
        db.update_workflow_run(args.run, phase_states=states)
        log_event(logging.getLogger("mini_hermes"), logging.INFO, "gate_waived",
                  run_id=args.run, gate=args.gate, reason=args.reason)
        print(f"gate {args.gate} 已豁免(run {args.run});用 workflow resume 继续")
        return supervisor.EXIT_CLEAN
    finally:
        db.close()


# --------------------------------------------------------------------------
# skills 子命令(M5a)
# --------------------------------------------------------------------------

from .skills_lib import SEED_DIR  # noqa: E402


def cmd_review(args: argparse.Namespace) -> int:
    """手动触发一次复盘(同步排空)。"""
    try:
        config = load_config(args.config)
    except Exception as e:
        print(f"配置错误:{e}", file=sys.stderr)
        return supervisor.EXIT_FATAL_CONFIG
    configure_from_config(config)
    db = SessionDB(config.paths.data_dir / "state.db")
    try:
        fork = _make_review_fork(config, db, Path(args.workdir) if args.workdir else None)
        fork.submit("manual", args.session, phase_id=args.phase)
        fork.drain()
        print(f"review 完成(session={args.session})")
        return supervisor.EXIT_CLEAN
    finally:
        db.close()


def cmd_skills_backfill(args: argparse.Namespace, config) -> int:
    from .review_fork import DEFAULT_BACKFILL_SOURCES, run_backfill

    db = SessionDB(config.paths.data_dir / "state.db")
    try:
        lib = SkillLibrary(config.paths.skill_lib_dir,
                           remote=config.skill_lib_remote)
        sources = args.source or DEFAULT_BACKFILL_SOURCES
        created = run_backfill(lib, _make_aux_caller(config, db), sources)
        print(f"backfilled {len(created)} skills:{', '.join(created) or '(无新增)'}")
        return supervisor.EXIT_CLEAN
    finally:
        db.close()


def cmd_skills(args: argparse.Namespace) -> int:
    try:
        config = load_config(args.config)
    except Exception as e:
        print(f"配置错误:{e}", file=sys.stderr)
        return supervisor.EXIT_FATAL_CONFIG
    lib = SkillLibrary(config.paths.skill_lib_dir,
                       remote=config.skill_lib_remote)

    if args.skills_command == "seed":
        if not SEED_DIR.is_dir():
            print(f"种子目录不存在:{SEED_DIR}", file=sys.stderr)
            return 1
        added = lib.seed_from_directory(SEED_DIR)
        print(f"seeded {len(added)} skills:{', '.join(added) or '(无新增)'}")
        return supervisor.EXIT_CLEAN

    if args.skills_command == "backfill":
        return cmd_skills_backfill(args, config)

    if args.skills_command == "list":
        for s in lib.list_skills(include_deprecated=True):
            print(f"{s.name} [{s.status}] {s.description} "
                  f"evidence={s.evidence}")
        return supervisor.EXIT_CLEAN

    config.paths.data_dir.mkdir(parents=True, exist_ok=True)
    db = SessionDB(config.paths.data_dir / "state.db")
    try:
        if args.skills_command == "pending":
            rows = db._conn.execute(
                "SELECT key, value FROM state_meta"
                " WHERE key LIKE 'pending_deprecation:%'"
            ).fetchall()
            for r in rows:
                print(f"{r['key'].split(':', 1)[1]}: {r['value']}")
            return supervisor.EXIT_CLEAN
        if args.skills_command == "approve-deprecation":
            key = f"pending_deprecation:{args.name}"
            record = db.meta_get(key)
            if record is None:
                print(f"无待批记录:{args.name}", file=sys.stderr)
                return 1
            lib.update(args.name, status="deprecated")
            db._conn.execute("DELETE FROM state_meta WHERE key=?", (key,))
            print(f"{args.name} 已 deprecated(人审通过)")
            return supervisor.EXIT_CLEAN
    finally:
        db.close()
    return supervisor.EXIT_FATAL_CONFIG


def cmd_experiment(args: argparse.Namespace) -> int:
    from .experiments.analysis import pilot_baseline, render_report
    from .experiments.runner import ExperimentRunner, load_tasks

    try:
        config = load_config(args.config)
    except Exception as e:
        print(f"配置错误:{e}", file=sys.stderr)
        return supervisor.EXIT_FATAL_CONFIG
    configure_from_config(config)
    exp_root = Path(__file__).resolve().parents[2] / "experiments"
    out_dir = Path(args.out or config.paths.data_dir / "experiments")

    if args.exp_command == "run":
        tasks = load_tasks(exp_root / "tasks.yaml",
                           only_ids=args.tasks.split(",") if args.tasks else None,
                           pilot=args.pilot)
        if not tasks:
            print("任务集为空", file=sys.stderr)
            return 1
        runner = ExperimentRunner(
            config, out_dir, seed=args.seed,
            rubric_path=exp_root / "rubric.md",
            on_progress=lambda m: print(m, flush=True),
        )
        reps = 3 if args.pilot else args.reps
        records = runner.run(tasks, "control" if args.pilot else args.arm, reps=reps)
        if args.pilot:
            baseline = pilot_baseline(records)
            print(f"pilot 通过率:{baseline['task_rates']}")
            print(f"spread={baseline['spread']:.1%} retry_mean={baseline['retry_mean']:.2f}"
                  f" retry_var={baseline['retry_var']:.2f}")
            if baseline["warning"]:
                print(f"⚠ {baseline['warning_text']}")
        print(f"完成 {len(records)} runs → {out_dir / 'experiment.db'}")
        return supervisor.EXIT_CLEAN

    if args.exp_command == "report":
        runner_db = out_dir / "experiment.db"
        if not runner_db.exists():
            print(f"实验库不存在:{runner_db}(先跑 experiment run)", file=sys.stderr)
            return 1
        db = SessionDB(runner_db)
        try:
            runs = db.get_experiment_runs()
        finally:
            db.close()
        result = render_report(exp_root / "report_template.md", runs,
                               args.report_out, seed=args.seed)
        print(f"verdict: {result['verdict']} → {args.report_out}")
        return supervisor.EXIT_CLEAN
    return supervisor.EXIT_FATAL_CONFIG


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mini-hermes")
    parser.add_argument("--config", default="config.yaml")
    sub = parser.add_subparsers(dest="command", required=True)
    chat = sub.add_parser("chat", help="对话(新会话或 --resume 恢复)")
    chat.add_argument("--session", help="新会话 ID")
    chat.add_argument("--resume", help="恢复已有会话 ID")
    chat.add_argument("--message", action="append",
                      help="非交互模式:按顺序发送(可重复)")

    wf = sub.add_parser("workflow", help="工作流(M3)")
    wf_sub = wf.add_subparsers(dest="wf_command", required=True)
    wf_run = wf_sub.add_parser("run", help="启动 workflow run")
    wf_run.add_argument("--workflow", default="academic_paper")
    wf_run.add_argument("--task", required=True, help="任务 YAML 路径")
    wf_run.add_argument("--session", help="会话 ID(缺省自动生成)")
    wf_run.add_argument("--run-id", help="run ID(缺省自动生成)")
    wf_resume = wf_sub.add_parser("resume", help="从 checkpoint 恢复 run")
    wf_resume.add_argument("--run", required=True, help="run ID")
    wf_resume.add_argument("--workflow", default="academic_paper")
    wf_waive = wf_sub.add_parser("waive", help="豁免 USER_DECISION gate")
    wf_waive.add_argument("--run", required=True)
    wf_waive.add_argument("--gate", required=True)
    wf_waive.add_argument("--reason", required=True)
    wf_waive.add_argument("--workflow", default="academic_paper")

    sk = sub.add_parser("skills", help="skill 库管理(M5a)")
    sk_sub = sk.add_subparsers(dest="skills_command", required=True)
    sk_sub.add_parser("seed", help="导入种子 skill(幂等)")
    sk_bf = sk_sub.add_parser("backfill", help="从历史运行回填 skill(低置信度)")
    sk_bf.add_argument("--source", action="append",
                       help="历史运行目录(可重复;缺省用两个默认源)")
    sk_sub.add_parser("list", help="列出全部 skill")
    sk_sub.add_parser("pending", help="列出待批准的 deprecate")
    sk_approve = sk_sub.add_parser("approve-deprecation",
                                   help="批准 deprecate(人审,不变量 7)")
    sk_approve.add_argument("name")

    rv = sub.add_parser("review", help="手动触发一次复盘(M5b)")
    rv.add_argument("--session", required=True)
    rv.add_argument("--phase", help="关联的 phase(可选)")
    rv.add_argument("--workdir", help="REVIEW_REPORT 所在 run 目录(可选)")

    ex = sub.add_parser("experiment", help="对照实验(M6)")
    ex_sub = ex.add_subparsers(dest="exp_command", required=True)
    ex_run = ex_sub.add_parser("run", help="跑一臂(或领航)")
    ex_run.add_argument("--arm", choices=["treatment", "control"],
                        default="treatment")
    ex_run.add_argument("--pilot", action="store_true",
                        help="§10.4 基线方差:control 臂 × pilot 任务 × 3 次")
    ex_run.add_argument("--tasks", help="逗号分隔的任务 id 子集")
    ex_run.add_argument("--reps", type=int, default=1)
    ex_run.add_argument("--seed", type=int, default=0)
    ex_run.add_argument("--out", help="实验输出目录(默认 <data_dir>/experiments)")
    ex_rep = ex_sub.add_parser("report", help="生成配对分析报告")
    ex_rep.add_argument("--report-out", default="report.md")
    ex_rep.add_argument("--out", help="实验输出目录(同 run)")
    ex_rep.add_argument("--seed", type=int, default=0)

    args = parser.parse_args(argv)

    if args.command == "chat":
        if not args.session and not args.resume:
            args.session = uuid.uuid4().hex[:16]
        return cmd_chat(args)
    if args.command == "workflow":
        return {
            "run": cmd_workflow_run,
            "resume": cmd_workflow_resume,
            "waive": cmd_workflow_waive,
        }[args.wf_command](args)
    if args.command == "skills":
        return cmd_skills(args)
    if args.command == "review":
        return cmd_review(args)
    if args.command == "experiment":
        return cmd_experiment(args)
    return supervisor.EXIT_FATAL_CONFIG


if __name__ == "__main__":
    sys.exit(main())
