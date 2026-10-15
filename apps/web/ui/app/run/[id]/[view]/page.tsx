import { notFound } from "next/navigation";
import { isDetailView, parseRunPath } from "@/lib/runRoute";
import { RunDetailPage } from "@/components/RunDetailPage";

/**
 * /run/<id>/<view> — a run detail view as a real, bookmarkable page.
 * The view segment is whitelisted; anything else is a 404. The run id comes
 * from the path at runtime (client component), so we only validate the shape
 * server-side here.
 */
export function generateStaticParams() {
  return [];
}

export default function RunViewPage({ params }: { params: { id: string; view: string } }) {
  const { id, view } = params;
  if (!id || !view || !isDetailView(view)) notFound();
  if (!parseRunPath(`/run/${encodeURIComponent(id)}/${view}`)) notFound();
  return <RunDetailPage view={view} />;
}
