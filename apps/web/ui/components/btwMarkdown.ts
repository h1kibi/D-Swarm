import { createElement } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

export type BtwMessageRole = "user" | "assistant";

export interface BtwMessageBodyProps {
  role: BtwMessageRole;
  content: string;
  streaming: boolean;
}

export function BtwMessageBody({
  role,
  content,
  streaming,
}: BtwMessageBodyProps) {
  const text = content || (role === "assistant" && streaming ? "…" : "");

  if (role === "user") {
    return createElement("span", { className: "btw-plain" }, text);
  }

  return createElement(
    "div",
    { className: "btw-markdown" },
    createElement(ReactMarkdown, {
      remarkPlugins: [remarkGfm],
      components: {
        a: ({ node: _node, ...props }) =>
          createElement("a", { ...props, target: "_blank", rel: "noopener noreferrer" }),
      },
    }, text),
  );
}
