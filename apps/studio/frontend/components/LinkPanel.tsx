import type { WorkerLinkEdge } from "@/lib/types";
import Badge from "@/components/Badge";

export interface LinkPanelProps {
  edge: WorkerLinkEdge;
}

export default function LinkPanel({ edge }: LinkPanelProps) {
  const isCode = edge.mode === "code";

  return (
    <div className="flex flex-col gap-3 border border-neutral-800 bg-neutral-950 p-4">
      {/* Source → Target + mode badge */}
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-mono text-sm font-medium text-neutral-200">{edge.source}</span>
        <span className="text-neutral-500" aria-hidden>
          →
        </span>
        <span className="font-mono text-sm font-medium text-neutral-200">{edge.target}</span>
        <Badge tone={isCode ? "pending" : "ok"}>{isCode ? "code" : "simple"}</Badge>
      </div>

      {/* Simple mode: condition + field map */}
      {!isCode ? (
        <>
          {edge.condition ? (
            <div>
              <span className="text-xs uppercase text-neutral-500">condition</span>
              <p className="mt-0.5 rounded border border-neutral-800 bg-neutral-900 px-2 py-1 font-mono text-xs text-neutral-300">
                {edge.condition}
              </p>
            </div>
          ) : null}

          {edge.map && Object.keys(edge.map).length > 0 ? (
            <div>
              <span className="text-xs uppercase text-neutral-500">field map</span>
              <table className="mt-1 w-full text-xs">
                <thead>
                  <tr className="border-b border-neutral-800 text-left text-neutral-500">
                    <th className="pb-1 pr-3 font-normal">from</th>
                    <th className="pb-1 font-normal">to</th>
                  </tr>
                </thead>
                <tbody>
                  {Object.entries(edge.map).map(([from, to]) => (
                    <tr key={from} className="border-b border-neutral-900">
                      <td className="py-1 pr-3 font-mono text-neutral-300">{from}</td>
                      <td className="py-1 font-mono text-neutral-300">{to}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : null}
        </>
      ) : null}

      {/* Code mode: handler code block */}
      {isCode && edge.handler ? (
        <div>
          <span className="text-xs uppercase text-neutral-500">handler</span>
          <pre className="mt-1 max-h-48 overflow-auto rounded border border-neutral-800 bg-neutral-900 p-3 text-xs leading-5 text-neutral-400 whitespace-pre-wrap">
            {edge.handler}
          </pre>
        </div>
      ) : null}
    </div>
  );
}
