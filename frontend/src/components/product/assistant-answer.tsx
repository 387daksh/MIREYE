import type { ReactNode } from "react";

type Block = { type: "paragraph" | "bullets" | "numbered"; items: string[] };

export function parseAssistantAnswer(text: string): Block[] {
  const blocks: Block[] = [];

  for (const rawLine of text.trim().split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line) continue;
    const bullet = line.match(/^[-*]\s+(.+)$/);
    const numbered = line.match(/^\d+[.)]\s+(.+)$/);
    const type = bullet ? "bullets" : numbered ? "numbered" : "paragraph";
    const content = bullet?.[1] ?? numbered?.[1] ?? line;
    const previous = blocks.at(-1);

    if (type !== "paragraph" && previous?.type === type) previous.items.push(content);
    else blocks.push({ type, items: [content] });
  }

  return blocks;
}

function Inline({ text }: { text: string }) {
  return text.split(/(\*\*[^*]+\*\*)/g).map((part, index): ReactNode =>
    part.startsWith("**") && part.endsWith("**") ? (
      <strong key={index} className="font-semibold text-mi-fg-strong">{part.slice(2, -2)}</strong>
    ) : part,
  );
}

export function AssistantAnswer({ text }: { text: string }) {
  return (
    <div aria-live="polite" className="space-y-3 text-[12px] leading-relaxed text-mi-fg">
      {parseAssistantAnswer(text).map((block, index) => {
        if (block.type === "paragraph") return <p key={index}><Inline text={block.items[0]} /></p>;
        const List = block.type === "numbered" ? "ol" : "ul";
        return (
          <List key={index} className={`${block.type === "numbered" ? "list-decimal" : "list-disc"} space-y-1 pl-5`}>
            {block.items.map((item, itemIndex) => <li key={itemIndex}><Inline text={item} /></li>)}
          </List>
        );
      })}
    </div>
  );
}
