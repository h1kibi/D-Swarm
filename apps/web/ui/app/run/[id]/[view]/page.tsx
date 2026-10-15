import { notFound } from "next/navigation";
import { isDetailView, parseRunPath } from "@/lib/runRoute";
import { RunDetailPage } from "@/components/RunDetailPage";

/**
 * /run/<id>/<view> — a run detail view as a real, bookmarkable page.
 * The view segment is whitelisted; anything else is a 404. The run id comes
 * from the path at runtime (client component), so we only validate the shape
 * server-side here. Next.js 15: params is a Promise in server components.
 */
export function generateStaticParams() {
  return [];
}

export default async function RunViewPage({
  params,
}: {
  params: Promise<{ id: string; view: string }>;
}) {
  const { id, view } = await params;
  if (!id || !view || !isDetailView(view)) notFound();
  if (!parseRunPath(`/run/${encodeURIComponent(id)}/${view}`)) notFound();
  return <RunDetailPage view={view} />;
}
