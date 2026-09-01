import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { AssistantAnswer } from "./assistant-answer";

describe("AssistantAnswer", () => {
  it("renders agent markdown as readable paragraphs, lists, and emphasis", () => {
    const html = renderToStaticMarkup(
      <AssistantAnswer text={"Power is **blocked**.\n\n- 100 MW Phase 1\n- 300 MW expansion\n\n1. Ask the utility\n2. Review the response"} />,
    );

    expect(html).toContain("<strong");
    expect(html).toContain(">blocked</strong>");
    expect(html).toContain("<ul");
    expect(html).toContain("<ol");
    expect(html.match(/<li/g)).toHaveLength(4);
    expect(html).not.toContain("**");
  });
});
