# Target / Run UI 视觉模板

> 推荐：Template A「Quiet Command Center」作为默认。三套模板都遵守 Target → Direction → Run 的层级，复杂数据进入详情页。

## 共同设计原则

- 深色安全指挥台为默认，浅色主题保持结构一致；
- 无网格背景，使用纯色层级、细边框、状态色和留白建立秩序；
- 主屏最多突出一个 Target、一个 selected Run 和一个主要用户动作；
- Target 卡片是父对象，Run 卡片缩进或连接显示从属关系；
- solved、running、waiting、blocked、degraded 使用稳定且可读的状态语义；
- 主区显示摘要，Inspector 显示 binding / usage / health，Detail 显示完整证据。

## Template A — Quiet Command Center（推荐）

### 结构

```text
┌──────────────────────────────────────────────────────────────┐
│ top bar: D-Swarm / Target count / running / token / cost     │
├──────────────┬───────────────────────────────┬───────────────┤
│ Target Rail  │ Target Workspace               │ Run Inspector │
│              │ target header                  │ status        │
│ target card  │ relationship map               │ roles         │
│   run stack  │ selected run summary           │ usage         │
│              │ decision feed                  │ health        │
├──────────────┴───────────────────────────────┴───────────────┤
│ command / guidance bar                                        │
└──────────────────────────────────────────────────────────────┘
```

### 视觉

- 深色背景 `#0d1117`，面板 `#151b24`，边框低对比度；
- accent 使用冷青或蓝绿，仅标记 active、verified 和 focus；
- danger 只用于 blocked / provenance failure；
- 圆角中等，阴影弱，避免“游戏 HUD”感；
- 适合单用户高频观察和 1–5 个并行 Run。

### 优点

信息密度、可读性和详情入口平衡最好；Target 与 Run 的父子关系一眼可见，不会让主页面变成日志墙。

## Template B — Evidence Ledger

### 结构

```text
Target header
├── status / solved / primary direction
├── verified fact ledger
└── Runs as chronological evidence rows
    ├── run id / direction / status
    ├── worker progress
    ├── cost / token / elapsed
    └── open detail
```

### 视觉

白色或浅灰背景、纸张式分组、细竖线连接事件。适合复盘、写 writeup 和核对 provenance；不如 Template A 适合实时 swarm 指挥。可作为浅色主题或未来 Review workspace。

## Template C — Focused Run Canvas

### 结构

```text
Target selector / run switcher
┌────────────────────────────────────────────┐
│ selected Run                               │
│ workers + current intent                   │
│ decision timeline                           │
│ evidence highlights                         │
└────────────────────────────────────────────┘
side drawer: target facts / other runs / binding
```

### 视觉

大留白、宽主画布、少量高亮；将 Target 和其他 Run 收进可展开 drawer。适合专注一个 Run 长时间观察，但多 Run 从属关系不如 Template A 明确。

## 推荐采用方式

- 默认：Template A；
- 浅色主题：沿用 Template A 的结构，借鉴 Template B 的高对比度和 ledger 分组；
- Run Detail：采用 Template C 的宽画布，但保留顶部 Target breadcrumb 和 Run binding inspector；
- 不把完整 graph、raw output、ledger 全部塞进主 UI；它们必须经由明确详情入口展开。

## 交互状态

1. 未选 Target：显示 Target Library 和 Create Target；
2. Target 无 Run：显示简短说明和 Start first Run；
3. Target 多 Run：按 Direction 分组，selected Run 使用 accent outline；
4. Run waiting for help：在 Target 卡片和 Inspector 显示 attention badge；
5. Run blocked：显示安全错误摘要和建议动作，不显示 raw exception；
6. legacy Run：显示 Legacy / binding unavailable，允许 replay；
7. solved：Target header 显示 solved，但仍保留所有 Run 和 provenance 链。


## Implementation status (2026-09-05)

Template A (Quiet Command Center) is the current implementation baseline. Target
Library, Target Workspace, Run Launch Wizard, Run Inspector and Run Detail are
implemented. Templates B/C remain design references until an evidence-review or
focused-run workflow requires them; do not maintain three parallel shells.
