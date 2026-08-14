import { afterEach, describe, expect, it, vi } from "vitest";
import { startStartupTest } from "../lib/useRun";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("startStartupTest", () => {
  it("posts to /api/startup-test and returns the test id", async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      expect(String(_input)).toBe("/api/startup-test");
      expect(init?.method).toBe("POST");
      return {
        ok: true,
        status: 200,
        json: async () => ({ test_id: "startup-test-1" }),
      } as Response;
    });
    vi.stubGlobal("fetch", fetchMock);

    await expect(startStartupTest()).resolves.toBe("startup-test-1");
  });



  it("posts full-flow mode and benchmark when requested", async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      expect(String(_input)).toBe("/api/startup-test");
      expect(init?.method).toBe("POST");
      expect(new Headers(init?.headers).get("Content-Type")).toBe("application/json");
      expect(init?.body).toBe(JSON.stringify({ mode: "full_flow", benchmark: "local-smoke" }));
      return {
        ok: true,
        status: 200,
        json: async () => ({ test_id: "full-flow-1" }),
      } as Response;
    });
    vi.stubGlobal("fetch", fetchMock);

    await expect(startStartupTest({ mode: "full_flow", benchmark: "local-smoke" })).resolves.toBe("full-flow-1");
  });

  it("throws when the backend does not return a test id", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => ({
      ok: true,
      status: 200,
      json: async () => ({}),
    } as Response)));

    await expect(startStartupTest()).rejects.toThrow("did not return an id");
  });
});

  it("opens startup test events with an auth ticket", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      expect(String(input)).toBe("/api/auth/ticket");
      expect(init?.method).toBe("POST");
      return {
        ok: true,
        status: 200,
        json: async () => ({ ticket: "ticket-1" }),
      } as Response;
    });
    vi.stubGlobal("fetch", fetchMock);

    const opened: string[] = [];
    vi.stubGlobal("EventSource", class {
      constructor(url: string) {
        opened.push(url);
      }
      close() {}
    });

    const { openStartupTestEvents } = await import("../lib/useRun");
    const es = await openStartupTestEvents("startup-test-1");

    expect(es).toBeTruthy();
    expect(opened).toEqual(["/api/startup-test/startup-test-1/events?ticket=ticket-1"]);
  });


describe("startup test presentation", () => {
  it("formats provider batch alerts and the hint directive semantics", async () => {
    const {
      startupTestAlertHeading,
      startupTestAlertTone,
      startupTestAlertMeta,
      startupTestHintDeliveryCopy,
      startupTestModeTitle,
      startupTestModeDescription,
      startupTestWorkerPhaseLabel,
      startupTestWorkerDetail,
      startupTestCheckLabel,
      startupTestStatusLabel,
    } = await import("../lib/startupTestPresentation");

    const alert = {
      type: "provider.batch_alert",
      seq: 7,
      provider: "deepseek",
      account_id: "main",
      category: "insufficient_quota",
      retryable: false,
      should_pause_dispatch: true,
    } as const;

    expect(startupTestAlertHeading(alert)).toBe("批量 LLM 错误");
    expect(startupTestAlertTone(alert)).toBe("bad");
    expect(startupTestAlertMeta(alert)).toContain("deepseek");
    expect(startupTestAlertMeta(alert)).toContain("account=main");
    expect(startupTestAlertMeta(alert)).toContain("建议暂停派发");
    expect(startupTestHintDeliveryCopy()).toContain("立即写入黑板 directive");
    expect(startupTestHintDeliveryCopy()).toContain("下一个 Worker/intent 会消费");

    expect(startupTestModeTitle("startup")).toBe("快速启动测试");
    expect(startupTestModeTitle("full_flow")).toBe("完整流程演练");
    expect(startupTestModeDescription("full_flow")).toContain("Reason、黑板、BTW、停止、提示、恢复");
    expect(startupTestWorkerPhaseLabel("done", true)).toBe("已通过");
    expect(startupTestWorkerPhaseLabel("launching", null)).toBe("启动中");
    expect(startupTestWorkerDetail("startup_test_ok")).toBe("启动链路已验证");
    expect(startupTestCheckLabel("blackboard.checked")).toBe("知识黑板");
    expect(startupTestCheckLabel("resume.checked")).toBe("恢复解题");
    expect(startupTestStatusLabel({ busy: true, passed: 2, total: 8, failed: 0 })).toBe("运行中 · 2/8");
    expect(startupTestStatusLabel({ busy: false, passed: 8, total: 8, failed: 0 })).toBe("8/8 通过");
    expect(startupTestStatusLabel({ busy: false, passed: 6, total: 8, failed: 2 })).toBe("2 项失败");
  });

  it("formats realtime SSE events as readable timeline items", async () => {
    const {
      startupTestEventDetail,
      startupTestEventLabel,
      startupTestEventSubject,
      startupTestEventTime,
      startupTestEventTone,
    } = await import("../lib/startupTestPresentation");

    expect(startupTestEventLabel({ type: "test.started" })).toBe("测试开始");
    expect(startupTestEventLabel({ type: "worker.phase", ok: true })).toBe("Worker 通过");
    expect(startupTestEventLabel({ type: "worker.phase", phase: "cleanup" })).toBe("清理收尾");
    expect(startupTestEventLabel({ type: "flow.check", ok: false })).toBe("流程失败");
    expect(startupTestEventLabel({ type: "provider.batch_alert", should_pause_dispatch: true })).toBe("批量告警");
    expect(startupTestEventTone({ type: "provider.batch_alert", should_pause_dispatch: true })).toBe("bad");
    expect(startupTestEventTone({ type: "provider.error", retryable: true })).toBe("warn");
    expect(startupTestEventSubject({ check_id: "blackboard.checked" })).toBe("知识黑板");
    expect(startupTestEventSubject({ worker_id: "web-1", check_id: "blackboard.checked" })).toBe("web-1");
    expect(startupTestEventDetail({ detail: "startup_test_ok" })).toBe("启动链路已验证");
    expect(startupTestEventDetail({ user_message: "余额不足", suggested_action: "暂停派发" })).toBe("余额不足 · 暂停派发");
    expect(startupTestEventTime({ ts: 1_725_000_000 })).toMatch(/^\d{2}:\d{2}:\d{2}$/);
    expect(startupTestEventTime({})).toBe("--:--:--");
  });

});
