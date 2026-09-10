"""Supervisor 契约 —— Hermes 骨架组件 #10。

exit 75 (EX_TEMPFAIL) = "请重启我"。进程内绝不自重启(不变量 4);
复活完全由外部 supervisor(systemd user unit,见 packaging/systemd/)负责。

退出码约定:
- 0  EXIT_CLEAN        正常退出(含 Ctrl-C / SIGTERM 优雅中断)
- 2  EXIT_FATAL_CONFIG 配置错误,重启无意义,不要拉起
- 75 EXIT_RESTART      看门狗杀卡死进程 / 内部请求重启 → supervisor 应拉起
"""

EXIT_CLEAN = 0
EXIT_FATAL_CONFIG = 2
EXIT_RESTART = 75  # EX_TEMPFAIL
