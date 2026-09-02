#!/usr/bin/env python3
"""删除 Legacy Docker-Exec Backend 和 Identity Migration Layer"""

def delete_legacy_dockerexec():
    """删除 Legacy Docker-Exec Backend (477行)"""
    
    with open('dswarm/solver/container_exec.py', 'r', encoding='utf-8') as f:
        lines = f.readlines()
    
    # 标记要删除的行（基于Agent 3的分析）
    delete_lines = set()
    
    # 1. _oom_kill_count() 函数: 行750-772 (23行)
    delete_lines.update(range(749, 772))  # 0-based
    
    # 2. _ContainerProc 类: 行952-996 (45行)
    delete_lines.update(range(951, 996))
    
    # 3. _DockerExecBackend 类: 行998-1231 (234行)
    delete_lines.update(range(997, 1231))
    
    # 保留未删除的行
    new_lines = [line for i, line in enumerate(lines) if i not in delete_lines]
    
    # 写入新内容
    content = ''.join(new_lines)
    
    # 文本替换清理
    import re
    
    # 移除 LEGACY 段落说明（模块文档字符串中）
    content = re.sub(
        r'LEGACY fallback.*?see `_DockerExecBackend`\.\n\n',
        '',
        content,
        flags=re.DOTALL
    )
    
    # 移除 _USE_DOCKEREXEC 标志定义
    content = re.sub(
        r'_BACKEND = .*?\n_USE_DOCKEREXEC = _BACKEND == "container_dockerexec"\n',
        '',
        content
    )
    
    # 替换 mode 赋值为固定 rcp
    content = content.replace(
        'mode = "dockerexec" if _USE_DOCKEREXEC else "rcp"',
        'mode = "rcp"'
    )
    
    # 移除 dockerexec 条件分支（行647-649: sleep infinity）
    content = re.sub(
        r'        if mode == "dockerexec":\n.*?cmd \+= \["sleep", "infinity"\]\n        else:\n',
        '',
        content,
        flags=re.DOTALL
    )
    
    # 移除 run_cli_container 中的 _DockerExecBackend.run 调用
    content = re.sub(
        r'    if handle\.mode == "rcp":\n(.*?)        return CliResult\([^)]+\)\n    return _DockerExecBackend\.run\([^)]+\)',
        r'    from dswarm.solver.control_client import run_cli_rcp\n\1        return CliResult(returncode=res.exit_code or 0, raw_stdout=res.stdout, raw_stderr=res.stderr, timed_out=res.timed_out, error=(res.raw_stderr or "").strip()[:300])',
        content,
        flags=re.DOTALL
    )
    
    # 移除 run_cli_streaming_container 中的 _DockerExecBackend.run_streaming 调用
    content = re.sub(
        r'    if handle\.mode == "rcp":\n(.*?)        return res\n    return _DockerExecBackend\.run_streaming\([^)]+\)',
        r'    from dswarm.solver.control_client import run_cli_streaming_rcp\n\1        return res',
        content,
        flags=re.DOTALL
    )
    
    # 清理 ContainerHandle 文档字符串
    content = content.replace('# "rcp" | "dockerexec"', '# always "rcp"')
    content = content.replace(
        'mode; `sleep infinity` in legacy dockerexec mode',
        'mode'
    )
    
    # 移除 LEGACY backend 注释段
    content = re.sub(
        r'# ── LEGACY docker-exec backend.*?\n\n',
        '',
        content,
        flags=re.DOTALL
    )
    
    with open('dswarm/solver/container_exec.py', 'w', encoding='utf-8') as f:
        f.write(content)
    
    print(f"✅ 删除了 Legacy Docker-Exec Backend")
    print(f"✅ 原始行数: {len(lines)}")
    print(f"✅ 删除行数: {len(delete_lines)}")
    print(f"✅ 新行数: {len(content.splitlines())}")


def delete_identity_migration():
    """删除 Identity Migration Layer (280行)"""
    
    with open('dswarm/solver/identity_model.py', 'r', encoding='utf-8') as f:
        lines = f.readlines()
    
    # 删除行189-470 (~280行)
    delete_lines = set(range(188, 470))  # 0-based
    
    new_lines = [line for i, line in enumerate(lines) if i not in delete_lines]
    
    with open('dswarm/solver/identity_model.py', 'w', encoding='utf-8') as f:
        f.writelines(new_lines)
    
    print(f"✅ 删除了 Identity Migration Layer")
    print(f"✅ 原始行数: {len(lines)}")
    print(f"✅ 删除行数: {len(delete_lines)}")
    print(f"✅ 新行数: {len(new_lines)}")


if __name__ == '__main__':
    print("=== 开始阶段2删除 ===\n")
    
    print("1. 删除 Legacy Docker-Exec Backend...")
    delete_legacy_dockerexec()
    
    print("\n2. 删除 Identity Migration Layer...")
    delete_identity_migration()
    
    print("\n=== 删除完成 ===")
    print("总计删除: ~757行")
