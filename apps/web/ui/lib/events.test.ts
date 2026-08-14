/**
 * Reducer smoke tests — the regression floor for the event-normalizer work
 * (docs/07 Phase 1). The reducer must stay total: old sessions (missing
 * fields) and future kernels (unknown event types / extra payload fields)
 * must fold without throwing.
 */
import { describe, expect, it } from "vitest";
import { emptyDeck, EventType, reduce, type DSwarmEvent } from "./events";

function ev(
  event_type: EventType | string,
  payload: Record<string, unknown> = {},
  over: Partial<DSwarmEvent> = {},
): DSwarmEvent {
  return {
    event_type: event_type as EventType,
    seq: 1,
    ts: 1723000000,
    run_id: "run-test",
    payload,
    ...over,
  };
}

describe("reduce", () => {
  it("folds run.started into an opened thread", () => {
    const s0 = emptyDeck("run-test");
    const s1 = reduce(
      s0,
      ev(EventType.RUN_STARTED, {
        challenge: {
          name: "web-042",
          category: "web",
          target: "https://example.test/",
          expected_flags: 2,
          multi_flag: true,
        },
      }),
    );
    expect(s1.started).toBe(true);
    expect(s1.challengeName).toBe("web-042");
    expect(s1.expectedFlags).toBe(2);
    expect(s1.multiFlag).toBe(true);
  });

  it("folds worker.status into a lane", () => {
    const s0 = emptyDeck("run-test");
    const s1 = reduce(
      s0,
      ev(
        EventType.WORKER_STATUS,
        { online: true, status: "online" },
        { solver_id: "pi-worker-1" },
      ),
    );
    expect(s1.lanes["pi-worker-1"]?.online).toBe(true);
  });

  it("tolerates missing payload fields (legacy sessions)", () => {
    const s0 = emptyDeck("run-test");
    expect(() =>
      reduce(s0, ev(EventType.RUN_STARTED, {})),
    ).not.toThrow();
    expect(() =>
      reduce(s0, ev(EventType.WORKER_STATUS, {})),
    ).not.toThrow();
    expect(() =>
      reduce(s0, ev(EventType.BLACKBOARD_DELTA, {})),
    ).not.toThrow();
  });

  it("ignores unknown event types (forward compatibility)", () => {
    const s0 = emptyDeck("run-test");
    let s1 = s0;
    expect(() => {
      s1 = reduce(s0, ev("some.future.event", { whatever: 1 }));
    }).not.toThrow();
    // state may be re-copied by the reducer, but nothing may change
    expect(s1.started).toBe(s0.started);
    expect(s1.challengeName).toBe(s0.challengeName);
    expect(Object.keys(s1.lanes)).toHaveLength(0);
  });

  it("surfaces provider diagnostics and batch alerts in the operator chat", () => {
    let s = emptyDeck("run-test");
    s = reduce(
      s,
      ev(
        EventType.PROVIDER_ERROR,
        {
          category: "insufficient_quota",
          severity: "fatal",
          should_pause_dispatch: true,
          provider: "deepseek",
          account_id: "pi-main",
          user_message: "quota exhausted",
          suggested_action: "recharge or switch account",
        },
        { solver_id: "worker-a" },
      ),
    );
    expect(s.chat.at(-1)?.content).toContain("insufficient_quota");
    expect(s.chat.at(-1)?.content).toContain("建议暂停继续派发");
    expect(s.lanes["worker-a"]?.status).toBe("provider_error");

    s = reduce(
      s,
      ev(EventType.PROVIDER_BATCH_ALERT, {
        category: "insufficient_quota",
        count: 3,
        affected_workers: 3,
        active_workers: 4,
        should_pause_dispatch: true,
      }),
    );
    expect(s.chat.at(-1)?.content).toContain("系统性 LLM 错误告警");
    expect(s.chat.at(-1)?.content).toContain("3 次");
    expect(s.chat.at(-1)?.content).toContain("暂停派发");
  });


  it("surfaces provider recovery blackboard directives with operator-readable next-worker semantics", () => {
    let s = emptyDeck("run-test");
    s = reduce(
      s,
      ev(EventType.BLACKBOARD_DELTA, {
        kind: "provider_recovery_directive",
        actor: "worker-a",
        worker: "worker-a",
        category: "transient_network",
        recovery_action: "retry_next_worker",
        current_worker_interrupted: false,
        operator_message: "当前 worker 是 single-shot，本轮自然结束；恢复指令已写入黑板，下一个 Worker/Reason 会消费。",
      }),
    );

    expect(s.blackboard.events.at(-1)?.label).toContain("single-shot");
    expect(s.blackboard.events.at(-1)?.label).toContain("下一个 Worker");
    expect(s.chat.at(-1)?.content).toContain("single-shot");
    expect(s.chat.at(-1)?.content).toContain("下一个 Worker/Reason 会消费");

    s = reduce(
      s,
      ev(EventType.BLACKBOARD_DELTA, {
        kind: "worker_recovery_scheduled",
        actor: "reason",
        worker: "worker-b",
        intent_id: "intent-1",
        category: "timeout",
        attempt: 1,
        max_attempts: 2,
      }),
    );
    expect(s.blackboard.events.at(-1)?.label).toContain("自动恢复");
    expect(s.blackboard.events.at(-1)?.label).toContain("1/2");
  });

  it("surfaces fatal provider blackboard alerts even when only blackboard deltas arrive", () => {
    let s = emptyDeck("run-test");
    s = reduce(
      s,
      ev(EventType.BLACKBOARD_DELTA, {
        kind: "provider_dispatch_paused",
        actor: "reason",
        profile: "deepseek-main",
        category: "insufficient_quota",
        provider: "deepseek",
      }),
    );
    expect(s.blackboard.events.at(-1)?.label).toContain("暂停派发");
    expect(s.chat.at(-1)?.content).toContain("暂停派发");
    expect(s.chat.at(-1)?.content).toContain("insufficient_quota");

    s = reduce(
      s,
      ev(EventType.BLACKBOARD_DELTA, {
        kind: "worker_recovery_exhausted",
        actor: "reason",
        worker: "worker-a",
        intent_id: "intent-2",
        category: "rate_limited",
        attempts: 2,
      }),
    );
    expect(s.blackboard.events.at(-1)?.label).toContain("恢复重试已用尽");
    expect(s.chat.at(-1)?.content).toContain("恢复重试已用尽");

    s = reduce(
      s,
      ev(EventType.BLACKBOARD_DELTA, {
        kind: "provider_batch_alert",
        actor: "reason",
        category: "insufficient_quota",
        count: 3,
        affected_workers: 3,
        active_workers: 4,
        should_pause_dispatch: true,
      }),
    );
    expect(s.blackboard.events.at(-1)?.label).toContain("批量错误告警");
    expect(s.chat.at(-1)?.content).toContain("系统性 LLM 错误告警");
    expect(s.chat.at(-1)?.content).toContain("额度/认证/模型配置");
  });

});
