import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { BtwMessageBody } from "./btwMarkdown";
import { applyBtwAnswerFrame, btwErrorBannerText } from "./btwStream";

function renderBody(
  role: "user" | "assistant",
  content: string,
  streaming = false,
) {
  return renderToStaticMarkup(
    createElement(BtwMessageBody, { role, content, streaming }),
  );
}

describe("BtwMessageBody", () => {
  it("renders assistant Markdown formatting", () => {
    const html = renderBody("assistant", "**verified**\n\n- one\n- two");

    expect(html).toContain("<strong>verified</strong>");
    expect(html).toContain("<ul>");
    expect(html).toContain("<li>one</li>");
  });

  it("renders fenced code blocks", () => {
    const html = renderBody("assistant", "```bash\ncurl http://target\n```");

    expect(html).toContain("<pre><code class=\"language-bash\">");
    expect(html).toContain("curl http://target");
  });

  it("supports GFM tables", () => {
    const html = renderBody("assistant", "| 状态 | 结果 |\n| --- | --- |\n| A | 通过 |");

    expect(html).toContain("<table>");
    expect(html).toContain("<th>状态</th>");
    expect(html).toContain("<td>通过</td>");
  });

  it("keeps user messages as plain text", () => {
    const html = renderBody("user", "**not markdown**");

    expect(html).toContain("**not markdown**");
    expect(html).not.toContain("<strong>");
  });

  it("does not render raw HTML", () => {
    const html = renderBody("assistant", "<script>alert('xss')</script>");

    expect(html).not.toContain("<script>");
    expect(html).toContain("&lt;script&gt;");
  });
});


describe("BTW SSE answer state", () => {
  it("lets final replace provisional deltas and ignores late duplicate deltas", () => {
    let state = { content: "", final: false, error: "" };
    state = applyBtwAnswerFrame(state, { delta: "重复草稿" });
    state = applyBtwAnswerFrame(state, { final: "最终 **答案**" });
    state = applyBtwAnswerFrame(state, { delta: "重复草稿" });
    expect(state.content).toBe("最终 **答案**");
    expect(state.final).toBe(true);
  });

  it("turns an error into readable assistant content when no final exists", () => {
    const state = applyBtwAnswerFrame(
      { content: "", final: false, error: "" },
      { error: "认证失败" },
    );
    expect(state.content).toBe("认证失败");
    expect(state.error).toBe("认证失败");
  });
});


it("does not render the same stream error in both answer and error banner", () => {
  expect(btwErrorBannerText("观察员暂时无法完成只读总结：", "观察员暂时无法完成只读总结：")).toBe("");
  expect(btwErrorBannerText("观察员暂时无法回答", "认证失败")).toBe("认证失败");
});
