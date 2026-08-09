# 安全模型与评测

## 安全定位

本项目是攻击性安全自动化工具，只允许用于：

- 明确授权的 CTF。
- 自己拥有的靶场。
- 有书面授权的渗透测试。

不支持对未授权目标扫描、测试或攻击。

## 威胁模型

主要威胁：

1. 恶意 CTF challenge 试图攻击宿主。
2. 模型产生危险命令。
3. 模型把幻觉 flag 当成解题成功。
4. 模型把题目附件、flag、key 上传到第三方。
5. 多个 worker 并发攻击同一目标导致碰撞或资源耗尽。

## Docker 沙箱要求

### 镜像

每个题型独立镜像：

```text
# BTFly stage images - build-time ONLY. The worker base Dockerfile extracts
# the pi runtime + category skills from these; they are rebuilt by
# ./docker/worker-pi/build-base.sh and removed again after ./build.sh.
ctf-agent-pi-base:0.1.0
ctf-agent-pi-web:0.1.0
ctf-agent-pi-crypto:0.1.0
ctf-agent-pi-pwn:0.1.0
ctf-agent-pi-reverse:0.1.0
ctf-agent-pi-forensics:0.1.0
ctf-agent-pi-misc:0.1.0

# Runtime worker images (per-direction, from ./docker/worker-pi/build.sh):
ctf-swarm-pi-base:0.2.0
ctf-swarm-pi-web:0.2.0
ctf-swarm-pi-pwn:0.2.0
ctf-swarm-pi-rev:0.2.0
ctf-swarm-pi-crypto:0.2.0
ctf-swarm-pi-misc:0.2.0
ctf-swarm-pi-forensics:0.2.0
ctf-swarm-pi-aisec:0.2.0
```

### 容器配置

推荐：

- 非 root 用户 `ctf`。
- drop all capabilities。
- no-new-privileges。
- 默认 seccomp。
- memory / cpu / pids 限制。
- 只挂载 task workspace。
- 不暴露端口。
- network 默认 bridge，不直接使用 host。
- Pwn 题若需要调试，再加 `SYS_PTRACE`。
- 优先 gVisor，Pwn 可优先 Kata，没有时显式警告。

### rootfs

第一版可以保留可写 rootfs，以便 worker 安装临时工具，但建议：

- `/workspace` 可写。
- `/tmp` 使用 tmpfs。
- `/home/ctf` 使用独立可写目录。
- 系统目录尽量只读。

如果镜像已经内置完整工具链，应开启 `ReadonlyRootfs`。

## 模型网关安全

- 真实 API key 只存在于宿主 daemon。
- 每个 task 签发随机短期 token。
- 容器请求 `/model` 必须携带 task token。
- daemon 校验 token 后替换为真实 upstream key。
- 前端永不显示真实 key。
- 日志不记录 prompt / response 原文，只记录用量。

## 共享图安全

- `graph.db` 由 daemon 以 0600 权限创建。
- worker 通过 bind mount 访问，不暴露到宿主管网。
- worker 写入事实必须带 actor / source。
- `--verified` 只能由 daemon 根据真实证据确认。
- 共享图不保存真实凭据明文，除非题目要求并已明确授权。

## 命令安全

daemon 在 Pi RPC `tool_call` 阶段可检查命令：

```text
block:
- rm -rf /
- ssh / scp 到任意主机
- 对非授权目标扫描
- 安装后门
- 外传题目附件
```

建议不要只依赖 prompt，而是：

- Pi extension hook 检查 tool call。
- daemon 在收到 RPC tool_call 时记录并审计。
- 对高风险命令默认 ask。
- 对 worker 设置 allowlist / deny list。

## Flag 防污染

不接受的 flag 来源：

- 模型最终回复里的纯文本 flag。
- `FOUND_FLAG=` marker 但无真实命令输出。
- `final-result.json` 但无 evidence。
- 占位符 `flag{...}`、`{uuid}`、`<flag>`。

只接受：

- 匹配 flag format。
- 非占位符。
- 逐字出现在真实 command output / artifact content 中。

## 评测目标

### 第一版评测集

- NYU CTF Bench：200 道 CSAW 题，覆盖面广。
- CTFTiny：轻量回归。
- PicoCTF 本地题：快速 smoke test。
- OverTheWire：适合验证命令型 agent。

### 评测指标

- solve rate
- Pass@1
- median solve time
- total cost
- total tokens
- winner per category
- winner per engine / Pi agent
- false positive flag count
- dead-end reuse rate

### 离线模式

能力评测必须关闭：

- WebSearch
- WebFetch
- 公开 writeup 搜索

真实比赛可以打开联网，但 flag 仍必须经过 provenance gate。

## 评测目录

```text
eval/
├── harness/
│   ├── runner.go
│   └── oracle.go
├── nyu/
│   └── challenges.json
├── pico/
│   └── local.json
├── results/
│   └── latest/
└── traces/
    └── task_xxx/
```

## 评测流程

```text
load challenge metadata
  -> create task
  -> run agent
  -> collect graph + RPC trace
  -> submit to oracle
  -> write result row
```

## 发布前检查

1. 单元测试通过。
2. 基准评测无回归。
3. 无真实 key / token / cookie 进入 git。
4. 无真实 flag 进入日志。
5. 容器配置不暴露宿主敏感目录。
6. 文档明确授权边界。

## 风险清单

| 风险 | 缓解 |
|---|---|
| 恶意题目逃逸 | 非 root、cap drop、gVisor/Kata、资源限制 |
| 模型幻觉 flag | provenance gate |
| worker 撞车 | intent claim、resource lock |
| 成本失控 | max worker、soft pause、cost budget |
| Pi 版本升级破坏 RPC | 固定 Pi 版本，单独测试升级 |
| BTFly 源码授权不明确 | 只借鉴设计，不复制有争议代码 |
| Muteki AGPL 传染 | 如果直接复制 Muteki 代码，项目可能受 AGPL 约束 |

## 结论

第一版的最小安全目标不是“绝对隔离”，而是：

- 不在生产主机上直接跑恶意 challenge。
- 不让 worker 访问真实模型 key。
- 不让模型把幻觉 flag 当成功。
- 不让多个 worker 无约束重复攻击同一目标。
