
from __future__ import annotations

import tempfile
from pathlib import Path


class PathSandbox:

    # 默认禁写路径：这些文件包含敏感配置，不允许 Agent 直接修改
    _DEFAULT_DENY_WRITE: list[str] = [
        ".aieb/config.yaml",
        ".aieb/permissions.local.yaml",
        ".aieb/skills/",
    ]

    def __init__(
        self,
        project_root: str,
        extra_allowed: list[str] | None = None,
        deny_write: list[str] | None = None,
    ) -> None:
        root = Path(project_root).resolve()
        self._allowed_roots: list[Path] = [root, Path(tempfile.gettempdir()).resolve()]
        if extra_allowed:
            for p in extra_allowed:
                self._allowed_roots.append(Path(p).resolve())

        # 会话级放行目录：只有用户点过 "Allow Always" 才会加进来，仅存内存，
        # 会话结束后消失。禁写清单仍然优先，因此不会因此放开配置文件写入。
        self._session_approved: list[Path] = []

        # 禁写路径列表：相对路径基于 project_root 解析
        self._deny_write_specs: list[str] = list(
            deny_write if deny_write is not None else self._DEFAULT_DENY_WRITE
        )
        self._rebuild_deny_write()

    def _rebuild_deny_write(self) -> None:
        root = self._allowed_roots[0]
        self._deny_write: list[Path] = []
        for dp in self._deny_write_specs:
            dp_path = Path(dp)
            if not dp_path.is_absolute():
                dp_path = root / dp_path
            self._deny_write.append(dp_path.resolve())

    def set_project_root(self, project_root: str | Path) -> None:
        """切换 workspace 根（例如进入/退出 git worktree）。

        权限层的 root 必须和 Agent.work_dir、工具根保持同一个 canonical 路径，
        否则会出现“工具在 worktree 里写，权限层还在校验主仓库”的分裂。
        """
        root = Path(project_root).resolve()
        self._allowed_roots[0] = root
        self._rebuild_deny_write()


    @property
    def project_root(self) -> Path:
        return self._allowed_roots[0]


    def approve_dir(self, path: str) -> None:
        """会话级放行：用户对某个文件点了 "Allow Always"，则它的**父目录**
        在本会话内视为已授权。

        动机：同一批文件操作（读一个、写几个）以前每个文件都会重新弹窗，
        因为放行记录是精确路径。这里把粒度放"一个目录"，但仍满足：
          * 仅当前进程/会话有效，不落盘、不跨会话；
          * 禁写清单（``*_DENY_WRITE``）优先，配置文件仍然不可写；
          * 不会因为放行 workspace 就放开整个磁盘。
        """
        if not path:
            return
        try:
            target = Path(path).expanduser()
            if not target.is_absolute():
                target = self.project_root / target
            parent = target.parent.resolve()
        except (OSError, ValueError):
            return
        if parent not in self._session_approved:
            self._session_approved.append(parent)

    @property
    def session_approved_dirs(self) -> list[Path]:
        return list(self._session_approved)


    def _is_deny_write(self, real_path: Path) -> bool:
        """检查路径是否命中禁写列表。

        支持目录前缀匹配：如果禁写项以 / 结尾或本身是目录，
        则该目录下的所有文件都被禁止写入。
        """
        for deny_path in self._deny_write:
            # 精确匹配
            if real_path == deny_path:
                return True
            # 目录前缀匹配
            try:
                real_path.relative_to(deny_path)
                return True
            except ValueError:
                continue
        return False


    def check(self, path: str) -> tuple[bool, str]:
        p = Path(path).expanduser()
        if not p.is_absolute():
            p = self.project_root / p
        abs_path = p.absolute()

        try:
            real_path = abs_path.resolve(strict=True)
        except OSError:
            ancestor = abs_path
            while not ancestor.exists():
                parent = ancestor.parent
                if parent == ancestor:
                    return False, f"无法解析路径: {path}"
                ancestor = parent
            try:
                resolved_ancestor = ancestor.resolve(strict=True)
            except OSError:
                return False, f"无法解析路径: {path}"
            real_path = resolved_ancestor / abs_path.relative_to(ancestor)

        # 禁写检查优先于允许检查
        if self._is_deny_write(real_path):
            return False, f"路径 {path} 在禁写列表中"

        for root in self._allowed_roots:
            try:
                real_path.relative_to(root)
                return True, ""
            except ValueError:
                continue

        # 用户在本会话里显式放行过的目录（Allow Always）
        for approved in self._session_approved:
            try:
                real_path.relative_to(approved)
                return True, ""
            except ValueError:
                continue

        return False, f"路径 {path} 超出沙箱范围"
