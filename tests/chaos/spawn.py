"""混沌测试共享辅助:CLI 子进程 spawn + 确定性文件等待。

不用 sleep-and-hope:所有等待都是轮询文件出现,带 deadline。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def write_config(tmp_path: Path, base_url: str, data_dir: Path) -> Path:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f"""model:
  name: fake-model
  base_url: {base_url}
  api_key: fake
paths:
  data_dir: {data_dir}
learning:
  review_enabled: false
logging:
  level: WARNING
  dir: {data_dir}/logs
""",
        encoding="utf-8",
    )
    return cfg


def spawn_cli(
    config: Path,
    *cli_args: str,
    env_extra: dict | None = None,
) -> subprocess.Popen:
    """通用 CLI 子进程(workflow 等子命令用)。"""
    env = dict(os.environ)
    env.pop("MINI_HERMES_TEST_BARRIER_DIR", None)
    env.pop("MINI_HERMES_TEST_MARKER_FILE", None)
    if env_extra:
        env.update(env_extra)
    return subprocess.Popen(
        [sys.executable, "-m", "mini_hermes.cli", "--config", str(config),
         *cli_args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        cwd=REPO_ROOT,
    )


def spawn_chat(
    config: Path,
    *chat_args: str,
    env_extra: dict | None = None,
) -> subprocess.Popen:
    return spawn_cli(config, "chat", *chat_args, env_extra=env_extra)


def wait_for_file(path: Path, timeout_s: float = 60.0) -> None:
    """轮询等文件出现;超时抛 AssertionError(绝不 sleep-and-hope)。"""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.05)
    raise AssertionError(f"等待超时:{path} 未出现")


def wait_exit(proc: subprocess.Popen, timeout_s: float = 60.0) -> tuple[int, str, str]:
    try:
        out, err = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise AssertionError("子进程未在预算内退出")
    return proc.returncode, out, err


def barrier_env(barrier_dir: Path, marker_file: Path) -> dict:
    return {
        "MINI_HERMES_TEST_BARRIER_DIR": str(barrier_dir),
        "MINI_HERMES_TEST_MARKER_FILE": str(marker_file),
    }


def prep_barrier_go(barrier_dir: Path, except_point: str) -> None:
    """预放行除目标点外的所有 barrier 点:子进程只冻结在 except_point。"""
    from mini_hermes.barrier import POINTS

    barrier_dir.mkdir(parents=True, exist_ok=True)
    for point in POINTS:
        if point != except_point:
            (barrier_dir / f"{point}.go").touch()
